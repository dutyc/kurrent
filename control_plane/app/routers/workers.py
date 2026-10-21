"""Worker 管理端点（Bearer 鉴权）：CRUD / 批量 / 系统盘 / 默认启动 / MAC 换绑，及 Worker 台账投影。
含 NVMe-oF 凭据端点（/workers/{worker_id}/credential，DHHC-1 密钥按 Worker 跟盘）；
nvmet 节点上建系统盘即由控制面自动签发密钥（见 _issue_credential），手工端点用于轮换/吊销。"""

import base64
import copy
import hashlib
import hmac
import logging
import secrets
import threading
import time
import zlib
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..agent_client import AgentAPIError
from ..auth import authenticate, authorize_request
from ..config import settings
from ..placement import PlacementError, resolve as resolve_placement
from ..models import (
    BatchCreateWorkerDiskRequest,
    BatchCreateWorkersRequest,
    BatchDeleteWorkersRequest,
    BatchDiskTarget,
    CreateWorkerDiskRequest,
    CreateWorkerRequest,
    CredentialRequest,
    DiskPlacementRequest,
    SetWorkerDefaultBootRequest,
    UpdateWorkerMacRequest,
)
from ..stores import agents, credential_pushes, credentials, devices, dnsmasq, record, store
from ..utils import (
    add_worker_disk,
    build_disk_filename,
    build_iqn,
    build_nqn,
    canonical_hostname,
    canonical_id,
    canonical_mac,
    canonical_os,
    canonical_os_tag,
    canonical_os_version,
    client_host,
    disk_source,
    ensure_hostname_not_in_workers,
    find_disk_by_os,
    find_disk_by_os_version,
    find_disk_by_tag,
    gen_os_tag,
    now_iso,
    nqn_to_iqn,
    validate_disk,
    worker_disks,
)
from .devices import _bind_device, _ensure_device_poolable, _rollback_devices_binding, _unbind_worker_devices

log = logging.getLogger("control-plane")

# menu.ipxe 主菜单 item ID（choose --default 的合法值），作为 boot.menu_default 的严格校验集合
MENU_ITEMS = {
    "windows", "ubuntu", "debian", "centos", "esxi",
    "menu-diag", "menu-install", "config", "shell", "reboot", "exit",
}
# 菜单导航项（非 OS 语义）：boot.menu_default 归一逻辑按此集合区分——
# OS 语义默认值（默认盘存在或旧系统名）统一归一到唯一 OS 项 boot-os（2026-08-30 MAIN MENU 动态化）
MENU_NAV_ITEMS = {"menu-diag", "menu-install", "config", "shell", "reboot", "exit"}
# OS_ITEMS 已退役（2026-08-30 裁定）：os 为备注性质（人类理解用），唯一性由 os_tag 随机标识保证；
# 建盘不再白名单校验，os 合法集合不再存在

router = APIRouter(dependencies=[Depends(authenticate), Depends(authorize_request)])


@router.get("/workers")
def list_workers():
    data = store.load_workers()
    return [
        _enrich_worker(worker_id, record_)
        for worker_id, record_ in sorted(data["workers"].items())
    ]


@router.get("/workers/{worker_id}")
def get_worker(worker_id: str):
    worker_id = canonical_id(worker_id)
    data = store.load_workers()
    record_ = data["workers"].get(worker_id)
    if not record_:
        raise HTTPException(404, f"worker not found: {worker_id}")
    return _enrich_worker(worker_id, record_)


@router.get("/workers/{worker_id}/status")
def get_worker_status(worker_id: str):
    worker_id = canonical_id(worker_id)
    data = store.load_workers()
    record_ = data["workers"].get(worker_id)
    if not record_:
        raise HTTPException(404, f"worker not found: {worker_id}")
    return {
        "worker": _enrich_worker(worker_id, record_),
        "actual": _actual_state(record_),
    }


@router.post("/workers", status_code=201)
def create_worker(req: CreateWorkerRequest, request: Request):
    """注册 Worker 身份：写入台账 + hostname 绑定。
    mac 可选：不传 = 纯空转 Worker；传 = 校验设备在设备池中并直接绑定（一对一授权）。
    存储与身份分离：系统盘须另调 POST /workers/{worker_id}/luns/disk。"""
    worker_id = canonical_id(req.worker_id)
    hostname = canonical_hostname(req.hostname or worker_id)
    arch = req.arch or settings.default_arch
    mac = canonical_mac(req.mac) if req.mac else None

    cd_record: dict[str, Any] | None = None

    with store.locked():
        data = store.load_workers()
        workers = data["workers"]
        if worker_id in workers:
            raise HTTPException(409, f"worker already exists: {worker_id}")
        ensure_hostname_not_in_workers(workers, hostname)
        if mac:
            _ensure_device_poolable(mac)
            try:
                dnsmasq.ensure_free(mac, hostname)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

        client_ip = client_host(request)
        record("create_worker", "started", worker_id=worker_id, client=client_ip)

        try:
            if req.windows_iso:
                cd_agent, cd_caps = agents.select_cd_agent()
                # CD 盘无 nqn（nvmet 不支持 cd）：base_nqn 派生 IQN 后按历史契约生成盘 IQN
                cd_iqn = build_iqn(nqn_to_iqn(cd_caps["base_nqn"]), worker_id, "windows.iso")
                cd_client = agents.client(cd_agent)
                cd_result = cd_client.create_cd(req.windows_iso, cd_iqn)
                cd_record = {
                    "agent": cd_agent.id,
                    "iqn": cd_result.get("iqn", cd_iqn),
                    "iso": req.windows_iso,
                    "backing": cd_result.get("backing"),
                }
                record("agent.create_cd", "ok", worker_id=worker_id, agent=cd_agent.id, iqn=cd_record["iqn"])

            worker_record = {
                "hostname": hostname,
                "arch": arch,
                "state": "installing" if cd_record else "registered",
                "disks": [],
                "cd": cd_record,
            }
            if req.boot:
                worker_record["boot"] = req.boot.dict(exclude_none=True)
            workers[worker_id] = worker_record
            store.save_workers(data)
            record("workers.write", "ok", worker_id=worker_id)

            if mac:
                # 绑定设备（设备↔worker 一对一授权）：复用绑定核心流程
                bind_result = _bind_device(mac, worker_id, force=False)
                record("device.bind", "ok", mac=mac, worker_id=worker_id, force=False,
                       old_worker_id=bind_result.get("old_worker_id"),
                       old_device_mac=bind_result.get("old_device_mac"), client=client_ip)
            else:
                record("create_worker", "idle", worker_id=worker_id, hostname=hostname,
                       mac=None, client=client_ip)

            record("create_worker", "succeeded", worker_id=worker_id, client=client_ip)
            return _enrich_worker(worker_id, worker_record)
        except AgentAPIError as exc:
            record("create_worker", "failed", worker_id=worker_id, client=client_ip, error=exc.detail)
            _persist_failed_worker(data, worker_id, hostname, arch, None, cd_record, exc.detail)
            raise HTTPException(exc.status_code, {"agent": exc.agent_id, "error": exc.detail}) from exc
        except Exception as exc:
            record("create_worker", "failed", worker_id=worker_id, client=client_ip, error=str(exc))
            _persist_failed_worker(data, worker_id, hostname, arch, None, cd_record, str(exc))
            raise HTTPException(500, str(exc)) from exc


