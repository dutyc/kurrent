"""Worker 管理端点（Bearer 鉴权）：CRUD / 批量 / MAC 换绑与台账投影。

系统盘、默认启动与 NVMe-oF 凭据端点在 routes_worker_disks / routes_credentials。
"""

import logging
from typing import Any

from fastapi import Depends, HTTPException, Query, Request

from .client import AgentAPIError
from ..auth.auth import authenticate, authorize_request
from ..core.config import settings
from ..core.models import BatchCreateWorkersRequest, BatchDeleteWorkersRequest, CreateWorkerRequest, UpdateWorkerMacRequest
from ..core.stores import agents, credentials, devices, dnsmasq, record, store
from ..core.utils import canonical_hostname, canonical_id, canonical_mac, client_host, ensure_hostname_not_in_workers, worker_disks
from .routes_devices import _bind_device, _boot_tag, _ensure_device_poolable, _rollback_devices_binding, _unbind_worker_devices
from ..auth.principal import PrincipalRouter

from .credentials import _credential_meta, _forget_worker_credentials, _push_credentials
from .worker_state import MENU_ITEMS, _actual_state, _delete_target, _enrich_worker

log = logging.getLogger("control-plane")

router = PrincipalRouter(
    dependencies=[Depends(authenticate), Depends(authorize_request)],
    principal={"human", "automation"},
)


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


@router.get("/workers/{worker_id}/disks/status")
def get_worker_disks_status(worker_id: str):
    """逐盘运行时状态：导出在线、凭据在位、活跃会话、盘占用（逻辑大小 + 物理分配）。

    盘可能分布在不同 Agent（放置策略均摊）：按盘所属 Agent 分组各取一次盘状态，
    再按盘 NQN 对齐。凭据为 Worker 级，关联展示、不重复取数。"""
    worker_id = canonical_id(worker_id)
    record_ = store.load_workers()["workers"].get(worker_id)
    if not record_:
        raise HTTPException(404, f"worker not found: {worker_id}")
    disks = worker_disks(record_)

    status_by_nqn: dict[str, dict[str, Any]] = {}
    agent_errors: dict[str, str] = {}
    for agent_id in {d.get("agent") for d in disks if d.get("agent")}:
        try:
            agent = agents.get(agent_id)
            targets = agents.client(agent).disk_status()
        except KeyError:
            agent_errors[agent_id] = f"agent not registered: {agent_id}"
            continue
        except AgentAPIError as exc:
            agent_errors[agent_id] = exc.detail
            continue
        for t in targets:
            key = t.get("nqn") or t.get("iqn")
            if key:
                status_by_nqn[key] = t

    items = []
    for d in disks:
        st = status_by_nqn.get(d.get("nqn")) or status_by_nqn.get(d.get("iqn")) or {}
        luns = st.get("luns") or []
        lun = luns[0] if luns else {}
        items.append({
            "os_tag": d.get("os_tag"),
            "os": d.get("os"),
            "nqn": d.get("nqn"),
            "iqn": d.get("iqn"),
            "exported": st.get("exported"),
            "hosts": st.get("hosts", []),
            "controllers": st.get("controllers", []),
            "size": lun.get("size"),
            "physical_bytes": lun.get("physical_bytes"),
            "error": agent_errors.get(d.get("agent")),
        })

    with credentials.locked():
        entry = credentials.load()["credentials"].get(worker_id)
    credential = _credential_meta(entry) if entry else {"worker_id": worker_id, "exists": False}
    return {"worker_id": worker_id, "credential": credential, "disks": items}


