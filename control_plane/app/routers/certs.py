"""users 域证书端点：签发、列表与吊销（CRL）。

签发只允许 users.yml 中已登记的用户（组来自该文件，证书只承载 CN）；私钥由申请者
本地生成并以 CSR 提交，控制面只签不存——丢失即用同一私钥重签或换钥重发，旧证书
可用序列号单独吊销。
吊销是写操作（admin 组）：登记标记 → 重签 clients.crl → reload nginx → 联动删除
该用户（或该证书）的会话。吊销后证书在下一次 TLS 握手即被 nginx 拒绝（CRL 校验），
无需等待证书自然过期；证书文件保留供审计，CRL 是唯一有效性权威。

components 域（agent / nvmet-host 的 nodes 域证书）同样列出与吊销：吊销写 nodes CRL
（clients.crl 的另一半），下一次 mTLS 握手即拒绝；同时删除该 agent 的 bootstrap
token，断掉「吊销后再 enroll 一张新证书」的路径。
"""

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from .. import pki, sessions, users
from ..auth import Identity, authenticate, authorize_request
from ..config import settings
from ..nginx_reload import reload_nginx
from ..principal import PrincipalRouter
from ..stores import record
from ..utils import client_host

router = PrincipalRouter(
    dependencies=[Depends(authenticate), Depends(authorize_request)],
    principal={"human", "automation"},
)


class IssueUserCertRequest(BaseModel):
    """签发参数：csr 必填（私钥留在申请者本地）；days 缺省 365，上限 USER_CERT_DAYS_MAX。"""

    csr: str = ""
    days: int | None = None