@router.post("/workers/batch")
def batch_create_workers(req: BatchCreateWorkersRequest, request: Request):
    """批量创建 Worker（逐项独立，幂等重跑）：命名规则 + 数量生成 worker_id（worker-01…），
    macs 与 count 等长时逐项绑定（设备须在池中，不传 = 全部纯空转）。
    已存在 → skipped；设备不可绑定 → failed 且该项不创建（可修正后重试）。不支持 windows_iso。"""
    prefix = req.name_prefix.strip()
    if not prefix:
        raise HTTPException(400, "name_prefix must not be empty")
    digits = max(2, len(str(req.count)))
    canonical_id(f"{prefix}{1:0{digits}d}")  # 命名规则预校验：生成的 worker_id 必须合法
    if req.macs is not None and len(req.macs) != req.count:
        raise HTTPException(400, f"macs length {len(req.macs)} must equal count {req.count}")

    succeeded: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    client_ip = client_host(request)
    record("create_worker.batch", "started", count=req.count, prefix=prefix, client=client_ip)

    with store.locked():
        data = store.load_workers()
        workers = data["workers"]
        for i in range(1, req.count + 1):
            worker_id = f"{prefix}{i:0{digits}d}"
            hostname = worker_id
            mac = canonical_mac(req.macs[i - 1]) if req.macs else None
            created = False
            try:
                if worker_id in workers:
                    skipped.append({"worker_id": worker_id, "reason": "already exists"})
                    continue
                ensure_hostname_not_in_workers(workers, hostname)
                if mac:
                    _ensure_device_poolable(mac)
                    try:
                        dnsmasq.ensure_free(mac, hostname)
                    except ValueError as exc:
                        raise HTTPException(409, str(exc)) from exc
                worker_record = {
                    "hostname": hostname,
                    "arch": req.arch or settings.default_arch,
                    "state": "registered",
                    "disks": [],
                    "cd": None,
                }
                if req.boot:
                    worker_record["boot"] = req.boot.dict(exclude_none=True)
                workers[worker_id] = worker_record
                store.save_workers(data)
                created = True
                record("workers.write", "ok", worker_id=worker_id)
                if mac:
                    bind_result = _bind_device(mac, worker_id, force=False)
                    record("device.bind", "ok", mac=mac, worker_id=worker_id, force=False,
                           old_worker_id=bind_result.get("old_worker_id"),
                           old_device_mac=bind_result.get("old_device_mac"), client=client_ip)
                succeeded.append({"worker_id": worker_id, "hostname": hostname, "mac": mac})
            except Exception as exc:
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                if created:
                    # 绑定阶段失败：回滚该项台账（dnsmasq 由 _bind_device 内部尽力恢复），可修正后重试
                    workers.pop(worker_id, None)
                    try:
                        store.save_workers(data)
                    except Exception:
                        log.exception("batch create rollback: workers save failed")
                    record("create_worker.batch", "rollback", worker_id=worker_id,
                           error=str(detail), client=client_ip)
                failed.append({
                    "worker_id": worker_id,
                    "hostname": hostname,
                    "mac": mac,
                    "error": str(detail),
                })

    record("create_worker.batch", "ok", created=len(succeeded), skipped=len(skipped),
           failed=len(failed), prefix=prefix, client=client_ip)
    return {"succeeded": succeeded, "skipped": skipped, "failed": failed}


@router.put("/workers/{worker_id}/mac")
def update_worker_mac(worker_id: str, req: UpdateWorkerMacRequest, request: Request):
    """修改 Worker 的 MAC 绑定（hostname 不变）：映射为设备换绑——
    新 MAC 须在设备池中（pooled），绑定新设备；旧设备解绑回池。审计记 device.bind + worker.mac.update（兼容）。"""
    worker_id = canonical_id(worker_id)
    new_mac = canonical_mac(req.mac)

    with store.locked():
        data = store.load_workers()
        record_ = data["workers"].get(worker_id)
        if not record_:
            raise HTTPException(404, f"worker not found: {worker_id}")
        hostname = record_["hostname"]
        client_ip = client_host(request)
        record("worker.mac.update", "started", worker_id=worker_id, client=client_ip)

        old_mac = dnsmasq.find_mac(hostname)
        if old_mac == new_mac:
            record("worker.mac.update", "ok", worker_id=worker_id, hostname=hostname,
                   old_mac=old_mac, new_mac=new_mac, changed=False, client=client_ip)
            return _enrich_worker(worker_id, record_)

        # 新绑定落盘 + 旧设备回池（一次原子写）：预校验——新 MAC 设备须在池中且未绑定
        old_released: str | None = None
        with devices.locked():
            devs_data = devices.load()
            devs = devs_data["devices"]
            new_dev = devs.get(new_mac)
            if not new_dev:
                raise HTTPException(409, f"device not in pool, register first: {new_mac}")
            new_state = new_dev.get("state")
            if new_state == "revoked":
                raise HTTPException(409, f"device revoked: {new_mac}")
            if new_state == "bound":
                raise HTTPException(409, f"device already bound to {new_dev.get('bound_worker_id')}: {new_mac}")
            old_dev = devs.get(old_mac) if old_mac else None
            if old_dev and old_dev.get("bound_worker_id") not in (None, worker_id):
                raise HTTPException(409, f"device {old_mac} bound to unexpected worker: "
                                    f"{old_dev.get('bound_worker_id')}")

            new_dev["state"] = "bound"
            new_dev["bound_worker_id"] = worker_id
            if old_dev and old_dev.get("state") == "bound" and old_dev.get("bound_worker_id") == worker_id:
                old_dev["state"] = "pooled"
                old_dev["bound_worker_id"] = None
                old_released = old_mac
            try:
                devices.save(devs_data)
            except Exception as exc:
                raise HTTPException(500, f"devices save failed: {exc}") from exc

        # dnsmasq 替换绑定（hostname 不变，仅换 MAC）
        try:
            replaced = dnsmasq.replace_binding(hostname, new_mac)
        except ValueError as exc:
            _rollback_devices_binding(new_mac, old_released, worker_id)
            record("worker.mac.update", "failed", worker_id=worker_id, client=client_ip, error=str(exc))
            raise HTTPException(409, str(exc)) from exc
        if replaced is None:
            _rollback_devices_binding(new_mac, old_released, worker_id)
            record("worker.mac.update", "failed", worker_id=worker_id, client=client_ip,
                   error=f"no dnsmasq binding for hostname: {hostname}")
            raise HTTPException(409, f"no dnsmasq binding for hostname: {hostname}")
        try:
            dnsmasq_result = dnsmasq.reload()
        except Exception as exc:
            record("worker.mac.update", "failed", worker_id=worker_id, client=client_ip,
                   old_mac=old_mac, new_mac=new_mac, error=f"dnsmasq reload failed: {exc}")
            raise HTTPException(500, f"dnsmasq reload failed: {exc}") from exc

        record("worker.mac.update", "ok", worker_id=worker_id, hostname=hostname,
               old_mac=old_mac, new_mac=new_mac, changed=True, client=client_ip)
        record("dnsmasq.reload", "ok", worker_id=worker_id, result=dnsmasq_result)
        if old_released:
            record("device.unbind", "ok", mac=old_released, worker_id=worker_id,
                   reason="worker.mac.update", client=client_ip)
        record("device.bind", "ok", mac=new_mac, worker_id=worker_id, force=True,
               old_worker_id=worker_id, old_device_mac=old_released, client=client_ip)
        _push_credentials(worker_id)  # 换绑：host NQN 随新设备 UUID 变化，重新推送
        return _enrich_worker(worker_id, record_)