@router.post("/workers", status_code=201)
def create_worker(req: CreateWorkerRequest, request: Request):
    """注册 Worker 身份：写入台账 + hostname 绑定。
    mac 可选：不传 = 纯空转 Worker；传 = 校验设备在设备池中并直接绑定（一对一授权）。
    存储与身份分离：系统盘须另调 POST /workers/{worker_id}/luns/disk。"""
    worker_id = canonical_id(req.worker_id)
    hostname = canonical_hostname(req.hostname or worker_id)
    arch = req.arch or settings.default_arch
    mac = canonical_mac(req.mac) if req.mac else None

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

        worker_record = {
            "hostname": hostname,
            "arch": arch,
            "state": "registered",
            "disks": [],
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


@router.post("/workers/batch")
def batch_create_workers(req: BatchCreateWorkersRequest, request: Request):
    """批量创建 Worker（逐项独立，幂等重跑）：命名规则 + 数量生成 worker_id（worker-01…），
    macs 与 count 等长时逐项绑定（设备须在池中，不传 = 全部纯空转）。
    已存在 → skipped；设备不可绑定 → failed 且该项不创建（可修正后重试）。"""
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
    新 MAC 须在设备池中（pooled），绑定新设备；旧设备解绑回池。
    绑定关系以设备台账为权威：旧设备从 `devices.yml` 反查，dnsmasq 绑定是派生投影，
    缺失不影响换绑（本端点负责补写）。审计记 device.bind + worker.mac.update（兼容）。"""
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
            bound_worker = new_dev.get("bound_worker_id") if new_state == "bound" else None
            if bound_worker and bound_worker != worker_id:
                raise HTTPException(409, f"device already bound to {bound_worker}: {new_mac}")

            old_mac = next((mac for mac, dev in devs.items()
                            if dev.get("state") == "bound" and dev.get("bound_worker_id") == worker_id), None)
            changed = bound_worker != worker_id
            if changed:
                new_dev["state"] = "bound"
                new_dev["bound_worker_id"] = worker_id
                if old_mac and old_mac != new_mac:
                    old_dev = devs[old_mac]
                    old_dev["state"] = "pooled"
                    old_dev["bound_worker_id"] = None
                    old_released = old_mac
                try:
                    devices.save(devs_data)
                except Exception as exc:
                    raise HTTPException(500, f"devices save failed: {exc}") from exc

        # dnsmasq 是设备台账的派生投影：缺失补写、已有替换（hostname 不变，仅换 MAC）
        try:
            dnsmasq.set_binding(new_mac, hostname, _boot_tag(new_dev.get("boot_firmware")))
        except ValueError as exc:
            if changed:
                _rollback_devices_binding(new_mac, old_released, worker_id)
            record("worker.mac.update", "failed", worker_id=worker_id, client=client_ip, error=str(exc))
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            if changed:
                _rollback_devices_binding(new_mac, old_released, worker_id)
            record("worker.mac.update", "failed", worker_id=worker_id, client=client_ip, error=str(exc))
            raise HTTPException(500, f"dnsmasq binding failed: {exc}") from exc

        record("worker.mac.update", "ok", worker_id=worker_id, hostname=hostname,
               old_mac=old_mac, new_mac=new_mac, changed=changed, client=client_ip)
        if old_released:
            record("device.unbind", "ok", mac=old_released, worker_id=worker_id,
                   reason="worker.mac.update", client=client_ip)
        if changed:
            record("device.bind", "ok", mac=new_mac, worker_id=worker_id, force=True,
                   old_worker_id=worker_id, old_device_mac=old_released, client=client_ip)
        # host NQN/PSK 按 worker 派生、与设备无关；换绑后重放一次，保持节点侧登记与台账一致
        _push_credentials(worker_id)
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

    return {"succeeded": succeeded, "failed": failed}


def migrate_single_disk_default() -> None:
    """存量迁移：只有一块盘却没有 default_disk 的 Worker，将该盘设为默认启动盘（幂等）。"""
    try:
        with store.locked():
            data = store.load_workers()
            changed = False
            for record_ in data["workers"].values():
                disks = worker_disks(record_)
                if len(disks) == 1 and not record_.get("default_disk") and disks[0].get("os_tag"):
                    record_["default_disk"] = str(disks[0]["os_tag"])
                    changed = True
            if changed:
                store.save_workers(data)
    except Exception:
        log.exception("workers: single-disk default migration failed")


def migrate_menu_defaults() -> None:
    """存量迁移：删除菜单里已不存在的 boot.menu_default（旧 OS 名与子菜单项）。

    新写入由 API 拒绝旧值；存量记录若保留，会把不存在的项传给 iPXE
    `choose --default`。删除后回落到 default_disk 或 reboot（幂等）。
    """
    try:
        with store.locked():
            data = store.load_workers()
            changed = False
            for worker_id, record_ in data["workers"].items():
                boot = record_.get("boot")
                if not isinstance(boot, dict):
                    continue
                invalid = False
                for key in ("menu_default", "menu-default"):
                    raw = boot.get(key)
                    if raw is None:
                        continue
                    if str(raw).strip().lower() in MENU_ITEMS:
                        continue
                    boot.pop(key, None)
                    invalid = True
                if not invalid:
                    continue
                if not boot:
                    record_.pop("boot", None)
                changed = True
                log.warning("workers: removed legacy menu_default from %s", worker_id)
            if changed:
                store.save_workers(data)
    except Exception:
        log.exception("workers: menu_default migration failed")
