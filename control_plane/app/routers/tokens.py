"""token 端点：bootstrap 引导凭据 + 管理 token 的签发 / 列表 / 吊销。

bootstrap token 必须绑定 component + agent_id：签发时指定节点身份，enroll 只接受
同一 agent_id 的请求（不再有「任意节点可用」的通用 token）。TTL 内可复用（一次性
模式见认证蓝图 P3）。usage=admin 的管理 token 不绑组件，供认证器链 Bearer
（CLI --token / 脚本）与 WebUI 登录使用（groups 携带授权组）。
GET 列出未过期条目（台账可视，不返回明文/hash）；DELETE 吊销 = 删条目
（删除即失效；留痕在操作审计）。吊销为写操作，经 authorize_request 要求 admin 组。
nvmet-host 组件凭据不在此签发：agent enroll 上报 backend=nvmet 时控制面派生
随响应下发（能力上报驱动，签发不预知后端）。
"""

from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from .. import audit, config, pki
from ..auth import authenticate, authorize_request
from ..stores import record
from ..utils import client_host

from ..principal import PrincipalRouter

router = PrincipalRouter(
    dependencies=[Depends(authenticate), Depends(authorize_request)],
    principal={"human", "automation"},
)


class IssueTokenRequest(BaseModel):
    """签发参数：bootstrap（节点引导，需绑 agent_id）| admin（管理 token）。

    admin token 可用 scopes 收窄权限（admin/readonly/enroll）与 ttl_hours
    （默认 24h，上限 720h）。readonly/enroll token 不得签发 admin token。
    """
    usage: Literal["bootstrap", "admin"] = "bootstrap"
    agent_id: str = ""
    component: Literal["agent", "nvmet-host"] = "agent"
    scopes: list[str] = []
    ttl_hours: int | None = None
    max_uses: int = 1


@router.post("/pki/tokens", status_code=201)
def issue_token(request: Request, req: IssueTokenRequest | None = None):
    """签发 token，返回明文（仅本次响应可见）。

    响应含 token / expires_at / usage / groups / scopes / agent_id；join 命令示例
    由 CLI 侧组装（kurrent token create --agent-id）。
    """
    ident = request.state.identity
    if req and req.usage == "admin":
        if ident.method == "token" and not ident.is_admin:
            raise HTTPException(403, "only an admin token may issue admin tokens")
        try:
            token = pki.issue_bootstrap_token(
                config.settings.pki_dir, component="", usage=["admin"],
                scopes=req.scopes, ttl_hours=req.ttl_hours, name="admin",
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        agent_id = (req.agent_id if req else "").strip()
        if not agent_id:
            raise HTTPException(400, "bootstrap token requires agent_id")
        try:
            token = pki.issue_bootstrap_token(
                config.settings.pki_dir, agent_id=agent_id,
                component=req.component if req else "agent",
                max_uses=req.max_uses if req else 1,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    token_id = token.split(".", 1)[0]
    info = pki.get_bootstrap_token(config.settings.pki_dir, token_id) or {}
    record("pki.token", "ok", client=client_host(request), token_id=token_id,
           usage=info.get("usage", []))
    audit.emit("credential", principal_class=ident.principal_class, principal=ident.name,
               issuer=ident.issuer or "session", route="POST /pki/tokens",
               decision="allow", reason="token_issued", token_id=token_id,
               request_id=getattr(request.state, "request_id", ""),
               client_ip=client_host(request))
    return {
        "token": token,
        "expires_at": info.get("expires_at", ""),
        "usage": info.get("usage", []),
        "groups": info.get("groups", []),
        "scopes": info.get("scopes", []),
        "uses": info.get("uses", 0),
        "max_uses": info.get("max_uses", 0),
        "agent_id": info.get("agent_id", ""),
        "component": info.get("component", ""),
    }


@router.get("/pki/tokens")
def list_tokens():
    """列出未过期 token 台账条目（管理可视：bootstrap / 管理 / nvmet-host 派生）。

    不返回明文（本就不可恢复）与 secret hash；created_at 对旧条目为空串（签发时才注入）。
    """
    return pki.list_bootstrap_tokens(config.settings.pki_dir)


@router.delete("/pki/tokens/{token_id}")
def revoke_token(token_id: str, request: Request):
    """吊销（删除）一个 token 条目（无 tombstone）：删除即失效。

    Bearer 校验每请求重读台账（无缓存），吊销后立即 401；「谁在何时吊销了哪个 token」
    由操作审计留痕。不存在返回 404。
    """
    token_id = token_id.strip()
    if not pki.revoke_bootstrap_token(config.settings.pki_dir, token_id):
        record("pki.token.revoke", "failed", client=client_host(request), token_id=token_id,
               error="token not found")
        audit.emit("credential", route="DELETE /pki/tokens/{token_id}", decision="deny",
                   reason="token_not_found", token_id=token_id,
                   request_id=getattr(request.state, "request_id", ""))
        raise HTTPException(404, f"token not found: {token_id}")
    record("pki.token.revoke", "ok", client=client_host(request), token_id=token_id)
    audit.emit("credential", route="DELETE /pki/tokens/{token_id}", decision="allow",
               reason="token_revoked", token_id=token_id,
               request_id=getattr(request.state, "request_id", ""))
    return {"revoked": token_id}
