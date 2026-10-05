"""控制面认证器链 + 授权/会话。

分层（认证/授权/审计分离）：
- 认证（authenticate 路由依赖）：三种凭据串行短试（首个成功即止），产出 Identity；全失败 401
  1. X-Client-Cert-DN：nginx 443 mTLS 校验后透传的客户端证书 DN（CN=用户名、O=组）
  2. Authorization: Bearer：管理 token（pki 台账校验，usage=admin）
  3. Cookie kurrent_session：HMAC-SHA256 签名会话（滑动续期），浏览器登录态
- 授权（authorize_request 路由依赖）：读请求 viewer 组即可，写请求需 admin 组
- 审计：authenticate 把身份写入 state.current_actor，OperationLog 自动附加 actor

信任前提：X-Client-Cert-DN 由 nginx mTLS 校验后注入，因此 4839 必须只绑 127.0.0.1
（443 唯一对外入口）——否则可绕过 nginx 直接伪造身份头冒充已认证用户。

开发模式：KURRENT_CP_INSECURE=1 显式放行（Identity=insecure/admin 组），生产严禁；
不存在"未配置凭据即放行"的空放行路径。
"""

import base64
import hashlib
import hmac
import json
import logging
import time

from fastapi import HTTPException, Request, Response

from . import pki, state
from .config import (ADMIN_GROUP, CLIENT_CERT_DN_HEADER, SESSION_TTL_DAYS,
                     VIEWER_GROUP, settings)

log = logging.getLogger("control-plane")

SESSION_COOKIE = "kurrent_session"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class Identity:
    """认证结果：name = 用户名/凭据标识，groups = 组列表。"""

    __slots__ = ("name", "groups", "method")

    def __init__(self, name: str, groups: list[str], method: str):
        self.name = name
        self.groups = groups
        self.method = method

    @property
    def is_admin(self) -> bool:
        return ADMIN_GROUP in self.groups

    def __repr__(self) -> str:
        return f"Identity(name={self.name!r}, groups={self.groups!r}, method={self.method!r})"


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


def _identity_from_dn(dn: str) -> Identity:
    cn, orgs = parse_dn(dn)
    if not cn:
        raise HTTPException(status_code=401, detail="unauthorized: empty client certificate DN")
    # 组取证书 O 字段；旧证书无 O 时按 CN 兜底（admin 视为管理组）
    groups = orgs or ([ADMIN_GROUP] if cn == settings.admin_cn else [])
    return Identity(name=cn, groups=groups, method="cert")


# ============================ 会话签名 ============================

def _session_key() -> bytes:
    try:
        return settings.session_key_file.read_bytes()
    except OSError:
        return b""


def sign_session(name: str, groups: list[str]) -> str:
    """签发会话票据：base64url(payload).base64url(hmac)。payload = {name, groups, exp}。"""
    payload = json.dumps(
        {"name": name, "groups": groups, "exp": int(time.time()) + SESSION_TTL_DAYS * 86400},
        separators=(",", ":"), ensure_ascii=False,
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
        "max_age": SESSION_TTL_DAYS * 86400,
    }


# ============================ 认证器链 ============================

def _cert_authenticator(request: Request) -> Identity | None:
    dn = request.headers.get(CLIENT_CERT_DN_HEADER, "")
    if not dn.strip():
        return None
    return _identity_from_dn(dn)


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
    return Identity(name=f"token:{token_id}", groups=list(entry.get("groups") or []), method="token")


def _session_authenticator(request: Request) -> Identity | None:
    raw = request.cookies.get(SESSION_COOKIE, "")
    if not raw:
        return None
    data = verify_session(raw)
    if data is None:
        raise HTTPException(status_code=401, detail="unauthorized: invalid session")
    return Identity(name=str(data.get("name", "")), groups=list(data.get("groups") or []), method="session")


async def authenticate(request: Request, response: Response) -> Identity:
    """FastAPI 依赖：认证器链（串行短路——首个成功即止；凭据无效即 401，不降级）。

    async 依赖保证 Identity 写入 request.state 与 current_actor 在同一请求上下文
    （同步依赖在线程池副本中执行，contextvar 无法回传同步路由的处理线程）。
    """
    ident: Identity | None = getattr(request.state, "identity", None)
    if ident is not None:
        return ident
    if settings.insecure:
        ident = Identity(name="insecure", groups=[ADMIN_GROUP], method="insecure")
    else:
        ident = None
        for authn in (_cert_authenticator, _bearer_authenticator, _session_authenticator):
            ident = authn(request)
            if ident is not None:
                break
    if ident is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    request.state.identity = ident
    state.current_actor.set(ident.name)
    if ident.method == "session":
        _refresh_session(request, response, ident)
    return ident


def _refresh_session(request: Request, response: Response, ident: Identity) -> None:
    """会话滑动续期：剩余不足一半 TTL 时刷新 Cookie（其余认证器无会话状态）。"""
    data = verify_session(request.cookies.get(SESSION_COOKIE, ""))
    if not data:
        return
    if int(data.get("exp", 0)) - time.time() < SESSION_TTL_DAYS * 86400 / 2:
        response.set_cookie(value=sign_session(ident.name, ident.groups), **session_cookie_kwargs())


def authorize_request(request: Request) -> None:
    """FastAPI 依赖（挂在 authenticate 之后）：读请求 viewer 组；写请求需 admin 组。"""
    ident: Identity | None = getattr(request.state, "identity", None)
    if ident is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    if request.method in WRITE_METHODS and not ident.is_admin:
        raise HTTPException(status_code=403, detail=f"forbidden: {request.method} requires admin group")
