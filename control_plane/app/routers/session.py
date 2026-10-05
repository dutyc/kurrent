"""会话与身份端点：管理凭据换会话 Cookie（浏览器登录态）、登出、whoami。

认证器链已在 authenticate 完成（证书 DN / 管理 token / 会话）；POST /session 把当前
凭据提升为 HttpOnly 签名会话（浏览器免重复携带凭据），DELETE /session 登出，
GET /whoami 返回当前身份（WebUI 进入前探测——有证书者直通，无证书者展示登录页）。
"""

from fastapi import APIRouter, Depends, Request, Response

from ..auth import SESSION_COOKIE, Identity, authenticate, session_cookie_kwargs, sign_session

router = APIRouter(dependencies=[Depends(authenticate)])


@router.get("/whoami")
def whoami(request: Request) -> dict:
    """返回当前请求身份（认证器链结果）。"""
    ident: Identity = request.state.identity
    return {"name": ident.name, "groups": ident.groups, "method": ident.method}


@router.post("/session")
def create_session(request: Request, response: Response) -> dict:
    """凭据（证书 / 管理 token）换会话 Cookie：登录态在 TTL 内滑动续期。"""
    ident: Identity = request.state.identity
    response.set_cookie(value=sign_session(ident.name, ident.groups), **session_cookie_kwargs())
    return {"name": ident.name, "groups": ident.groups, "method": ident.method}


@router.delete("/session")
def delete_session(response: Response) -> dict:
    """登出：清除会话 Cookie（无状态签名会话，服务端无吊销态）。"""
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "logged-out"}