@router.post("/pki/users/{cn}", status_code=201)
def issue_user_cert(cn: str, request: Request, req: IssueUserCertRequest | None = None) -> dict:
    """按 CSR 为已登记用户签发证书（私钥不经过控制面）。

    CN 必须先存在于 users.yml（否则签出来也无法通过管理面认证），未知用户 400；
    CSR 的 CN 必须与路径一致，且除 CN 外不得携带 subject 属性。控制面只回传证书。
    """
    cn = cn.strip()
    if users.user_groups(settings.users_file, cn) is None:
        raise HTTPException(
            status_code=400,
            detail=f"unknown user {cn}: add it to users.yml before issuing a certificate",
        )
    if not req or not req.csr.strip():
        raise HTTPException(
            status_code=400,
            detail="csr required: generate a key and CSR locally (kurrent cert keygen)",
        )
    try:
        issued = pki.issue_user_cert(settings.pki_dir, cn, req.csr.encode(),
                                     days=req.days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record("pki.user.issue", "ok", client=client_host(request), cn=cn,
           serial=issued["serial"], expires_at=issued["expires_at"])
    return issued


@router.get("/pki/users")
def list_user_certs() -> list[dict]:
    """列出 users 域证书（含已吊销/已过期）：serial 为 hex，fingerprint 为 sha256。

    groups 来自 users.yml（证书本身只承载 CN）；已从目录移除的用户返回空组。
    """
    items = pki.list_user_certs(settings.pki_dir)
    groups = {cn: users.user_groups(settings.users_file, cn) or []
              for cn in {item["cn"] for item in items}}
    for item in items:
        item["groups"] = groups.get(item["cn"], [])
    return items


@router.delete("/pki/users/{cn}")
def revoke_user_cert(cn: str, request: Request, reason: str = "unspecified",
                     serial: str | None = None) -> dict:
    """吊销某用户的证书：标记吊销 + 重签 CRL + reload nginx + 踢掉相关会话。

    serial 省略 = 吊销该 CN 的全部未吊销证书；给出时只吊销该序列号（只踢绑定
    该证书指纹的会话）。不能吊销当前请求身份自己的证书（避免把自己锁在门外）；
    不存在未吊销证书返回 404；CRL 已写入但 nginx reload 失败返回 500（吊销尚未生效）。
    """
    ident: Identity = request.state.identity
    cn = cn.strip()
    if ident.name == cn:
        raise HTTPException(status_code=400, detail="refusing to revoke the current principal's certificate")
    try:
        revoked = pki.revoke_user_cert(settings.pki_dir, cn, reason, serial=serial)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not revoked:
        target = f" for serial {serial}" if serial else ""
        record("pki.user.revoke", "failed", client=client_host(request), cn=cn,
               serial=serial or "", error="no active certificate")
        raise HTTPException(status_code=404, detail=f"no active certificate{target} for {cn}")
    if serial is not None:
        kicked = sum(sessions.revoke_by_cert_fp(e["fingerprint"]) for e in revoked)
    else:
        kicked = sessions.revoke_user(cn)
    try:
        reload_nginx()
    except RuntimeError as exc:
        record("pki.user.revoke", "failed", client=client_host(request), cn=cn,
               serials=[e["serial"] for e in revoked], error=str(exc))
        raise HTTPException(
            status_code=500,
            detail=f"certificate revoked and CRL written, but nginx reload failed: {exc}",
        ) from exc
    record("pki.user.revoke", "ok", client=client_host(request), cn=cn,
           serials=[e["serial"] for e in revoked], sessions=kicked)
    return {
        "revoked": cn,
        "serials": [e["serial"] for e in revoked],
        "sessions": kicked,
        "reason": reason,
    }


@router.get("/pki/control")
def list_control_certs() -> list[dict]:
    """列出 control 域证书（控制面自身 client cert；含已吊销/已过期）。"""
    return pki.list_control_certs(settings.pki_dir)


@router.delete("/pki/control/{cn}")
def revoke_control_cert(cn: str, request: Request, reason: str = "unspecified",
                        serial: str | None = None) -> dict:
    """吊销控制面自身证书（control CA）：标记 + 重签 control CRL。

    节点在下一个刷新周期内拒绝该证书；控制面需要删除本地 client.crt 并重启以
    签发新证书，否则 cp→agent 出站与 agent 入站都会失败。不能吊销当前请求身份
    自己的证书；不存在未吊销证书返回 404。
    """
    ident: Identity = request.state.identity
    cn = cn.strip()
    if ident.name == cn:
        raise HTTPException(status_code=400,
                            detail="refusing to revoke the current principal's certificate")
    try:
        revoked = pki.revoke_control_cert(settings.pki_dir, cn, reason, serial=serial)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not revoked:
        target = f" for serial {serial}" if serial else ""
        record("pki.control.revoke", "failed", client=client_host(request), cn=cn,
               serial=serial or "", error="no active certificate")
        raise HTTPException(status_code=404,
                            detail=f"no active control certificate{target} for {cn}")
    record("pki.control.revoke", "ok", client=client_host(request), cn=cn,
           serials=[e["serial"] for e in revoked])
    return {
        "revoked": cn,
        "serials": [e["serial"] for e in revoked],
        "reason": reason,
    }


@router.get("/pki/components")
def list_component_certs() -> list[dict]:
    """列出 nodes 域组件证书（agent / nvmet-host；含已吊销/已过期）。"""
    return pki.list_component_certs(settings.pki_dir)


@router.delete("/pki/components/{agent_id}")
def revoke_component_cert(agent_id: str, request: Request, reason: str = "unspecified",
                          serial: str | None = None) -> dict:
    """吊销某节点的组件证书：标记吊销 + 重签 nodes CRL + reload nginx + 删引导 token。

    serial 省略 = 吊销该节点的全部未吊销证书（client + serving）；给出时只吊销该
    序列号。吊销后该证书在下一次 mTLS 握手即被拒绝，且不能再经 /enroll/renew 换新；
    已下发的 bootstrap token 一并删除，断掉重新 enroll 的路径。
    """
    agent_id = agent_id.strip()
    try:
        revoked = pki.revoke_component_cert(settings.pki_dir, agent_id, reason, serial=serial)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not revoked:
        target = f" for serial {serial}" if serial else ""
        record("pki.component.revoke", "failed", client=client_host(request),
               agent_id=agent_id, serial=serial or "", error="no active certificate")
        raise HTTPException(
            status_code=404,
            detail=f"no active component certificate{target} for {agent_id}",
        )
    for component in ("agent", "nvmet-host"):
        pki.revoke_agent_tokens(settings.pki_dir, agent_id, component)
    try:
        reload_nginx()
    except RuntimeError as exc:
        record("pki.component.revoke", "failed", client=client_host(request),
               agent_id=agent_id, serials=[e["serial"] for e in revoked], error=str(exc))
        raise HTTPException(
            status_code=500,
            detail=f"certificate revoked and CRL written, but nginx reload failed: {exc}",
        ) from exc
    record("pki.component.revoke", "ok", client=client_host(request), agent_id=agent_id,
           serials=[e["serial"] for e in revoked])
    return {
        "revoked": agent_id,
        "serials": [e["serial"] for e in revoked],
        "kinds": [e["kind"] for e in revoked],
        "reason": reason,
    }