@router.post("/workers/{worker_id}/luns/disk", status_code=201)
def create_worker_disk(worker_id: str, req: CreateWorkerDiskRequest, request: Request):
    """给指定 Worker 创建系统盘 LUN：传 master 走母盘克隆，传 size 建空白盘。
    os/os_version 为系统备注（人类理解用），同 (os, os_version) 至多一块；
    盘唯一标识 = os_tag（随机 12 hex），进盘 NQN/文件名（数据面）。"""
    worker_id = canonical_id(worker_id)
    os_name = canonical_os(req.os)
    os_version = canonical_os_version(req.os_version)
    validate_disk(req.type, req.name, req.size)

    with store.locked():
        data = store.load_workers()
        record_ = data["workers"].get(worker_id)
        if not record_:
            raise HTTPException(404, f"worker not found: {worker_id}")
        if find_disk_by_os_version(record_, os_name, os_version):
            label = f"{os_name} {os_version}".strip()
            raise HTTPException(409, f"worker already has a {label} system disk: {worker_id}")

        client_ip = client_host(request)
        record("worker.disk.create", "started", worker_id=worker_id, client=client_ip)

        try:
            if req.disk_agent:
                disk_agent = agents.get(req.disk_agent)
                if not disk_agent.role_disk:
                    raise HTTPException(400, f"agent {req.disk_agent} not configured for disk role")
                if not disk_agent.enabled:
                    # enabled = 排空语义（只拦新增，不碰存量：已有盘照常服务/引导）
                    raise HTTPException(409, f"agent {req.disk_agent} disabled: not accepting new disks")
                client = agents.client(disk_agent)
                try:
                    client.healthz()
                    disk_caps = client.capabilities()
                except Exception as exc:
                    raise HTTPException(503, f"agent {req.disk_agent} not reachable: {exc}") from exc
            else:
                # 未点名节点 → 交给放置策略（候选须启用、磁盘角色、数据面就绪，且母盘在该节点本地存在）
                try:
                    plan = resolve_placement(agents, [worker_id], disk_type=req.type, master=req.name)
                except PlacementError as exc:
                    raise HTTPException(503, {"error": exc.detail, "excluded": exc.excluded}) from exc
                chosen = plan.candidates[plan.assignments[worker_id]]
                disk_agent, disk_caps = chosen.agent, chosen.caps

            # 盘标识权威 = NQN（NVMe-oF 首选）：build_nqn 生成盘 NQN（后缀带 os_tag，
            # 同系统多版本/多盘靠随机标识区分），IQN 由其派生（不参与定义）
            os_tag = gen_os_tag()
            disk_nqn = build_nqn(disk_caps["base_nqn"], worker_id, f"{os_name}.{os_tag}")
            disk_iqn = nqn_to_iqn(disk_nqn)
            disk_filename = build_disk_filename(worker_id, os_name, os_tag)
            disk_client = agents.client(disk_agent)
            if req.type == "master":
                disk_result = disk_client.create_disk(disk_iqn, disk_filename, master=req.name)
            else:
                disk_result = disk_client.create_disk(disk_iqn, disk_filename, size=req.size)
            disk_record = {
                "agent": disk_agent.id,
                "nqn": disk_nqn,                              # NVMe-oF 数据面标识（权威）
                "iqn": nqn_to_iqn(disk_nqn),                  # iSCSI 数据面标识（由 NQN 派生）
                "filename": disk_filename,
                "backing": disk_result.get("backing"),
                "os": os_name,                                # 系统备注（人类理解用）
                "os_version": os_version,                     # 版本备注（'' = 无版本）
                "os_tag": os_tag,                             # 盘级随机标识（数据面唯一键）
                "remark": req.remark,                          # 盘备注（自由文本，可空）
                "source": disk_source(req.type, req.name, req.size),
            }
            record("agent.create_disk", "ok", worker_id=worker_id, agent=disk_agent.id, iqn=disk_record["iqn"])

            add_worker_disk(record_, disk_record)
            if record_.get("state") == "registered":
                record_["state"] = "ready"
            store.save_workers(data)
            record("workers.disk.write", "ok", worker_id=worker_id, iqn=disk_record["iqn"])

            record("worker.disk.create", "succeeded", worker_id=worker_id, client=client_ip)
            # 建盘即签发：nvmet 子系统与其认证密钥同时就位（已有密钥保留，不轮换）
            _issue_credential(worker_id, disk_caps.get("backend", ""))
            _push_credentials(worker_id)  # 新子系统须登记 hosts（有密钥即注入）
            return _enrich_worker(worker_id, record_)
        except HTTPException:
            raise  # 校验类错误（agent 角色/排空 4xx）原样透出，不落 500
        except AgentAPIError as exc:
            record("worker.disk.create", "failed", worker_id=worker_id, client=client_ip, error=exc.detail)
            raise HTTPException(exc.status_code, {"agent": exc.agent_id, "error": exc.detail}) from exc
        except Exception as exc:
            record("worker.disk.create", "failed", worker_id=worker_id, client=client_ip, error=str(exc))
            raise HTTPException(500, str(exc)) from exc


def _resolve_batch_targets(
    req: BatchCreateWorkerDiskRequest,
) -> tuple[list[BatchDiskTarget], list[dict[str, str]], dict[str, tuple[Any, dict[str, Any]]]]:
    """把请求解析成逐项 (worker_id, agent)。返回 (targets, warnings, 已校验 agent 缓存)。

    targets 与 workers 是两种定位方式，二选一：targets 逐项点名（显式覆盖）；
    workers 交给放置策略展开。dry_run 与执行都走这里，保证「预览即执行」。
    """
    if req.targets and req.workers:
        raise HTTPException(400, "specify either targets or workers, not both")
    if req.targets:
        return list(req.targets), [], {}
    if not req.workers:
        raise HTTPException(400, "workers or targets must not be empty")

    placement_req = req.placement or DiskPlacementRequest()
    try:
        plan = resolve_placement(
            agents,
            req.workers,
            disk_type=req.type,
            master=req.name,
            candidates=placement_req.candidates or None,
            pins=placement_req.pins,
        )
    except PlacementError as exc:
        raise HTTPException(503, {"error": exc.detail, "excluded": exc.excluded}) from exc

    targets = [BatchDiskTarget(worker_id=w, agent=plan.assignments[w]) for w in req.workers]
    validated = {agent_id: (c.agent, c.caps) for agent_id, c in plan.candidates.items()}
    return targets, plan.warnings, validated


def _preview_batch_disks(
    targets: list[BatchDiskTarget],
    os_name: str,
    os_version: str,
    warnings: list[dict[str, str]],
) -> dict[str, Any]:
    """dry_run 计划：逐项给出将落到哪个节点、以及是建盘还是跳过（与执行同一套判定）。"""
    data = store.load_workers()
    label = f"{os_name} {os_version}".strip()
    plan: list[dict[str, Any]] = []
    for target in targets:
        worker_id = canonical_id(target.worker_id)
        record_ = data["workers"].get(worker_id)
        if not record_:
            plan.append({"worker_id": worker_id, "agent": target.agent,
                         "action": "error", "reason": "worker not found"})
        elif find_disk_by_os_version(record_, os_name, os_version):
            plan.append({"worker_id": worker_id, "agent": target.agent,
                         "action": "skip", "reason": f"already has a {label} system disk"})
        else:
            plan.append({"worker_id": worker_id, "agent": target.agent, "action": "create"})
    return {"dry_run": True, "plan": plan, "warnings": warnings}


