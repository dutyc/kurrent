"""会话与身份端点：凭据换会话、登出、whoami、会话台账（列表/逐个吊销/全踢）。

认证器链已在 authenticate 完成（证书 DN / 管理 token / 会话）；POST /session 把当前
凭据登记为服务端会话（Cookie 只带 jti，身份以 state/sessions.yml 为准），
DELETE /session 登出并删除登记。会话台账仅管理员可见。
"""

from fastapi import Depends, HTTPException, Request, Response

from .. import sessions
from ..auth import (LEGACY_SESSION_COOKIE, SESSION_COOKIE, Identity, authenticate,
                    session_cookie_kwargs, start_session, verify_session)
from ..principal import PrincipalRouter

router = PrincipalRouter(
    dependencies=[Depends(authenticate)],
    principal={"human", "automation"},
)


def _require_admin(request: Request) -> Identity:
    ident: Identity = request.state.identity
    if not ident.is_admin:
        raise HTTPException(status_code=403, detail="forbidden: session management requires admin group")
    return ident


@router.get("/whoami")
def whoami(request: Request) -> dict:
    """返回当前请求身份（认证器链结果）。"""
    ident: Identity = request.state.identity
    return {"name": ident.name, "groups": ident.groups, "method": ident.method}


@router.post("/session")
def create_session(request: Request, response: Response) -> dict:
    """凭据（证书 / 管理 token）换会话 Cookie：登记在服务端，删登记即失效。

    enroll scope 的 token 只用于签发节点引导 token，不换浏览器会话（否则会话
    继承 scope 后仍可能被误用于读管理面）。"""
    ident: Identity = request.state.identity
    if "enroll" in ident.scopes and not ident.is_admin:
        raise HTTPException(status_code=403,
                            detail="forbidden: enroll scope cannot create a session")
    value, _, _ = start_session(ident)
    response.set_cookie(value=value, **session_cookie_kwargs())
    # 旧无状态 Cookie（改名前的 kurrent_session）顺手清除，避免浏览器残留
    response.delete_cookie(LEGACY_SESSION_COOKIE, path="/")
    return {"name": ident.name, "groups": ident.groups, "method": ident.method}


@router.delete("/session")
def delete_session(request: Request, response: Response) -> dict:
    """登出：删除服务端登记并清除 Cookie（下一个请求立即 401）。"""
    data = verify_session(request.cookies.get(SESSION_COOKIE, ""))
    sessions.revoke_by_jti(str((data or {}).get("jti", "")))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(LEGACY_SESSION_COOKIE, path="/")
    return {"status": "logged-out"}


@router.get("/sessions")
def list_sessions(request: Request) -> list[dict]:
    """列出未过期会话（管理员）：session_id 是 jti 的 sha256，不是可用凭据。"""
    _require_admin(request)
    return sessions.list_sessions()


@router.delete("/sessions/{session_id}")
def revoke_session(session_id: str, request: Request) -> dict:
    """逐个吊销（管理员）：删除登记，下一请求 401。"""
    _require_admin(request)
    if not sessions.revoke(session_id):
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return {"revoked": session_id}


@router.delete("/sessions")
def revoke_all_sessions(request: Request) -> dict:
    """全部踢下线（管理员）：清空登记并递增 epoch。"""
    _require_admin(request)
    return {"revoked": sessions.revoke_all()}
