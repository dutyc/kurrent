"""Agent HTTP 接口：健康探针、LUN/母盘管理、凭据推送、操作日志。

除 /healthz 外全部要求 mTLS 客户端证书（control CA + 应用层 CN/CRL 校验）。
"""

import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import disks, security
from .backends import backend
from .config import DEFAULT_PKI_DIR
from .oplog import oplog
from .runtime import BACKEND, DISK_DIR, IQN_BASE, NQN_BASE

router = APIRouter()


def verify_client_cert(request: Request) -> None:
    """mTLS 客户端证书鉴权（control CA 信任模型 + 应用层 CRL/CN）。

    TLS 层以 ssl_ca_certs=ca-control.crt + CERT_REQUIRED 强制客户端证书链；
    应用层再从 peer cert 校验 issuer/CN 与 control CRL（可热更新，无需重启）。
    """
    security.verify_peer(
        request.scope,
        allowed_cns={security.CONTROL_PLANE_CN},
        issuer_cn=security.ca_subject_cn(str(Path(DEFAULT_PKI_DIR) / "ca-control.crt")),
        crl_kind="control",
        store=security.CRL_STORE,
    )


@contextmanager
def logged(op: str, req_dict: dict, client: str):
    """写操作外壳：成功记 ok，任何失败记 failed + 原因，再抛出。"""
    try:
        yield
        oplog.record(op, req_dict, "ok", client)
    except HTTPException as e:
        oplog.record(op, req_dict, "failed", client, error=str(e.detail))
        raise
    except Exception as e:
        oplog.record(op, req_dict, "failed", client, error=str(e))
        raise


class DiskReq(BaseModel):
    iqn: str
    master: Optional[str] = None
    size: Optional[str] = None
    filename: Optional[str] = None


class CdReq(BaseModel):
    iso: str
    iqn: Optional[str] = None


class CredentialPushReq(BaseModel):
    """NVMe-oF 凭据推送（控制面驱动，仅 nvmet 后端）：
    secret=null 吊销该 worker；sub_nqns = 该 worker 在本节点的盘子系统 NQN，host_nqns = 绑定设备派生 NQN。
    tls_psk = 目标端 TLS-PSK（由控制面从认证密钥派生，十六进制）；随 secret 一并同步/吊销。"""
    worker_id: str
    secret: Optional[str] = None
    sub_nqns: list = []
    host_nqns: list = []
    tls_psk: Optional[str] = None
    generation: int = 0


class CredentialStatusReq(BaseModel):
    """只读查询某 worker 在本节点的目标端 PSK 是否在位（控制面对账用）。"""
    worker_id: str
    sub_nqns: list = []
    host_nqns: list = []


# ── 后端就绪度探针（healthz 投影，仅 nvmet 后端）──
# 短超时（探活要快，宿主服务不可达时秒回）+ 短缓存（控制面/编排会周期探测，避免把探活做成重操作）
BACKEND_PROBE_TTL = 5.0
BACKEND_PROBE_TIMEOUT = 2.0
_backend_probe_lock = threading.Lock()
_backend_probe_cache: dict = {"ts": 0.0, "value": None}


def _backend_probe() -> Optional[dict]:
    """宿主服务就绪度（nvmet 后端）：可达且 configfs 就绪才算 ready；其他后端无独立探针（None）。"""
    from .nvmet import NvmetBackend, NvmetHostError
    if not isinstance(backend, NvmetBackend):
        return None
    with _backend_probe_lock:
        if time.monotonic() - _backend_probe_cache["ts"] < BACKEND_PROBE_TTL:
            return _backend_probe_cache["value"]
    try:
        info = backend.host.healthz(timeout=BACKEND_PROBE_TIMEOUT)
        ready = bool(info.get("configfs"))
        value = {"ready": ready, "reachable": True,
                 "detail": None if ready else "nvmet-host reachable; configfs not ready"}
    except NvmetHostError as exc:
        value = {"ready": False, "reachable": False, "detail": exc.detail}
    with _backend_probe_lock:
        _backend_probe_cache.update(ts=time.monotonic(), value=value)
    return value