@router.post("/workers/luns/disk/batch")
def batch_create_worker_disks(req: BatchCreateWorkerDiskRequest, request: Request):
    """批量给多个 Worker 创建系统盘。定位方式二选一：targets 逐项点名存储节点，
    或 workers + placement 由控制面放置（候选筛选含母盘本地存在性，按现有盘数最少均摊；
    pins 逐项钉死）。dry_run=true 只返回计划不建盘。
    与单盘一致：master 走母盘克隆，empty 建空白盘；同一 (os, os_version) 至多一块，已存在则自动跳过（不算失败）。
    创建成功的 Worker 自动将 default_disk 设为本次新建盘（批量部署直接进入默认启动）。
    逐项独立执行，单项失败不影响其余；返回 succeeded / skipped / failed 汇总。"""
    os_name = canonical_os(req.os)
    os_version = canonical_os_version(req.os_version)
    validate_disk(req.type, req.name, req.size)
    targets, warnings, validated = _resolve_batch_targets(req)
    if req.dry_run:
        return _preview_batch_disks(targets, os_name, os_version, warnings)

    client_ip = client_host(request)
    succeeded: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    with store.locked():
        data = store.load_workers()
        for target in targets:
            worker_id = canonical_id(target.worker_id)
            record_ = data["workers"].get(worker_id)
            if not record_:
                failed.append({"worker_id": worker_id, "error": f"worker not found: {worker_id}"})
                continue
            if find_disk_by_os_version(record_, os_name, os_version):
                label = f"{os_name} {os_version}".strip()
                skipped.append({"worker_id": worker_id, "reason": f"already has a {label} system disk"})
                continue

            record("worker.disk.create", "started", worker_id=worker_id, client=client_ip)
            try:
                if target.agent in validated:
                    # 放置阶段已 healthz/capabilities：同一请求内复用，免去逐项重复探测
                    disk_agent, disk_caps = validated[target.agent]
                else:
                    try:
                        disk_agent = agents.get(target.agent)
                    except KeyError:
                        # agents.get 缺失抛 KeyError（不返回 None）：显式转清晰 404，避免落成 500 "'xxx'"
                        raise HTTPException(404, f"agent not found: {target.agent}") from None
                    if not disk_agent.role_disk:
                        raise HTTPException(400, f"agent {target.agent} not configured for disk role")
                    if not disk_agent.enabled:
                        # enabled = 排空语义（只拦新增，不碰存量）
                        raise HTTPException(409, f"agent {target.agent} disabled: not accepting new disks")
                    client = agents.client(disk_agent)
                    try:
                        client.healthz()
                        disk_caps = client.capabilities()
                    except Exception as exc:
                        raise HTTPException(503, f"agent {target.agent} not reachable: {exc}") from exc
                    validated[target.agent] = (disk_agent, disk_caps)

                # 盘标识权威 = NQN（NVMe-oF 首选）：build_nqn 生成盘 NQN（后缀带 os_tag），IQN 由其派生（不参与定义）
                os_tag = gen_os_tag()
                disk_nqn = build_nqn(disk_caps["base_nqn"], worker_id, f"{os_name}.{os_tag}")
                disk_iqn = nqn_to_iqn(disk_nqn)
                disk_filename = build_disk_filename(worker_id, os_name, os_tag)
                disk_client = agents.client(disk_agent)
                if req.type == "master":
                    disk_result = disk_client.create_disk(disk_iqn, disk_filename, master=req.name)
                else:
                    disk_result = disk_client.create_disk(disk_iqn, disk_filename, size=req.size)
                disk_record = {
                    "agent": disk_agent.id,
                    "nqn": disk_nqn,                              # NVMe-oF 数据面标识（权威）
                    "iqn": nqn_to_iqn(disk_nqn),                  # iSCSI 数据面标识（由 NQN 派生）
                    "filename": disk_filename,
                    "backing": disk_result.get("backing"),
                    "os": os_name,                                # 系统备注（人类理解用）
                    "os_version": os_version,                     # 版本备注（'' = 无版本）
                    "os_tag": os_tag,                             # 盘级随机标识（数据面唯一键）
                    "remark": req.remark,                          # 盘备注（自由文本，可空）
                    "source": disk_source(req.type, req.name, req.size),
                }
                record("agent.create_disk", "ok", worker_id=worker_id, agent=disk_agent.id, iqn=disk_record["iqn"])

                add_worker_disk(record_, disk_record)
                if record_.get("state") == "registered":
                    record_["state"] = "ready"
                # 批量部署约定：创建成功即设为默认启动盘（单盘接口不自动设置）
                record_["default_disk"] = os_tag
                store.save_workers(data)
                record("workers.disk.write", "ok", worker_id=worker_id, iqn=disk_record["iqn"])
                record(
                    "worker.boot.set",
                    "ok",
                    worker_id=worker_id,
                    client=client_ip,
                    changes=f"default_disk:{os_tag}",
                )
                record("worker.disk.create", "succeeded", worker_id=worker_id, client=client_ip)
                succeeded.append({"worker_id": worker_id, "agent": disk_agent.id, "iqn": disk_record["iqn"]})
                # 建盘即签发：批量 N 台 = N 份密钥一次到位，无需逐台手工设置
                _issue_credential(worker_id, disk_caps.get("backend", ""))
                _push_credentials(worker_id)  # 批量建盘后同步 hosts 登记
            except Exception as exc:
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                if isinstance(detail, dict):
                    detail = detail.get("error") or str(detail)
                record("worker.disk.create", "failed", worker_id=worker_id, client=client_ip, error=str(detail))
                failed.append({"worker_id": worker_id, "agent": target.agent, "error": str(detail)})

    return {"succeeded": succeeded, "skipped": skipped, "failed": failed}


@router.delete("/workers/{worker_id}/luns/disk/{os_tag}")
def delete_worker_disk(
    worker_id: str,
    os_tag: str,
    request: Request,
    delete_file: bool = Query(False, description="Delete the disk backing .img as well."),
    ignore_missing_target: bool = Query(False, description="Ignore 404 from Agent while deleting the target."),
):
    """删除 Worker 的单个系统盘（按盘标识 os_tag 定位）：可保留或删除 .img 文件；
    被删盘若是默认启动，联动清除 default_disk 与同名 menu_default；无盘时状态回退 registered。"""
    worker_id = canonical_id(worker_id)
    os_tag = canonical_os_tag(os_tag)
    with store.locked():
        data = store.load_workers()
        record_ = data["workers"].get(worker_id)
        if not record_:
            raise HTTPException(404, f"worker not found: {worker_id}")
        disk = find_disk_by_tag(record_, os_tag)
        if not disk:
            raise HTTPException(404, f"worker {worker_id} has no system disk with os_tag {os_tag}")
        os_name = str(disk.get("os", ""))

        client_ip = client_host(request)
        record("worker.disk.delete", "started", worker_id=worker_id, client=client_ip, os=os_name, os_tag=os_tag)

        try:
            _delete_target(disk, delete_file=delete_file, ignore_missing=ignore_missing_target)
            record("agent.delete_disk", "ok", worker_id=worker_id, agent=disk["agent"],
                   iqn=disk["iqn"], delete_file=delete_file)

            disks = record_.get("disks")
            if disks is not None:
                disks[:] = [d for d in disks if d is not disk]
            else:
                record_.pop("disk", None)  # 旧台账单盘字段

            # 联动：被删盘正是默认启动盘时清除 default_disk 与同名 menu_default
            if str(record_.get("default_disk", "")).lower() == os_tag:
                record_.pop("default_disk", None)
            boot = record_.get("boot") or {}
            if os_name and str(boot.get("menu_default", "")).lower() == os_name:
                boot.pop("menu_default", None)
                if not boot:
                    record_.pop("boot", None)

            # 无盘时状态回退 registered，等待重新建盘
            if not worker_disks(record_) and record_.get("state") == "ready":
                record_["state"] = "registered"

            store.save_workers(data)
            record("workers.disk.delete", "ok", worker_id=worker_id, os=os_name, os_tag=os_tag)
            record("worker.disk.delete", "succeeded", worker_id=worker_id, client=client_ip)
            _push_credentials(worker_id)  # 盘已删：同步移除 Agent 侧 hosts 登记
            return _enrich_worker(worker_id, record_)
        except AgentAPIError as exc:
            record("worker.disk.delete", "failed", worker_id=worker_id, client=client_ip, error=exc.detail)
            raise HTTPException(exc.status_code, {"agent": exc.agent_id, "error": exc.detail}) from exc
        except Exception as exc:
            record("worker.disk.delete", "failed", worker_id=worker_id, client=client_ip, error=str(exc))
            raise HTTPException(500, str(exc)) from exc


