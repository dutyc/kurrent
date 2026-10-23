"""控制面认证器链 + 授权/会话。

分层（认证/授权/审计分离）：
- 认证（authenticate 路由依赖）：三种凭据串行短试（首个成功即止），产出 Identity；全失败 401
  1. X-Client-Cert-DN + X-Client-Class=human：nginx 443 mTLS 校验后按 issuer 注入；
     CN 必须存在于 state/users.yml，组来自该文件（证书字段不承载组）
  2. Authorization: Bearer：管理 token（pki 台账校验，usage=admin）
  3. Cookie kurrent_session：HMAC-SHA256 签名会话（滑动续期），浏览器登录态
- 授权（authorize_request 路由依赖）：读请求 viewer 组即可，写请求需 admin 组
- 主体类（principal.py）：路由声明允许类，全局依赖在认证前比对并拒绝越类访问
- 审计：authenticate 把身份写入 state.current_actor，OperationLog 自动附加 actor

信任前提：X-Client-Class 只由 nginx 在 mTLS SUCCESS 时按 issuer 注入；控制面只监听
unix socket，客户端无法自带头绕过（P1）。证书不再解析 O 字段。

开发模式：KURRENT_CP_INSECURE=1 显式放行（Identity=insecure/admin 组），生产严禁；
不存在"未配置凭据即放行"的空放行路径。
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import urlparse

from fastapi import HTTPException, Request, Response

from . import pki, sessions, state, users
from .config import (ADMIN_GROUP, CLIENT_CERT_DN_HEADER, CLIENT_CLASS_HEADER,
                     SESSION_TTL_SECONDS, settings)

log = logging.getLogger("control-plane")

# __Host- 前缀：Secure + Path=/ + 无 Domain（浏览器强制约束，防子域注入）
SESSION_COOKIE = "__Host-kurrent_session"
LEGACY_SESSION_COOKIE = "kurrent_session"  # 无状态旧会话：仅用于登出时清除
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class Identity:
    """认证结果：name = 用户名/凭据标识，groups = 组列表，principal_class = 主体类。

    scopes 仅自动化 token 使用（admin / readonly / enroll）；证书与会话为空。
    """

    __slots__ = ("name", "groups", "method", "principal_class",
                 "issuer", "serial", "fingerprint", "scopes")

    def __init__(self, name: str, groups: list[str], method: str, *,
                 principal_class: str = "human", issuer: str = "",
                 serial: str = "", fingerprint: str = "",
                 scopes: list[str] | None = None):
        self.name = name
        self.groups = groups
        self.method = method
        self.principal_class = principal_class
        self.issuer = issuer
        self.serial = serial
        self.fingerprint = fingerprint
        self.scopes = list(scopes or [])

    @property
    def is_admin(self) -> bool:
        return ADMIN_GROUP in self.groups

    def __repr__(self) -> str:
        return (f"Identity(name={self.name!r}, groups={self.groups!r}, "
                f"method={self.method!r}, class={self.principal_class!r})")


# ============================ 证书 DN 解析 ============================

def parse_dn(dn: str) -> tuple[str, list[str]]:
    """解析客户端证书 DN（/CN=admin/O=admin 或 CN=admin,O=admin）→ (CN, [O...])。"""
    cn = ""
    orgs: list[str] = []
    for part in dn.replace("/", ",").split(","):
        key, _, value = part.partition("=")
        key, value = key.strip().upper(), value.strip()
        if not value:
            continue
        if key == "CN" and not cn:
            cn = value
        elif key == "O":
            orgs.append(value)
    return cn, orgs


def _identity_from_dn(dn: str, *, issuer: str = "", serial: str = "",
                      fingerprint: str = "") -> Identity:
    """证书身份：CN 必须在 users.yml 中，组只来自该文件（未知用户 403）。"""
    cn, _ = parse_dn(dn)
    if not cn:
        raise HTTPException(status_code=401, detail="unauthorized: empty client certificate DN")
    groups = users.user_groups(settings.users_file, cn)
    if groups is None:
        raise HTTPException(status_code=403, detail=f"forbidden: unknown user {cn}")
    return Identity(name=cn, groups=groups, method="cert", principal_class="human",
                    issuer=issuer, serial=serial, fingerprint=fingerprint)


# ============================ 会话签名 ============================

def _session_key() -> bytes:
    try:
        return settings.session_key_file.read_bytes()
    except OSError:
        return b""


def sign_session(jti: str, exp: int, epoch: int) -> str:
    """签发会话票据：base64url(payload).base64url(hmac)。

    payload 只含 {jti, exp, epoch}——身份与组以服务端登记为准，票据不承载权限。
    """
    payload = json.dumps(
        {"jti": jti, "exp": int(exp), "epoch": int(epoch)},
        separators=(",", ":"),
    ).encode()
    body = base64.urlsafe_b64encode(payload).rstrip(b"=")
    sig = hmac.new(_session_key(), body, hashlib.sha256).digest()
    return (body + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")).decode()


def verify_session(raw: str) -> dict | None:
    """校验会话票据（签名 + 未过期）；失败返回 None。"""
    body, _, sig = raw.partition(".")
    if not body or not sig:
        return None
    key = _session_key()
    if not key:
        return None
    expected = hmac.new(key, body.encode(), hashlib.sha256).digest()
    try:
        got = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None
    if not hmac.compare_digest(expected, got):
        return None
    if int(data.get("exp", 0)) <= time.time():
        return None
    return data


def session_cookie_kwargs() -> dict:
    """会话 Cookie 属性（HttpOnly + Secure + SameSite=Lax，仅经 443 使用）。"""
    return {
        "key": SESSION_COOKIE,
        "httponly": True,
        "secure": True,
        "samesite": "lax",
        "path": "/",
        "max_age": SESSION_TTL_SECONDS,
    }


def start_session(ident: Identity) -> tuple[str, int, int]:
    """按当前身份登记服务端会话，返回 (cookie_value, exp, epoch)。"""
    cert_fp = ident.fingerprint if ident.method in {"cert", "session"} else ""
    jti, exp, epoch = sessions.create(ident.name, ident.groups,
                                      ttl_seconds=SESSION_TTL_SECONDS, cert_fp=cert_fp,
                                      scopes=ident.scopes)
    return sign_session(jti, exp, epoch), exp, epoch


# ============================ 认证器链 ============================

def _cert_authenticator(request: Request) -> Identity | None:
    dn = request.headers.get(CLIENT_CERT_DN_HEADER, "").strip()
    cls = request.headers.get(CLIENT_CLASS_HEADER, "").strip().lower()
    # 只有 nginx 按 issuer 判定的 human 证书进入管理身份；component/control 类
    # 即使带了 DN 头也不被当作人类用户（越类访问由 principal 矩阵返回 403）
    if not dn or cls != "human":
        return None
    return _identity_from_dn(
        dn,
        issuer=request.headers.get("X-Client-Cert-Issuer", ""),
        serial=request.headers.get("X-Client-Cert-Serial", ""),
        fingerprint=request.headers.get("X-Client-Cert-Fingerprint", ""),
    )


def _bearer_authenticator(request: Request) -> Identity | None:
    auth = request.headers.get("Authorization", "")
    if not auth:
        return None
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="unauthorized: malformed Authorization header")
    token = auth[len("Bearer "):].strip()
    try:
        entry = pki.validate_admin_token(settings.pki_dir, token)
    except ValueError as exc:
        log.info("auth: bearer rejected: %s", exc)
        raise HTTPException(status_code=401, detail="unauthorized: invalid token") from None
    token_id = token.partition(".")[0]
    scopes = list(entry.get("scopes") or ["admin"])
    groups = list(entry.get("groups") or [])
    # scope 是权限上限：台账里的组不得越过 scope（readonly/enroll token 永不当 admin）
    if "admin" not in scopes:
        groups = [group for group in groups if group != ADMIN_GROUP]
    pki.touch_token(settings.pki_dir, token_id)
    return Identity(name=f"token:{token_id}", groups=groups,
                    method="token", principal_class="automation",
                    scopes=scopes, issuer="token", fingerprint=token_id)


def _session_authenticator(request: Request) -> Identity | None:
    raw = request.cookies.get(SESSION_COOKIE, "")
    if not raw:
        return None
    data = verify_session(raw)
    if data is None:
        raise HTTPException(status_code=401, detail="unauthorized: invalid session")
    resolved = sessions.resolve(str(data.get("jti", "")))
    if resolved is None:
        raise HTTPException(status_code=401, detail="unauthorized: session revoked or expired")
    key, entry = resolved
    if int(data.get("epoch", -1)) != sessions.epoch():
        raise HTTPException(status_code=401, detail="unauthorized: session epoch changed")
    name = str(entry.get("user") or "")
    if name.startswith("token:"):
        # 自动化 token 派生会话：组与 scope 来自登记（签发时的 token 快照）
        groups = list(entry.get("groups") or [])
        principal, scopes = "automation", list(entry.get("scopes") or [])
    else:
        # 人类会话：每次请求重查 users.yml——删用户立即失效，改组立即生效
        groups = users.user_groups(settings.users_file, name)
        if groups is None:
            sessions.revoke_user(name)
            raise HTTPException(status_code=401, detail="unauthorized: session user no longer exists")
        principal, scopes = "human", []
    sessions.touch(key)
    request.state.session_key = key
    return Identity(name=name, groups=groups, method="session", principal_class=principal,
                    fingerprint=str(entry.get("cert_fp") or ""), scopes=scopes,
                    issuer="session")


async def authenticate(request: Request, response: Response) -> Identity:
    """FastAPI 依赖：认证器链（串行短路——首个成功即止；凭据无效即 401，不降级）。

    async 依赖保证 Identity 写入 request.state 与 current_actor 在同一请求上下文
    （同步依赖在线程池副本中执行，contextvar 无法回传同步路由的处理线程）。
    """
    ident: Identity | None = getattr(request.state, "identity", None)
    if ident is not None:
        return ident
    if settings.insecure:
        ident = Identity(name="insecure", groups=[ADMIN_GROUP], method="insecure",
                         principal_class="human")
    else:
        ident = None
        for authn in (_cert_authenticator, _bearer_authenticator, _session_authenticator):
            ident = authn(request)
            if ident is not None:
                break
    if ident is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    if ident.method == "session" and request.method in WRITE_METHODS:
        _check_origin(request)
    request.state.identity = ident
    state.current_actor.set(ident.name)
    if ident.method == "session":
        _refresh_session(request, response)
    return ident


def _check_origin(request: Request) -> None:
    """Cookie 认证的写请求必须同源：Origin 与 Host 不一致即拒绝（CSRF 防线）。"""
    origin = request.headers.get("origin", "").strip()
    if not origin:
        return
    host = request.headers.get("host", "").strip()
    if urlparse(origin).netloc != host:
        raise HTTPException(status_code=403, detail="forbidden: cross-origin write")


def _refresh_session(request: Request, response: Response) -> None:
    """会话滑动续期：剩余不足一半 TTL 时续写登记与 Cookie（其余认证器无会话状态）。"""
    raw = request.cookies.get(SESSION_COOKIE, "")
    data = verify_session(raw)
    if not data:
        return
    if int(data.get("exp", 0)) - time.time() >= SESSION_TTL_SECONDS / 2:
        return
    key = getattr(request.state, "session_key", None)
    if not key:
        return
    exp = sessions.renew(key, SESSION_TTL_SECONDS)
    response.set_cookie(value=sign_session(str(data.get("jti", "")), exp, sessions.epoch()),
                        **session_cookie_kwargs())


def authorize_request(request: Request) -> None:
    """FastAPI 依赖（挂在 authenticate 之后）：读请求只需已认证；写请求需 admin 组。

    enroll scope 的自动化 token 是唯一例外：只能 POST /pki/tokens（签发节点引导
    token），读请求与其他写操作一律拒绝——scope 语义是「仅签发」，不能借读接口
    枚举设备、Worker、凭据台账与会话。
    """
    ident: Identity | None = getattr(request.state, "identity", None)
    if ident is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    if "enroll" in ident.scopes and not ident.is_admin:
        if request.method == "POST" and request.url.path == "/pki/tokens":
            return
        raise HTTPException(status_code=403,
                            detail="forbidden: enroll scope permits only POST /pki/tokens")
    if request.method not in WRITE_METHODS or ident.is_admin:
        return
    raise HTTPException(status_code=403, detail=f"forbidden: {request.method} requires admin group")
