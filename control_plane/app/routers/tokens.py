"""token 端点：bootstrap 引导凭据 + 管理 token 的签发 / 列表 / 吊销。

不带任何节点信息（不绑节点）：节点身份由节点自决——kurrent join
自动生成随机短标识（人工输入归属声明 metadata.remark，备注），enroll 自动登记。
每次调用新签（明文不可恢复，登记只存 sha256 hash）；TTL 内可被多次使用
（不限制使用次数），任意存储节点可用同一 bootstrap token 引导。
usage=admin 的管理 token 不绑组件，供认证器链 Bearer（CLI --token / 脚本）与 WebUI
登录使用（groups 携带授权组）。
GET 列出未过期条目（台账可视，不返回明文/hash）；DELETE 吊销 = 删条目
（删除即失效；留痕在操作审计）。吊销为写操作，经 authorize_request 要求 admin 组。
nvmet-host 组件凭据不在此签发：agent enroll 上报 backend=nvmet 时控制面派生
随响应下发（能力上报驱动，签发不预知后端）。
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import config, pki
from ..auth import authenticate, authorize_request
from ..stores import record
from ..utils import client_host

router = APIRouter(dependencies=[Depends(authenticate), Depends(authorize_request)])


class IssueTokenRequest(BaseModel):
    """签发参数：bootstrap（节点引导，默认）| admin（管理 token，认证器链 Bearer 用）。"""
    usage: Literal["bootstrap", "admin"] = "bootstrap"


@router.post("/pki/tokens", status_code=201)
def issue_token(request: Request, req: IssueTokenRequest | None = None):
    """签发 token，返回明文（仅本次响应可见）。

    响应含 token / expires_at / usage / groups；join 命令示例由 CLI 侧组装（kurrent token create）。
    """
    if req and req.usage == "admin":
        token = pki.issue_bootstrap_token(
            config.settings.pki_dir, component="", usage=["admin"],
            groups=[config.ADMIN_GROUP], name="admin",
        )
    else:
        token = pki.issue_bootstrap_token(config.settings.pki_dir)
    token_id = token.split(".", 1)[0]
    info = pki.get_bootstrap_token(config.settings.pki_dir, token_id) or {}
    record("pki.token", "ok", client=client_host(request), token_id=token_id,
           usage=info.get("usage", []))
    return {
        "token": token,
        "expires_at": info.get("expires_at", ""),
        "usage": info.get("usage", []),
        "groups": info.get("groups", []),
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
        raise HTTPException(404, f"token not found: {token_id}")
    record("pki.token.revoke", "ok", client=client_host(request), token_id=token_id)
    return {"revoked": token_id}