@router.put("/workers/{worker_id}/default-disk")
def set_worker_default_boot(worker_id: str, req: SetWorkerDefaultBootRequest, request: Request):
    """设置 Worker 默认启动配置：disk=默认启动盘（os_tag，须为已挂系统盘）；
    menu_default/menu_timeout=菜单项覆盖；传 null 清除对应项。
    推导链：default_disk（指向具体盘）> boot.menu_default > reboot。"""
    worker_id = canonical_id(worker_id)
    fields = req.model_fields_set
    if not (fields & {"disk", "menu_default", "menu_timeout"}):
        raise HTTPException(400, "need at least one of disk / menu_default / menu_timeout")

    with store.locked():
        data = store.load_workers()
        record_ = data["workers"].get(worker_id)
        if not record_:
            raise HTTPException(404, f"worker not found: {worker_id}")

        # 先全部校验，再统一应用，保证原子性
        new_default_disk: str | None = None
        clear_default_disk = False
        if "disk" in fields:
            if req.disk is None or req.disk == "":
                clear_default_disk = True
            else:
                os_tag = canonical_os_tag(req.disk)
                disk = find_disk_by_tag(record_, os_tag)
                if not disk:
                    existing = ", ".join(d.get("os_tag", "?") for d in worker_disks(record_)) or "none"
                    raise HTTPException(
                        400,
                        f"worker has no system disk with os_tag {os_tag} (worker disks: {existing})",
                    )
                new_default_disk = os_tag

        new_menu_default: str | None = None
        clear_menu_default = False
        if "menu_default" in fields:
            if req.menu_default is None or req.menu_default == "":
                clear_menu_default = True
            else:
                menu_default = req.menu_default.strip().lower()
                if menu_default not in MENU_ITEMS:
                    raise HTTPException(
                        400, f"menu_default must be one of {sorted(MENU_ITEMS)}: {req.menu_default}"
                    )
                new_menu_default = menu_default

        new_menu_timeout: int | None = None
        clear_menu_timeout = False
        if "menu_timeout" in fields:
            if req.menu_timeout is None:
                clear_menu_timeout = True
            elif req.menu_timeout < 0:
                raise HTTPException(400, f"menu_timeout must be >= 0: {req.menu_timeout}")
            else:
                new_menu_timeout = req.menu_timeout

        changes: list[str] = []
        if clear_default_disk:
            record_.pop("default_disk", None)
            changes.append("default_disk:cleared")
        elif new_default_disk:
            record_["default_disk"] = new_default_disk
            changes.append(f"default_disk:{new_default_disk}")

        boot = record_.setdefault("boot", {})
        if clear_menu_default:
            boot.pop("menu_default", None)
            changes.append("menu_default:cleared")
        elif new_menu_default:
            boot["menu_default"] = new_menu_default
            changes.append(f"menu_default:{new_menu_default}")
        if clear_menu_timeout:
            boot.pop("menu_timeout", None)
            changes.append("menu_timeout:cleared")
        elif new_menu_timeout is not None:
            boot["menu_timeout"] = new_menu_timeout
            changes.append(f"menu_timeout:{new_menu_timeout}")
        if not boot:
            record_.pop("boot", None)

        store.save_workers(data)
        record(
            "worker.boot.set",
            "ok",
            worker_id=worker_id,
            client=client_host(request),
            changes=",".join(changes),
        )
        return _enrich_worker(worker_id, record_)


@router.delete("/workers/{worker_id}")
def delete_worker(
    worker_id: str,
    request: Request,
    delete_disk: bool = Query(False, description="Delete the disk backing .img as well."),
    ignore_missing_target: bool = Query(False, description="Ignore 404 from Agent while deleting LUNs."),
):
    worker_id = canonical_id(worker_id)
    with store.locked():
        data = store.load_workers()
        record_ = data["workers"].get(worker_id)
        if not record_:
            raise HTTPException(404, f"worker not found: {worker_id}")

        client_ip = client_host(request)
        record("delete_worker", "started", worker_id=worker_id, client=client_ip, delete_disk=delete_disk)

        # 吊销凭据推送（删盘前）：Agent 移除该 worker 的 hosts 登记并清缓存
        _push_credentials(worker_id, secret=None)

        # 联动解绑设备（设备回池，不吊销）：先解绑落盘，再删 worker（解绑失败则中止删除）
        unbound: list[str] = []
        with devices.locked():
            devs_data = devices.load()
            unbound = _unbind_worker_devices(devs_data, worker_id)
            if unbound:
                devices.save(devs_data)

        try:
            if record_.get("cd"):
                _delete_target(record_["cd"], delete_file=False, ignore_missing=ignore_missing_target)
                record("agent.delete_cd", "ok", worker_id=worker_id, agent=record_["cd"]["agent"], iqn=record_["cd"]["iqn"])
            for disk in worker_disks(record_):
                _delete_target(disk, delete_file=delete_disk, ignore_missing=ignore_missing_target)
                record("agent.delete_disk", "ok", worker_id=worker_id, agent=disk["agent"], iqn=disk["iqn"], delete_file=delete_disk)

            hostname = record_["hostname"]
            del data["workers"][worker_id]
            store.save_workers(data)
            record("workers.delete", "ok", worker_id=worker_id)
            # worker 已消失 → 其凭据条目与推送台账一并作废（避免孤儿密钥被同 id 重建继承）
            _forget_worker_credentials(worker_id)
            for mac in unbound:
                record("device.unbind", "ok", mac=mac, worker_id=worker_id,
                       reason="worker.delete", client=client_ip)

            removed = dnsmasq.remove_hostname(hostname)
            record("dnsmasq.hosts.delete", "ok", worker_id=worker_id, hostname=hostname, removed=removed)
            dnsmasq_result = dnsmasq.reload()
            record("dnsmasq.reload", "ok", worker_id=worker_id, result=dnsmasq_result)

            record("delete_worker", "succeeded", worker_id=worker_id, client=client_ip)
            return {"deleted": worker_id, "delete_disk": delete_disk, "dnsmasq_removed": removed}
        except AgentAPIError as exc:
            record("delete_worker", "failed", worker_id=worker_id, client=client_ip, error=exc.detail)
            raise HTTPException(exc.status_code, {"agent": exc.agent_id, "error": exc.detail}) from exc
        except Exception as exc:
            record("delete_worker", "failed", worker_id=worker_id, client=client_ip, error=str(exc))
            raise HTTPException(500, str(exc)) from exc


@router.post("/workers/delete/batch")
def batch_delete_workers(req: BatchDeleteWorkersRequest, request: Request):
    """批量删除 Worker：逐项独立执行（单项失败不影响其余），返回 succeeded / failed 汇总。
    每项：删 CD/系统盘 target（delete_disk 控制是否连 .img）、移台账、移除 dnsmasq 绑定；
    全部成功项统一保存台账、统一 reload 一次 dnsmasq。"""
    if not req.worker_ids:
        raise HTTPException(400, "worker_ids must not be empty")

    client_ip = client_host(request)
    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    with store.locked():
        data = store.load_workers()
        removed_hostnames: list[str] = []
        unbound_devices: list[str] = []
        for raw_id in req.worker_ids:
            worker_id = canonical_id(raw_id)
            record_ = data["workers"].get(worker_id)
            if not record_:
                failed.append({"worker_id": worker_id, "error": f"worker not found: {worker_id}"})
                continue

            record("delete_worker", "started", worker_id=worker_id, client=client_ip,
                   delete_disk=req.delete_disk)
            # 吊销凭据推送（删盘前）：Agent 移除该 worker 的 hosts 登记并清缓存
            _push_credentials(worker_id, secret=None)
            try:
                if record_.get("cd"):
                    _delete_target(record_["cd"], delete_file=False, ignore_missing=req.ignore_missing_target)
                    record("agent.delete_cd", "ok", worker_id=worker_id,
                           agent=record_["cd"]["agent"], iqn=record_["cd"]["iqn"])
                for disk in worker_disks(record_):
                    _delete_target(disk, delete_file=req.delete_disk, ignore_missing=req.ignore_missing_target)
                    record("agent.delete_disk", "ok", worker_id=worker_id,
                           agent=disk["agent"], iqn=disk["iqn"], delete_file=req.delete_disk)

                hostname = record_["hostname"]
                # 联动解绑设备（设备回池，不吊销）：解绑失败则该 worker 不删除
                with devices.locked():
                    devs_data = devices.load()
                    unbound = _unbind_worker_devices(devs_data, worker_id)
                    if unbound:
                        devices.save(devs_data)
                        unbound_devices.extend(unbound)
                del data["workers"][worker_id]
                record("workers.delete", "ok", worker_id=worker_id)
                # worker 已消失 → 其凭据条目与推送台账一并作废（避免孤儿密钥被同 id 重建继承）
                _forget_worker_credentials(worker_id)
                for mac in unbound:
                    record("device.unbind", "ok", mac=mac, worker_id=worker_id,
                           reason="worker.delete", client=client_ip)
                removed_hostnames.append(hostname)
                record("delete_worker", "succeeded", worker_id=worker_id, client=client_ip)
                succeeded.append({"worker_id": worker_id, "hostname": hostname})
            except Exception as exc:
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                if isinstance(detail, dict):
                    detail = detail.get("error") or str(detail)
                record("delete_worker", "failed", worker_id=worker_id,
                       client=client_ip, error=str(detail))
                failed.append({"worker_id": worker_id, "error": str(detail)})

        if succeeded:
            store.save_workers(data)
            for hostname in removed_hostnames:
                removed = dnsmasq.remove_hostname(hostname)
                record("dnsmasq.hosts.delete", "ok", hostname=hostname, removed=removed)
            dnsmasq_result = dnsmasq.reload()
            record("dnsmasq.reload", "ok", batch=len(removed_hostnames), result=dnsmasq_result)

    return {"succeeded": succeeded, "failed": failed}