@router.get("/healthz")                          # 唯一不保护的接口，给健康检查/探活
def healthz():
    """进程存活（status 恒 ok）+（nvmet 后端）数据面就绪度。

    backend_ready=false 表示在线但存储不可用（nvmet-host 不可达或 configfs 未就绪）——
    控制面据此把节点标为 degraded，与「节点掉线（error）」区分开。"""
    resp = {"status": "ok", "crl": security.CRL_STORE.describe()}
    probe = _backend_probe()
    if probe is not None:
        resp["backend"] = BACKEND
        resp["backend_ready"] = probe["ready"]
        if probe["detail"]:
            resp["backend_detail"] = probe["detail"]
    return resp


@router.post("/credential", dependencies=[Depends(verify_client_cert)])
def push_credential(req: CredentialPushReq, request: Request):
    """NVMe-oF 凭据推送（控制面驱动，仅 nvmet 后端）：更新缓存 + 转调宿主服务同步 hosts 矩阵。
    secret=null 吊销该 worker 全部绑定设备认证；审计不记密钥本体。"""
    from .nvmet import NvmetBackend, NvmetHostError
    if not isinstance(backend, NvmetBackend):
        raise HTTPException(400, "credential push requires backend=nvmet")
    req_dict = {"worker_id": req.worker_id, "secret": bool(req.secret),
                "sub_nqns": req.sub_nqns, "host_nqns": req.host_nqns,
                "tls": bool(req.tls_psk)}
    with logged("credential", req_dict, request.client.host):
        sub_nqns = [disks.safe_nqn(x, "sub_nqns") for x in req.sub_nqns]
        host_nqns = [disks.safe_nqn(x, "host_nqns") for x in req.host_nqns]
        try:
            return backend.cache.apply(req.worker_id, req.secret, sub_nqns,
                                       host_nqns, req.tls_psk, req.generation)
        except NvmetHostError as exc:
            raise HTTPException(503, f"nvmet host error: {exc.detail}") from exc


@router.post("/credential/status", dependencies=[Depends(verify_client_cert)])
def credential_status(req: CredentialStatusReq):
    """只读查询目标端 key 在位状态；不写缓存、不改变宿主状态。"""
    from .nvmet import NvmetBackend, NvmetHostError
    if not isinstance(backend, NvmetBackend):
        raise HTTPException(400, "credential status requires backend=nvmet")
    try:
        sub_nqns = [disks.safe_nqn(x, "sub_nqns") for x in req.sub_nqns]
        host_nqns = [disks.safe_nqn(x, "host_nqns") for x in req.host_nqns]
        return backend.cache.credential_status(req.worker_id, sub_nqns, host_nqns)
    except NvmetHostError as exc:
        raise HTTPException(503, f"nvmet host error: {exc.detail}") from exc


@router.post("/lun/disk", dependencies=[Depends(verify_client_cert)])
def create_disk(req: DiskReq, request: Request):
    req_dict = {"iqn": req.iqn, "master": req.master, "size": req.size, "filename": req.filename}
    with logged("disk", req_dict, request.client.host):
        disks.check_iqn(req.iqn)
        iqn = req.iqn.lower()
        filename = req.filename or disks.iqn_to_filename(iqn)
        backing = disks.disk_path(filename, "filename")
        if disks.file_exists(backing):
            raise HTTPException(409, f"file exists: {filename}")
        if req.master:
            master_path = disks.disk_path(req.master, "master")
            if not disks.file_exists(master_path):
                raise HTTPException(404, f"master not found: {req.master}")
            backend.validate_disk_file(master_path)
            disks.clone_master(master_path, backing)
        elif req.size:
            disks.make_empty(backing, req.size)
        else:
            raise HTTPException(400, "need master (clone) or size (empty disk)")
        try:
            backend.create_target(iqn, backing, cd=False)
        except HTTPException:
            disks.remove_file(backing)
            raise
        return {"iqn": iqn, "backing": backing}


@router.post("/lun/cd", dependencies=[Depends(verify_client_cert)])
def create_cd(req: CdReq, request: Request):
    req_dict = {"iso": req.iso, "iqn": req.iqn}
    with logged("cd", req_dict, request.client.host):
        iso_name = disks.safe_name(req.iso, "iso")
        iso_path = disks.disk_path(iso_name, "iso")
        if not disks.file_exists(iso_path):
            raise HTTPException(404, f"iso not found: {iso_name}")
        iqn = (req.iqn or f"{IQN_BASE}:{iso_name}").lower()
        disks.check_iqn(iqn)
        backend.create_target(iqn, iso_path, cd=True)
        return {"iqn": iqn, "backing": iso_path}


