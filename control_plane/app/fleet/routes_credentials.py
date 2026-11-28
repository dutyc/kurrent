"""Worker 级 NVMe-oF 凭据端点：设置/查询/吊销（密钥本体不经 API 返回）。"""


from fastapi import Depends, HTTPException, Request

from ..auth.auth import authenticate, authorize_request
from ..core.models import CredentialRequest
from ..core.stores import credentials, record, store
from ..core.utils import canonical_id, client_host, now_iso
from ..auth.principal import PrincipalRouter

from .credentials import _credential_meta, _credential_push_view, _next_generation, _push_credentials, _secret_hash, _validate_dhhc1

router = PrincipalRouter(
    dependencies=[Depends(authenticate), Depends(authorize_request)],
    principal={"human", "automation"},
)


@router.put("/workers/{worker_id}/credential")
def set_worker_credential(worker_id: str, req: CredentialRequest, request: Request):
    """设置/更新 NVMe-oF 认证密钥（DHHC-1，按 Worker 跟盘裁定）：密钥库键 = worker_id。
    自检失败 422；worker 不存在 404；重复设置同值幂等（updated_at 不变，但仍触发推送补推）。
    响应含 push 投影：逐 Agent 送达状态（未送达由对账线程重放）。"""
    worker_id = canonical_id(worker_id)
    with store.locked():
        worker_record = store.load_workers()["workers"].get(worker_id)
        if worker_record is None:
            raise HTTPException(404, f"worker not found: {worker_id}")
    secret = req.secret.strip()
    try:
        _validate_dhhc1(secret)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    now = now_iso()
    client_ip = client_host(request)
    with credentials.locked():
        creds = credentials.load()
        entry = creds["credentials"].get(worker_id)
        if entry and entry.get("secret") == secret:
            record("credential.set", "ok", worker_id=worker_id, changed=False, client=client_ip)
            # 幂等重放也触发推送：上次推送失败（agent 不在线/中途失败）时，
            # 重放同值密钥可补推；agent 侧 set_host 幂等，重复推送无害。
            _push_credentials(worker_id, secret=secret)
            return {**_credential_meta(entry),
                    "push": _credential_push_view(worker_id, worker_record, desired=secret)}
        entry = {
            "worker_id": worker_id,
            "secret": secret,
            "secret_hash": _secret_hash(secret),
            "created_at": creds["credentials"].get(worker_id, {}).get("created_at", now),
            "updated_at": now,
        }
        creds["credentials"][worker_id] = entry
        credentials.save(creds)
    record("credential.set", "ok", worker_id=worker_id, changed=True, client=client_ip)
    _push_credentials(worker_id, secret=secret)
    return {**_credential_meta(entry),
            "push": _credential_push_view(worker_id, worker_record, desired=secret)}


@router.get("/workers/{worker_id}/credential")
def get_worker_credential(worker_id: str, request: Request):
    """凭据元数据 + 推送状态投影（不返回明文）：exists / secret_hash 前缀 / 更新时间；
    push 为逐 Agent 送达状态（对账依据，见 reconcile_credentials_once）。"""
    worker_id = canonical_id(worker_id)
    with store.locked():
        worker_record = store.load_workers()["workers"].get(worker_id)
        if worker_record is None:
            raise HTTPException(404, f"worker not found: {worker_id}")
    with credentials.locked():
        entry = credentials.load()["credentials"].get(worker_id)
    record("credential.get", "ok", worker_id=worker_id, exists=bool(entry), client=client_host(request))
    if not entry:
        return {"worker_id": worker_id, "exists": False,
                "push": _credential_push_view(worker_id, worker_record, desired=None)}
    return {**_credential_meta(entry),
            "push": _credential_push_view(worker_id, worker_record, desired=entry.get("secret"))}


@router.delete("/workers/{worker_id}/credential")
def delete_worker_credential(worker_id: str, request: Request):
    """删除密钥（吊销该 worker 名下全部绑定设备的认证资格）：下次启动拒绝引导。
    零明文后没有明文回落路径（PSK 由认证密钥派生，密钥一没就无可用凭据），故吊销即
    拒绝该 worker 的引导。响应 push 投影反映吊销推送送达状态（未送达由对账重放，不会静默滞留）。"""
    worker_id = canonical_id(worker_id)
    with store.locked():
        worker_record = store.load_workers()["workers"].get(worker_id)
        if worker_record is None:
            raise HTTPException(404, f"worker not found: {worker_id}")
    with credentials.locked():
        creds = credentials.load()
        if worker_id not in creds["credentials"]:
            raise HTTPException(404, f"credential not found: {worker_id}")
        del creds["credentials"][worker_id]
        credentials.save(creds)
    # 吊销也要推进 generation：迟到的旧 set push 不能把 key 加回来
    _next_generation(worker_id)
    record("credential.revoke", "ok", worker_id=worker_id, client=client_host(request))
    _push_credentials(worker_id, secret=None)
    return {"worker_id": worker_id, "deleted": True,
            "push": _credential_push_view(worker_id, worker_record, desired=None)}