def _delete_target(target_record: dict[str, Any], *, delete_file: bool, ignore_missing: bool) -> None:
    try:
        agent = agents.get(target_record["agent"])
    except KeyError:
        # 登记缺失（删除语义 = 仅清控制面记录）：该节点上的 target 无从删除——
        # ignore_missing 视为已缺失；否则走 AgentAPIError 通道报清晰 404（不再 KeyError 500）
        if ignore_missing:
            return
        raise AgentAPIError(target_record["agent"], 404,
                            f"agent not registered: {target_record['agent']}")
    try:
        agents.client(agent).delete_lun(target_record["iqn"], delete_file=delete_file)
    except AgentAPIError as exc:
        if ignore_missing and exc.status_code == 404:
            return
        raise


def _actual_state(record_: dict[str, Any]) -> dict[str, Any]:
    actual: dict[str, Any] = {
        "dnsmasq": {
            "hostname": record_["hostname"],
            "mac": dnsmasq.find_mac(record_["hostname"]),
        }
    }
    actual["disks"] = [_target_actual(d, os=d.get("os")) for d in worker_disks(record_)]
    actual["cd"] = _target_actual(record_.get("cd"))
    return actual


def _target_actual(target_record: dict[str, Any] | None, os: str | None = None) -> dict[str, Any] | None:
    if not target_record:
        return None
    try:
        agent = agents.get(target_record["agent"])
        targets = agents.client(agent).list_luns()
        # 标识集合对比（兼容双数据面）：nvmet 的 target 标识是 NQN，stgt/lio 是 IQN
        record_ids = {target_record["iqn"], target_record.get("nqn")} - {None}
        found = next((t for t in targets if (t.get("nqn") or t.get("iqn")) in record_ids), None)
        return {"os": os, "exists": found is not None, "target": found}
    except Exception as exc:
        return {"os": os, "exists": False, "error": str(exc)}