@router.post("/lun/scan", dependencies=[Depends(verify_client_cert)])
def scan(request: Request):
    try:
        result = backend.scan()
        oplog.record("scan", {}, "ok", request.client.host,
                     created=len(result["created"]), skipped=len(result["skipped"]))
        return result
    except HTTPException as e:
        oplog.record("scan", {}, "failed", request.client.host, error=str(e.detail))
        raise


@router.delete("/lun", dependencies=[Depends(verify_client_cert)])
def delete(iqn: str, request: Request, delete_file: bool = False):
    req_dict = {"iqn": iqn, "delete_file": delete_file}
    with logged("delete", req_dict, request.client.host):
        iqn = disks.safe_identifier(iqn)
        backings = []
        # nvmet 后端 list_targets 的 iqn 键实际为子系统 NQN（nqn. 前缀），
        # stgt/lio 后端则为 iqn. 前缀：请求可能是任一形态，前缀互转后双匹配
        alt = (
            f"nqn.{iqn[4:]}" if iqn.startswith("iqn.")
            else f"iqn.{iqn[4:]}" if iqn.startswith("nqn.")
            else None
        )
        t = next(
            (x for x in backend.list_targets()
             if x["iqn"] == iqn or (alt and x["iqn"] == alt)),
            None,
        )
        if t:
            backings = [l["backing"] for l in t["luns"] if l.get("backing")]
        backend.delete_target(iqn)
        if delete_file:
            for b in backings:
                # backing 是宿主服务视角路径（如 /srv/nvmet-disks/x.img），
                # agent 容器挂载同一目录于 KURRENT_DISK_DIR：basename 重建容器内路径
                disks.remove_file(disks.agent_disk_path(b))
        return {"deleted": iqn, "delete_file": delete_file}


@router.get("/lun", dependencies=[Depends(verify_client_cert)])
def list_luns():
    return backend.list_targets()


@router.get("/lun/status", dependencies=[Depends(verify_client_cert)])
def list_lun_status():
    """逐盘运行时状态（导出在线/凭据在位/活跃会话/盘占用），仅 nvmet 后端。"""
    from .nvmet import NvmetBackend
    if not isinstance(backend, NvmetBackend):
        raise HTTPException(400, "lun status requires backend=nvmet")
    return {"targets": backend.target_statuses()}


@router.get("/masters", dependencies=[Depends(verify_client_cert)])
def list_masters():
    """列出 DISK_DIR 下 *_tpl_* 母盘（后台线程周期扫描的缓存清单，供 Control Plane / WebUI 克隆选盘）。"""
    return {"masters": disks.masters.list()}


@router.get("/capabilities", dependencies=[Depends(verify_client_cert)])
def capabilities():
    caps = backend.capabilities()
    # 盘标识命名空间权威 = NQN（控制面建盘生成盘 NQN 依赖此键；iSCSI 数据面 IQN 由 NQN 派生）
    caps["base_nqn"] = NQN_BASE
    # 文件系统类型决定克隆方式：btrfs / ZFS(>=2.2 同数据集) / xfs(reflink 特性) 走 FICLONE 秒级，其余回退全量拷贝
    fs = disks.fs_type(DISK_DIR)
    caps["fs_type"] = fs
    if fs == "zfs":
        caps["clone"] = "reflink (FICLONE on OpenZFS >= 2.2, master and work disk in the same dataset) -> shutil.copy fallback"
    elif fs in ("btrfs", "xfs"):
        caps["clone"] = "reflink (FICLONE; xfs requires the reflink feature enabled) -> shutil.copy fallback"
    else:
        caps["clone"] = f"full copy only (reflink unsupported on {fs})"
    caps["empty_disk"] = "truncate (sparse)"
    return caps


@router.get("/logs", dependencies=[Depends(verify_client_cert)])
def get_logs(since: int = 0, limit: int = 1000):
    return oplog.read(since, limit)