def _enrich_worker(worker_id: str, record_: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(record_)
    item["worker_id"] = worker_id
    item["mac"] = dnsmasq.find_mac(item["hostname"])
    bound_device = _bound_device_mac_for(worker_id)
    item["bound_device"] = bound_device
    item["readiness"] = _readiness_for(bound_device, bool(worker_disks(record_)))
    return item


def _readiness_for(bound_device: str | None, has_disk: bool) -> str:
    """就绪度派生：绑定+有盘 → ready；绑定或有盘 → partial；皆无 → idle。"""
    if bound_device and has_disk:
        return "ready"
    if bound_device or has_disk:
        return "partial"
    return "idle"


def _bound_device_mac_for(worker_id: str) -> str | None:
    """反查绑定到该 worker 的设备 mac（设备台账权威面）。"""
    data = devices.load()
    for mac, dev in data["devices"].items():
        if dev.get("bound_worker_id") == worker_id:
            return mac
    return None


def _persist_failed_worker(
    data: dict[str, Any],
    worker_id: str,
    hostname: str,
    arch: str,
    disk_record: dict[str, Any] | None,
    cd_record: dict[str, Any] | None,
    error: str,
) -> None:
    if not disk_record and not cd_record:
        return
    data["workers"][worker_id] = {
        "hostname": hostname,
        "arch": arch,
        "state": "failed",
        "error": error,
        "disks": [disk_record] if disk_record else [],
        "cd": cd_record,
    }
    try:
        store.save_workers(data)
    except Exception:
        log.exception("failed to persist failed worker state")


# ============================ NVMe-oF 凭据（DHHC-1，按 Worker 跟盘） ============================
# 签发时机：nvmet 节点上建系统盘即自动签发（`_issue_credential`），密钥随子系统就位；
# 手工端点（PUT/DELETE /credential）只用于显式轮换或吊销——吊销是明确的「不用认证」，
# 不会被后续建盘复活（只有无条目才签发，吊销后条目已删）。

# Host NQN 与盘 NQN 同域：base 取盘 NQN 前缀（盘 NQN 由 build_nqn 按 Agent 上报的
# capabilities.base_nqn 生成——节点侧 spec.agent.nqnBase 权威，控制面不声明）


def _base_nqn_from_target(nqn: str | None) -> str | None:
    """盘 NQN 的命名空间前缀：`nqn.2026-07.com.kurrent:worker-01.ubuntu.<os_tag>` → 前缀。
    盘 NQN 由控制面按统一模板 `base:worker_id.os.<os_tag>` 生成（无存量盘），前缀重拼 = 盘记录权威值；
    盘记录缺 nqn → 返回 None（不派生，不兼容遗留）。"""
    if nqn and ":" in nqn:
        return nqn.rsplit(":", 1)[0]
    return None


def _host_nqn_for(base_nqn: str, worker_id: str) -> str:
    """Host NQN 派生（发起端身份，host. 前缀区分角色）：与盘 NQN 同域并立。"""
    return f"{base_nqn}:host.{worker_id}"


_UNSET = object()

# 推送结果三值：ok=已送达（Agent 缓存与宿主服务均已同步）；failed=未送达（Agent 不可达 /
# 宿主同步失败），对账会持续重放；n/a=目标 Agent 非 nvmet 后端（凭据不适用，非故障）。
PUSH_OK = "ok"
PUSH_FAILED = "failed"
PUSH_NA = "n/a"


def _credential_targets(record_: dict) -> dict[str, list[str]]:
    """worker 的推送目标：agent_id → 该 Agent 上本 worker 的盘 NQN 列表。

    NVMe-oF 子系统标识 = NQN（盘记录权威字段）；缺 nqn（存量盘）跳过该盘，不派生
    （不兼容遗留）：该盘不支持 NVMe-oF 凭据/引导。"""
    groups: dict[str, list[str]] = {}
    for disk in worker_disks(record_):
        if disk.get("nqn"):
            groups.setdefault(disk["agent"], []).append(disk["nqn"])
    return groups


def _registered_agent_ids() -> set[str]:
    return {agent.id for agent in agents.load()}


def _sync_targets(record_: dict, registered: set[str] | None = None) -> dict[str, list[str]]:
    """有同步语义的推送目标：`_credential_targets` ∩ 已登记 Agent。

    未登记的 Agent（登记被删 / 尚未 enroll）无从投递——对账据此剪除记录，投影也须据此
    排除，否则会显示永不收敛的「待推送」（对账只服务已登记 Agent，永远不会消费它）。
    批量调用（对账）可传入预取的 registered 以避免逐 worker 重读登记表。"""
    if registered is None:
        registered = _registered_agent_ids()
    return {agent_id: nqns for agent_id, nqns in _credential_targets(record_).items()
            if agent_id in registered}


def _forget_worker_credentials(worker_id: str) -> None:
    """删除 Worker 时清空它在凭据域的全部痕迹：密钥条目 + 该 worker 的推送台账。

    不清理会留孤儿——条目对已不存在的 worker 既不可见（`GET /credential` 404）也删不掉；
    同 id 的 worker 若日后被建出来，还会静默继承旧密钥并被推给新机器的盘。"""
    with credentials.locked():
        creds = credentials.load()
        if creds["credentials"].pop(worker_id, None) is not None:
            credentials.save(creds)
            record("credential.forget", "ok", worker_id=worker_id, reason="worker.delete")
    prefix = f"{worker_id}/"  # 推送台账键 = "<worker_id>/<agent_id>"（worker_id 不含 "/"）
    with credential_pushes.locked():
        data = credential_pushes.load()
        stale = [key for key in data["pushes"] if key.startswith(prefix)]
        if stale:
            for key in stale:
                data["pushes"].pop(key)
            credential_pushes.save(data)


def _push_state_key(worker_id: str, agent_id: str) -> str:
    return f"{worker_id}/{agent_id}"


def _set_push_state(worker_id: str, agent_id: str, result: str,
                    secret: str | None, error: str = "") -> None:
    """回写推送状态记录（secret_hash 记期望密钥审计哈希，"" = 期望吊销）。"""
    entry: dict[str, Any] = {
        "worker_id": worker_id,
        "agent_id": agent_id,
        "secret_hash": _secret_hash(secret) if secret else "",
        "result": result,
        "updated_at": now_iso(),
    }
    if error:
        entry["error"] = error
    with credential_pushes.locked():
        data = credential_pushes.load()
        data["pushes"][_push_state_key(worker_id, agent_id)] = entry
        credential_pushes.save(data)


def _push_one(worker_id: str, agent_id: str, sub_nqns: list[str], secret: str | None,
              *, audit_always: bool) -> str:
    """推送单个 (worker, agent) 对并回写状态记录；返回 ok|failed|n/a|skipped。

    审计：用户动作（audit_always=True）每次记录；对账重放仅在结果或期望密钥变化时
    记录（周期重放不刷屏）。Agent 登记缺失（登记删除语义 = 仅清控制面记录）跳过且
    不留状态——登记恢复后由对账补推；非 nvmet 后端记 n/a（凭据不适用，不计故障）。"""
    try:
        agent = agents.get(agent_id)
    except KeyError:
        record("credential.push", "skipped", worker_id=worker_id, agent=agent_id,
               reason="agent not registered")
        return "skipped"
    with credential_pushes.locked():
        prev = credential_pushes.load()["pushes"].get(_push_state_key(worker_id, agent_id), {})
    # Host NQN base：盘 NQN 前缀（与 base_nqn 投影同机制）——同一 worker 持多盘
    # 时各盘同 agent 同域，base 一致；缺 nqn 的盘已跳过，此处恒有值
    host_nqns = [_host_nqn_for(_base_nqn_from_target(sub_nqns[0]), worker_id)]
    # TLS-PSK 随认证密钥派生（_derive_tls_psk）；secret=None（吊销）→ 同步吊销目标端 PSK
    tls_psk = _derive_tls_psk(secret) if secret else None
    try:
        agents.client(agent).set_credential(worker_id, secret, sub_nqns, host_nqns,
                                            tls_psk=tls_psk)
        result, error = PUSH_OK, ""
    except AgentAPIError as exc:
        if exc.status_code == 400 and "backend=nvmet" in exc.detail:
            result, error = PUSH_NA, ""
        else:
            result, error = PUSH_FAILED, exc.detail
    except Exception as exc:
        result, error = PUSH_FAILED, str(exc)
    _set_push_state(worker_id, agent_id, result, secret, error)
    if result == PUSH_FAILED:
        log.warning(f"credential push failed: worker={worker_id} agent={agent_id}: {error}")
    desired_hash = _secret_hash(secret) if secret else ""
    if audit_always or prev.get("result") != result or prev.get("secret_hash", "") != desired_hash:
        extra: dict[str, Any] = {"worker_id": worker_id, "agent_id": agent_id,
                                 "injected": bool(secret), "sub_count": len(sub_nqns),
                                 "host_count": len(host_nqns)}
        status = result
        if result == PUSH_NA:
            status, extra["reason"] = "skipped", "backend not nvmet"
        if error:
            extra["error"] = error
        record("credential.push", status, **extra)
    return result


def _push_state(worker_id: str, agent_id: str) -> dict:
    with credential_pushes.locked():
        return credential_pushes.load()["pushes"].get(_push_state_key(worker_id, agent_id), {})


def _no_sync_needed(prev: dict, secret: str | None) -> bool:
    """无密钥且该对从无推送历史 → 无需同步。

    nvmet 建盘已自动签发密钥，此处无密钥只剩两类：盘在非 nvmet 后端（DHHC-1 不适用，
    推送只会得 n/a），或密钥已被显式吊销（有历史记录，不走本判定）。这两类都不该产生
    新推送记录，否则 agent 离线时会显示「同步失败 / 待同步」的虚假故障。"""
    return not prev and not secret


def _issue_credential(worker_id: str, backend: str) -> bool:
    """为 nvmet 节点上的新子系统签发认证密钥：无条目则生成，已有条目原样保留。

    建盘即签发——盘一旦在 nvmet 上有了子系统，认证密钥就是它的配套凭据，由系统签发
    而非逐个手工设置（批量建盘 N 台即 N 份密钥）。已有条目视为已签发：按 Worker 跟盘，
    同一 worker 的多盘共用一把密钥，建盘不触发轮换。非 nvmet 后端不签发——DHHC-1 是
    NVMe-oF 专属（iSCSI 走 CHAP，不在本链路）。返回是否新签发。"""
    if backend != "nvmet":
        return False
    now = now_iso()
    with credentials.locked():
        creds = credentials.load()
        if worker_id in creds["credentials"]:
            return False
        secret = _generate_dhhc1()
        creds["credentials"][worker_id] = {
            "worker_id": worker_id,
            "secret": secret,
            "secret_hash": _secret_hash(secret),
            "created_at": now,
            "updated_at": now,
        }
        credentials.save(creds)
    record("credential.issue", "ok", worker_id=worker_id, backend=backend)
    return True


def _push_credentials(worker_id: str, secret: object = _UNSET) -> None:
    """把 worker 的 NVMe-oF 凭据期望状态推送给持有其盘的 Agent（失败仅审计与记录，不阻断主流程）。
    secret 缺省取密钥库当前条目（无条目 → None 吊销推送）；审计不记密钥本体。"""
    if secret is _UNSET:
        with credentials.locked():
            entry = credentials.load()["credentials"].get(worker_id)
        secret = entry.get("secret") if entry else None
    with store.locked():
        record_ = store.load_workers()["workers"].get(worker_id)
    if not record_:
        return
    for agent_id, sub_nqns in _sync_targets(record_).items():
        if _no_sync_needed(_push_state(worker_id, agent_id), secret):
            continue
        _push_one(worker_id, agent_id, sub_nqns, secret, audit_always=True)


_reconcile_thread_started = False


def reconcile_credentials_once() -> dict[str, int]:
    """对账全量重放（周期线程调用；可测核心）。

    期望（credentials.yml）× 目标（workers.yml 的盘 NQN 分组）逐对比较推送记录，
    密钥哈希一致且已送达（ok/n-a）才跳过——覆盖三类静默缺口：推送失败未重试、
    崩溃缝隙（台账已改、进程未推）、吊销未送达（期望吊销 = 无条目）。未被任何当前
    目标引用的记录剪除：Agent 注销后重新登记 / 盘迁移后无记录，会自然触发重推。"""
    with credentials.locked():
        creds = credentials.load()["credentials"]
    with store.locked():
        workers = store.load_workers()["workers"]
    targets: dict[str, tuple[str, str, list[str], str | None]] = {}
    registered = _registered_agent_ids()
    for worker_id, record_ in workers.items():
        secret = (creds.get(worker_id) or {}).get("secret")
        for agent_id, sub_nqns in _sync_targets(record_, registered).items():
            targets[_push_state_key(worker_id, agent_id)] = (worker_id, agent_id, sub_nqns, secret)
    with credential_pushes.locked():
        data = credential_pushes.load()
        stale = [key for key in data["pushes"] if key not in targets]
        if stale:
            for key in stale:
                data["pushes"].pop(key)
            credential_pushes.save(data)
    pushed = skipped = failed = 0
    for key, (worker_id, agent_id, sub_nqns, secret) in targets.items():
        with credential_pushes.locked():
            prev = credential_pushes.load()["pushes"].get(key, {})
        desired_hash = _secret_hash(secret) if secret else ""
        if _no_sync_needed(prev, secret):
            skipped += 1
            continue
        if prev.get("result") in (PUSH_OK, PUSH_NA) and prev.get("secret_hash", "") == desired_hash:
            skipped += 1
            continue
        if _push_one(worker_id, agent_id, sub_nqns, secret, audit_always=False) == PUSH_FAILED:
            failed += 1
        else:
            pushed += 1
    return {"targets": len(targets), "pushed": pushed, "skipped": skipped, "failed": failed}


def start_credential_reconciler() -> None:
    """启动控制面→Agent 凭据对账线程（FastAPI lifespan 调用；间隔 ≤0 禁用——测试环境）。

    三层互补：本线程覆盖「控制面→Agent」段；Agent reconcile 覆盖「Agent→宿主服务」段；
    Agent 启动重放覆盖其自身重启窗口（见 storager/agent NvmetCredentialCache）。"""
    global _reconcile_thread_started
    interval = settings.credential_reconcile_interval
    if interval <= 0 or _reconcile_thread_started:
        return
    _reconcile_thread_started = True

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                summary = reconcile_credentials_once()
                if summary["failed"] or summary["pushed"]:
                    log.info(f"credential reconcile: {summary}")
            except Exception:
                log.exception("credential reconcile failed")

    threading.Thread(target=_loop, name="credential-reconcile", daemon=True).start()
    log.info(f"credential reconcile loop started: interval={interval}s")


def _validate_dhhc1(secret: str) -> None:
    """DHHC-1 密钥自检（nvmeof-credential-design.md 2.1 契约）：
    前缀、类型两位数字、base64 解码长度 36/68（32/64 字节密钥 + CRC32 小端 4 字节）、CRC 终值。
    非法 → ValueError（配置阶段即失败而非运行时暴露，对齐 nvmet-setup.sh 自检模式）。"""
    if not secret.startswith("DHHC-1:"):
        raise ValueError("secret must start with 'DHHC-1:'")
    rest = secret[len("DHHC-1:"):]
    kind, _, b64 = rest.partition(":")
    if not b64 or len(kind) != 2 or not kind.isdigit():
        raise ValueError("secret type must be two digits (e.g. 01 for SHA-256)")
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        raise ValueError("secret base64 payload is invalid") from None
    if len(raw) not in (36, 68):
        raise ValueError("secret key must be 32 or 64 bytes (36/68 payload incl. CRC32)")
    key, crc = raw[:-4], raw[-4:]
    if zlib.crc32(key) != int.from_bytes(crc, "little"):
        raise ValueError("secret CRC32 checksum mismatch")


def _generate_dhhc1() -> str:
    """生成 DHHC-1:01:<base64(32B 随机 ‖ CRC32 小端终值)>（固件 2.1 契约）。

    与 CLI `workers credential set --generate` 同一格式；产出必过 `_validate_dhhc1`
    （生成与自检互为闭环）。明文不落审计、不经 API 返回，仅存密钥库供 /boot-vars 注入。"""
    key = secrets.token_bytes(32)
    crc = zlib.crc32(key)
    return "DHHC-1:01:" + base64.b64encode(key + crc.to_bytes(4, "little")).decode("ascii")


def _secret_hash(secret: str) -> str:
    """审计用哈希（不用于注入，注入用明文）：sha256 全量，审计输出仅前缀。"""
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _derive_tls_psk(secret: str) -> str:
    """从 worker 的 DHHC-1 认证密钥派生 NVMe/TCP TLS-PSK（32 字节，64 位十六进制）。

    HMAC-SHA256 域分离派生（RFC 2104 PRF）——与 DHHC 认证共用同一把源密钥，**轮换认证
    密钥即同时轮换 TLS-PSK**，因此推送台账的期望指纹不必扩展（PSK 是密钥的纯函数，
    `_push_one` 每次现算）。目标端（nvmet-host keyring）与发起端（initramfs /boot-vars）
    用同一串，identity 见 blueprint/nvme-tcp-tls-in-kernel.md §5。"""
    raw = base64.b64decode(secret.split(":", 2)[2], validate=True)[:32]
    return hmac.new(raw, b"kurrent-nvme-tls-psk-v1", hashlib.sha256).hexdigest()


def _credential_meta(entry: dict[str, Any]) -> dict[str, Any]:
    """凭据元数据视图：不返回明文；secret_hash 只给前缀便于轮换比对。"""
    return {
        "worker_id": entry["worker_id"],
        "exists": True,
        "secret_hash": str(entry.get("secret_hash", ""))[:15],
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
    }


def _credential_push_view(worker_id: str, record_: dict, *, desired: str | None) -> dict[str, Any]:
    """推送状态投影（GET/PUT/DELETE 响应）：逐 Agent 明细 + 汇总，供 CLI/WebUI 呈现。

    汇总 result：ok=全部适用目标已送达 / failed=任一未送达 / pending=等待对账重放
    （含密钥已轮换、记录仍是旧值）/ n-a=目标均非 nvmet 后端（凭据不适用）/ none=无同步目标
    （未设置凭据且无推送历史，或盘所在 Agent 未登记）。"""
    targets = _sync_targets(record_)
    if not targets:
        return {"result": "none", "updated_at": None, "agents": []}
    desired_hash = _secret_hash(desired) if desired else ""
    with credential_pushes.locked():
        pushes = credential_pushes.load()["pushes"]
    items: list[dict[str, Any]] = []
    for agent_id in targets:
        prev = pushes.get(_push_state_key(worker_id, agent_id)) or {}
        if _no_sync_needed(prev, desired):
            # 未设置凭据且无历史：无同步语义，不显示为「待同步」
            continue
        result = prev.get("result", "")
        item: dict[str, Any] = {"agent_id": agent_id, "updated_at": prev.get("updated_at") or None}
        if not prev:
            item["result"] = "pending"
        elif result == PUSH_FAILED:
            item["result"] = PUSH_FAILED
            item["error"] = prev.get("error", "")
        elif result == PUSH_NA:
            item["result"] = PUSH_NA
        elif result == PUSH_OK and prev.get("secret_hash", "") == desired_hash:
            item["result"] = PUSH_OK
        else:
            # 记录为旧值（轮换后未重推）：待对账重放
            item["result"] = "pending"
        items.append(item)
    if not items:
        return {"result": "none", "updated_at": None, "agents": []}
    results = [item["result"] for item in items]
    if PUSH_FAILED in results:
        overall = PUSH_FAILED
    elif "pending" in results:
        overall = "pending"
    elif all(r == PUSH_NA for r in results):
        overall = PUSH_NA
    else:
        overall = PUSH_OK
    stamps = [item["updated_at"] for item in items if item["updated_at"]]
    return {"result": overall, "updated_at": max(stamps) if stamps else None, "agents": items}


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
    record("credential.revoke", "ok", worker_id=worker_id, client=client_host(request))
    _push_credentials(worker_id, secret=None)
    return {"worker_id": worker_id, "deleted": True,
            "push": _credential_push_view(worker_id, worker_record, desired=None)}
