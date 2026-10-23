"""设备池管理端点（Bearer 鉴权）：CRUD / 导入 / 绑定 / 批量绑定 / 吊销，及绑定核心逻辑。"""

import copy
import logging
from typing import Any

from fastapi import Depends, HTTPException, Query, Request

from ..auth import authenticate, authorize_request
from ..models import BatchBindRequest, BindPair, CreateDeviceRequest, ImportDevicesRequest
from ..stores import dnsmasq, devices, record, store
from ..utils import canonical_mac, clean_str, client_host, now_iso

log = logging.getLogger("control-plane")

from ..principal import PrincipalRouter

router = PrincipalRouter(
    dependencies=[Depends(authenticate), Depends(authorize_request)],
    principal={"human", "automation"},
)


@router.get("/devices")
def list_devices(state: str = Query("all")):
    """设备池列表：state 过滤（all/pooled/bound/revoked），含指纹与绑定关系。"""
    if state not in {"all", "pooled", "bound", "revoked"}:
        raise HTTPException(400, "state must be one of: all, pooled, bound, revoked")
    data = devices.load()
    items: list[dict[str, Any]] = []
    for mac, record_ in sorted(data["devices"].items()):
        if state != "all" and record_.get("state") != state:
            continue
        item = copy.deepcopy(record_)
        item["mac"] = mac
        items.append(item)
    return items


@router.get("/devices/{mac}")
def get_device(mac: str):
    """单设备详情：绑定 worker、指纹、首/末次上报。"""
    mac = canonical_mac(mac)
    data = devices.load()
    record_ = data["devices"].get(mac)
    if not record_:
        raise HTTPException(404, f"device not found: {mac}")
    item = copy.deepcopy(record_)
    item["mac"] = mac
    return item


@router.post("/devices", status_code=201)
def create_device(req: CreateDeviceRequest, request: Request):
    """手动注册设备：MAC（+可选 UUID）入池。重复注册（含已吊销）返回 409。"""
    mac = canonical_mac(req.mac)
    with devices.locked():
        data = devices.load()
        devs = data["devices"]
        if mac in devs:
            raise HTTPException(409, f"device already exists: {mac} (state={devs[mac].get('state')})")
        now = now_iso()
        devs[mac] = {
            "mac": mac,
            "uuid": clean_str(req.uuid),
            "state": "pooled",
            "bound_worker_id": None,
            "key_hash": None,
            "source": "manual",
            "fingerprint": {k: v for k, v in {
                "manufacturer": clean_str(req.manufacturer),
                "product": clean_str(req.product),
                "serial": clean_str(req.serial),
            }.items() if v is not None},
            "first_seen": now,
            "last_seen": None,
        }
        devices.save(data)
    record("device.register", "ok", mac=mac, source="manual", client=client_host(request))
    return copy.deepcopy(devs[mac])


@router.post("/devices/import")
def import_devices(req: ImportDevicesRequest, request: Request):
    """批量导入设备清单（MAC 清单预导入）：逐项独立，重复跳过，非法/吊销计 failed。"""
    if not req.entries:
        raise HTTPException(400, "entries must not be empty")
    created: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    now = now_iso()
    with devices.locked():
        data = devices.load()
        devs = data["devices"]
        for entry in req.entries:
            try:
                mac = canonical_mac(entry.mac)
            except HTTPException:
                failed.append({"mac": entry.mac, "reason": "invalid mac"})
                continue
            existing = devs.get(mac)
            if existing:
                if existing.get("state") == "revoked":
                    failed.append({"mac": mac, "reason": "device revoked"})
                else:
                    skipped.append({"mac": mac, "reason": f"already {existing.get('state')}"})
                continue
            devs[mac] = {
                "mac": mac,
                "uuid": clean_str(entry.uuid),
                "state": "pooled",
                "bound_worker_id": None,
                "key_hash": None,
                "source": "manual",
                "fingerprint": {k: v for k, v in {
                    "manufacturer": clean_str(entry.manufacturer),
                    "product": clean_str(entry.product),
                    "serial": clean_str(entry.serial),
                }.items() if v is not None},
                "first_seen": now,
                "last_seen": None,
            }
            created.append({"mac": mac})
        if created:
            devices.save(data)
    record("device.import", "ok", client=client_host(request),
           created=len(created), skipped=len(skipped), failed=len(failed))
    return {"created": created, "skipped": skipped, "failed": failed}


@router.post("/devices/{mac}/bind")
def bind_device(
    mac: str,
    request: Request,
    worker_id: str = Query(..., description="Target worker to bind the device to."),
    force: bool = Query(False, description="Atomic rebind when device or worker is already bound."),
):
    """绑定设备到 Worker（设备↔worker 一对一授权）。默认 409（设备或 worker 已绑定）；
    force=true 原子换绑：预校验 → 新绑定落盘 → 旧绑定清除（旧设备回池）→ 失败回滚。
    幂等：重复绑定同 worker 直接返回。"""
    mac = canonical_mac(mac)
    result = _bind_device(mac, worker_id, force)
    record("device.bind", "ok", mac=mac, worker_id=worker_id, force=force,
           old_worker_id=result.get("old_worker_id"), old_device_mac=result.get("old_device_mac"),
           client=client_host(request))
    # 凭据推送：host NQN 随绑定设备 UUID 变化（force 换绑时旧 worker 的设备已回池，一并推送）
    from .workers import _push_credentials
    _push_credentials(worker_id)
    old_worker = result.get("old_worker_id")
    if old_worker and old_worker != worker_id:
        _push_credentials(old_worker)
    return _device_record(mac)


@router.delete("/devices/{mac}/bind")
def unbind_device(mac: str, request: Request):
    """解绑设备：设备回池（state=pooled）、移除 dnsmasq 绑定；系统盘保留在 Worker（readiness 降级 partial/idle）。"""
    mac = canonical_mac(mac)
    client_ip = client_host(request)
    with store.locked(), devices.locked():
        data = devices.load()
        devs = data["devices"]
        dev = devs.get(mac)
        if not dev:
            raise HTTPException(404, f"device not found: {mac}")
        if dev.get("state") != "bound":
            raise HTTPException(409, f"device not bound: {mac} (state={dev.get('state')})")
        worker_id = dev["bound_worker_id"]
        workers = store.load_workers()["workers"]
        hostname = _hostname_of(workers, worker_id)

        snapshot = copy.deepcopy(data)
        dev["state"] = "pooled"
        dev["bound_worker_id"] = None
        try:
            devices.save(data)
        except Exception as exc:
            raise HTTPException(500, f"devices save failed: {exc}") from exc
        if hostname:
            try:
                removed = dnsmasq.remove_hostname(hostname)
                record("dnsmasq.hosts.delete", "ok", worker_id=worker_id, hostname=hostname, removed=removed)
            except Exception as exc:
                # 回滚台账（dnsmasq 写入失败，恢复设备为 bound）
                data["devices"] = snapshot["devices"]
                try:
                    devices.save(data)
                except Exception:
                    log.exception("unbind rollback: devices save failed")
                raise HTTPException(500, f"dnsmasq unbind failed: {exc}") from exc
    record("device.unbind", "ok", mac=mac, worker_id=worker_id, client=client_ip)
    # 凭据推送：解绑后该 worker 无绑定设备，host_nqns 清空
    from .workers import _push_credentials
    _push_credentials(worker_id)
    return _device_record(mac)


@router.post("/devices/bind/batch/preview")
def batch_bind_preview(req: BatchBindRequest):
    """批量绑定预览（只读）：manifest 清单配对 / sequential 顺序配对 → 配对表。
    冲突项（设备已绑定/worker 已绑定/清单内重复）与池外设备（not_found）不产生任何写入。"""
    pairs = _resolve_pairs(req)
    with store.locked(), devices.locked():
        workers = store.load_workers()["workers"]
        devs = devices.load()["devices"]
        matched: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        not_found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_mac, worker_id, pair in pairs:
            try:
                mac = canonical_mac(raw_mac)
            except HTTPException:
                not_found.append({"mac": raw_mac, "worker_id": worker_id, "reason": "invalid mac"})
                continue
            if mac in seen:
                conflicts.append({"mac": mac, "worker_id": worker_id, "reason": "duplicate in manifest"})
                continue
            seen.add(mac)
            dev = devs.get(mac)
            if not dev:
                not_found.append({"mac": mac, "worker_id": worker_id, "reason": "device not in pool"})
                continue
            if dev.get("state") == "revoked":
                not_found.append({"mac": mac, "worker_id": worker_id, "reason": "device revoked"})
                continue
            if dev.get("state") == "bound":
                conflicts.append({"mac": mac, "worker_id": worker_id,
                                  "reason": f"device already bound to {dev.get('bound_worker_id')}"})
                continue
            if worker_id not in workers:
                conflicts.append({"mac": mac, "worker_id": worker_id, "reason": f"worker not found: {worker_id}"})
                continue
            bound = _device_bound_to_worker(devs, worker_id)
            if bound:
                conflicts.append({"mac": mac, "worker_id": worker_id,
                                  "reason": f"worker already bound to {bound}"})
                continue
            mismatch = _fingerprint_mismatch(dev, pair)
            matched.append({
                "mac": mac,
                "worker_id": worker_id,
                "device_state": dev.get("state", "pooled"),
                "worker_state": workers[worker_id].get("state", "registered"),
                "fingerprint_mismatch": mismatch,
            })
        return {
            "matched": matched,
            "conflicts": conflicts,
            "not_found": not_found,
            "summary": {
                "total": len(pairs),
                "ok": len(matched),
                "conflict": len(conflicts),
                "not_found": len(not_found),
            },
        }


@router.post("/devices/bind/batch")
def batch_bind(req: BatchBindRequest, request: Request):
    """批量绑定执行（幂等，逐项独立）：已绑定配对 = skipped；池外设备 = failed（须先入池）；
    指纹不符不阻断（succeeded 项带 fingerprint_mismatch 标记）。"""
    pairs = _resolve_pairs(req)
    client_ip = client_host(request)
    succeeded: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_mac, worker_id, pair in pairs:
        try:
            mac = canonical_mac(raw_mac)
        except HTTPException:
            failed.append({"mac": raw_mac, "worker_id": worker_id, "reason": "invalid mac"})
            continue
        if mac in seen:
            skipped.append({"mac": mac, "worker_id": worker_id, "reason": "duplicate in manifest"})
            continue
        seen.add(mac)
        # 幂等预检：已绑定目标 worker → skipped
        with store.locked(), devices.locked():
            devs = devices.load()["devices"]
            dev = devs.get(mac)
            if dev and dev.get("state") == "bound" and dev.get("bound_worker_id") == worker_id:
                skipped.append({"mac": mac, "worker_id": worker_id, "reason": "already bound"})
                continue
        try:
            _bind_device(mac, worker_id, force=False)
        except HTTPException as exc:
            if exc.status_code == 404:
                failed.append({"mac": mac, "worker_id": worker_id, "reason": str(exc.detail)})
            else:
                skipped.append({"mac": mac, "worker_id": worker_id, "reason": str(exc.detail)})
            continue
        item: dict[str, Any] = {"mac": mac, "worker_id": worker_id}
        with devices.locked():
            dev = devices.load()["devices"].get(mac)
            mismatch = _fingerprint_mismatch(dev, pair)
            if mismatch:
                item["fingerprint_mismatch"] = mismatch
        succeeded.append(item)
        record("device.bind", "ok", mac=mac, worker_id=worker_id, force=False,
               old_worker_id=None, old_device_mac=None, client=client_ip)
    record("device.bind.batch", "ok", client=client_ip, ok=len(succeeded),
           skipped=len(skipped), failed=len(failed))
    return {"succeeded": succeeded, "skipped": skipped, "failed": failed}


@router.delete("/devices/revoked")
def purge_revoked_devices(request: Request, dry_run: bool = False,
                          mac: list[str] = Query(default=[])) -> dict:
    """清除已吊销设备（revoked → 删除条目）；`dry_run=true` 只列出将被清除的 MAC。

    `mac` 可重复给出以限定范围（未吊销的 MAC 会被忽略）；缺省清除全部 revoked。
    只做台账清理，不改变绑定关系（revoked 设备本就不允许绑定）；凭据域无条目可清。
    返回按 MAC 排序的列表，便于核对（探测残留、误吊销的清理路径）。
    """
    wanted = {canonical_mac(item) for item in mac} or None
    with devices.locked():
        data = devices.load()
        targets = sorted(m for m, dev in data["devices"].items()
                         if dev.get("state") == "revoked"
                         and (wanted is None or m in wanted))
        if targets and not dry_run:
            for target in targets:
                data["devices"].pop(target)
            devices.save(data)
    if targets and not dry_run:
        record("device.purge", "ok", macs=targets, client=client_host(request))
    return {"purged": targets, "count": len(targets), "dry_run": dry_run}


@router.delete("/devices/{mac}")
def revoke_device(mac: str, request: Request):
    """注销设备（吊销）：pooled → revoked；已绑定需先解绑（bound → 409）；已吊销 409。"""
    mac = canonical_mac(mac)
    with devices.locked():
        data = devices.load()
        devs = data["devices"]
        record_ = devs.get(mac)
        if not record_:
            raise HTTPException(404, f"device not found: {mac}")
        if record_.get("state") == "revoked":
            raise HTTPException(409, f"device already revoked: {mac}")
        if record_.get("state") == "bound":
            raise HTTPException(
                409, f"device is bound to worker {record_.get('bound_worker_id')}, unbind first: {mac}"
            )
        record_["state"] = "revoked"
        devices.save(data)
    record("device.revoke", "ok", mac=mac, client=client_host(request))
    return copy.deepcopy(record_)


def _device_record(mac: str) -> dict[str, Any]:
    """设备台账记录投影（含 mac 字段）。"""
    data = devices.load()
    record_ = data["devices"].get(mac)
    if not record_:
        raise HTTPException(404, f"device not found: {mac}")
    item = copy.deepcopy(record_)
    item["mac"] = mac
    return item


def _bind_device(mac: str, worker_id: str, force: bool) -> dict[str, Any]:
    """绑定/换绑核心（设备↔worker 一对一授权）。
    force=true 原子换绑：预校验 → 新绑定落盘 → 旧绑定清除（旧设备回池）→ 失败回滚（恢复台账 + 尽力恢复 dnsmasq）。
    幂等：设备已绑该 worker 且 worker 绑定该设备 → 直接返回。返回 {old_worker_id, old_device_mac}。"""
    with store.locked(), devices.locked():
        workers = store.load_workers()["workers"]
        record_ = workers.get(worker_id)
        if not record_:
            raise HTTPException(404, f"worker not found: {worker_id}")
        hostname = str(record_["hostname"])
        data = devices.load()
        devs = data["devices"]
        dev = devs.get(mac)
        if not dev:
            raise HTTPException(404, f"device not found: {mac}")
        if dev.get("state") == "revoked":
            raise HTTPException(409, f"device revoked: {mac}")

        old_worker = dev.get("bound_worker_id") if dev.get("state") == "bound" else None
        old_device = _device_bound_to_worker(devs, worker_id)
        if old_worker == worker_id and old_device == mac:
            return {"old_worker_id": old_worker, "old_device_mac": old_device}  # 幂等
        if old_worker and not force:
            raise HTTPException(409, f"device already bound to {old_worker}: {mac}")
        if old_device and not force:
            raise HTTPException(409, f"worker already bound to {old_device}: {worker_id}")

        # dnsmasq 预校验（台账与 dhcp 一致性）：非 force 要求双空闲；force 允许旧绑定存在、拒绝第三方占用
        try:
            if force:
                _check_swap_bindings(mac, hostname, old_worker, old_device, workers)
            else:
                dnsmasq.ensure_free(mac, hostname)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

        snapshot = copy.deepcopy(data)

        # 1) 新绑定落盘 + 旧设备回池（一次原子写）
        dev["state"] = "bound"
        dev["bound_worker_id"] = worker_id
        if old_device and old_device != mac:
            devs[old_device]["state"] = "pooled"
            devs[old_device]["bound_worker_id"] = None
        try:
            devices.save(data)
        except Exception as exc:
            raise HTTPException(500, f"devices save failed: {exc}") from exc

        # 2) dnsmasq：清旧绑定 + 加新绑定（dnsmasq 自己监听 dhcp-hostsfile，无需通知）
        try:
            if old_worker and old_worker != worker_id:
                old_hostname = _hostname_of(workers, old_worker)
                if old_hostname:
                    dnsmasq.remove_hostname(old_hostname)
            if old_device and old_device != mac:
                dnsmasq.remove_hostname(hostname)
            dnsmasq.add_binding(mac, hostname)
        except Exception as exc:
            # 3) 失败回滚：恢复台账快照 + 尽力恢复 dnsmasq
            data["devices"] = snapshot["devices"]
            try:
                devices.save(data)
            except Exception:
                log.exception("bind rollback: devices save failed")
            try:
                dnsmasq.remove_hostname(hostname)
                if old_worker and old_worker != worker_id:
                    old_hostname = _hostname_of(workers, old_worker)
                    if old_hostname:
                        dnsmasq.add_binding(mac, old_hostname)
                if old_device and old_device != mac:
                    dnsmasq.add_binding(old_device, hostname)
            except Exception:
                log.exception("bind rollback: dnsmasq restore failed")
            raise HTTPException(500, f"dnsmasq binding failed: {exc}") from exc

        return {"old_worker_id": old_worker, "old_device_mac": old_device}


def _check_swap_bindings(
    mac: str,
    hostname: str,
    old_worker: str | None,
    old_device: str | None,
    workers: dict[str, Any],
) -> None:
    """force 换绑的 dnsmasq 一致性预检：允许旧绑定存在（mac→旧 worker hostname、hostname→旧设备），
    第三方占用（其他 hostname 用了该 mac、其他 mac 用了该 hostname）→ 拒绝。"""
    old_hostname = _hostname_of(workers, old_worker)
    for binding in dnsmasq.list_bindings():
        if binding.mac == mac:
            if old_hostname and binding.hostname == old_hostname:
                continue
            raise ValueError(f"mac already bound to {binding.hostname}: {mac}")
        if binding.hostname == hostname:
            if old_device == binding.mac:
                continue
            raise ValueError(f"hostname already bound in dnsmasq: {hostname}")


def _device_bound_to_worker(devs: dict[str, Any], worker_id: str) -> str | None:
    """反查绑定到该 worker 的设备 mac（设备台账权威面；devs 为已加载的 devices dict）。"""
    for mac, dev in devs.items():
        if dev.get("bound_worker_id") == worker_id:
            return mac
    return None


def _hostname_of(workers: dict[str, Any], worker_id: str | None) -> str | None:
    if not worker_id:
        return None
    record_ = workers.get(worker_id)
    return str(record_["hostname"]) if record_ else None


def _ensure_device_poolable(mac: str) -> dict[str, Any]:
    """设备池预校验：设备存在、未吊销、未绑定（pooled）→ 返回设备记录；否则 409。"""
    with devices.locked():
        data = devices.load()
        dev = data["devices"].get(mac)
        if not dev:
            raise HTTPException(409, f"device not in pool, register first: {mac}")
        if dev.get("state") == "revoked":
            raise HTTPException(409, f"device revoked: {mac}")
        if dev.get("state") == "bound":
            raise HTTPException(409, f"device already bound to {dev.get('bound_worker_id')}: {mac}")
        return dev


def _unbind_worker_devices(data: dict[str, Any], worker_id: str) -> list[str]:
    """联动解绑：把绑定到该 worker 的设备全部回池（state=pooled），返回解绑 mac 列表。"""
    unbound: list[str] = []
    for mac, dev in data["devices"].items():
        if dev.get("bound_worker_id") == worker_id:
            dev["state"] = "pooled"
            dev["bound_worker_id"] = None
            unbound.append(mac)
    return unbound


def _rollback_devices_binding(new_mac: str, old_released: str | None, worker_id: str) -> None:
    """PUT mac 换绑失败回滚：恢复台账（新设备回池、旧设备恢复绑定）。"""
    try:
        with devices.locked():
            data = devices.load()
            devs = data["devices"]
            dev = devs.get(new_mac)
            if dev and dev.get("state") == "bound" and dev.get("bound_worker_id") == worker_id:
                dev["state"] = "pooled"
                dev["bound_worker_id"] = None
            if old_released:
                old_dev = devs.get(old_released)
                if old_dev and old_dev.get("state") == "pooled":
                    old_dev["state"] = "bound"
                    old_dev["bound_worker_id"] = worker_id
            devices.save(data)
    except Exception:
        log.exception("worker.mac.update rollback: devices save failed")


def _resolve_pairs(req: BatchBindRequest) -> list[tuple[str, str, BindPair | None]]:
    """解析批量配对方式：manifest（pairs 清单）或 sequential（macs/worker_ids 下标对齐）。"""
    if req.mode == "manifest":
        if not req.pairs:
            raise HTTPException(400, "mode=manifest requires pairs")
        return [(p.mac, p.worker_id, p) for p in req.pairs]
    if req.mode == "sequential":
        if not req.macs or not req.worker_ids:
            raise HTTPException(400, "mode=sequential requires macs and worker_ids")
        if len(req.macs) != len(req.worker_ids):
            raise HTTPException(400, "macs and worker_ids must have the same length")
        return [(m, w, None) for m, w in zip(req.macs, req.worker_ids)]
    raise HTTPException(400, "mode must be manifest or sequential")


def _fingerprint_mismatch(dev: dict[str, Any] | None, pair: BindPair | None) -> dict[str, Any] | None:
    """清单申报列与设备上报指纹比对（申报性质，不阻断绑定）：
    申报值有值且与上报值（已上报）不符 → {"fields": [列名...]}；无比对列或一致 → None。"""
    if pair is None or dev is None:
        return None
    fp = dev.get("fingerprint") or {}
    checks = (
        ("manufacturer", pair.manufacturer, fp.get("manufacturer")),
        ("product", pair.product, fp.get("product")),
        ("serial", pair.serial, fp.get("serial")),
        ("uuid", pair.uuid, dev.get("uuid")),
    )
    fields = [
        name for name, expected, actual in checks
        if expected and actual is not None and str(expected).strip() != str(actual).strip()
    ]
    return {"fields": fields} if fields else None


def migrate_legacy_devices() -> None:
    """旧数据迁移：扫描 workers.yml + dnsmasq 绑定，为每个已有 MAC 绑定生成 bound 设备实体
    （state=bound, bound_worker_id=对应 worker, source=manual, 指纹空, 等待首次上报补充）。
    幂等：设备已存在则跳过。失败仅记日志，不阻断启动。"""
    try:
        with store.locked(), devices.locked():
            data = store.load_workers()
            devs = devices.load()
            changed = False
            for worker_id, record_ in data["workers"].items():
                hostname = str(record_.get("hostname", ""))
                mac = dnsmasq.find_mac(hostname) if hostname else None
                if not mac or mac in devs["devices"]:
                    continue
                devs["devices"][mac] = {
                    "mac": mac,
                    "uuid": None,
                    "state": "bound",
                    "bound_worker_id": worker_id,
                    "key_hash": None,
                    "source": "manual",
                    "fingerprint": {},
                    "first_seen": now_iso(),
                    "last_seen": None,
                }
                changed = True
            if changed:
                devices.save(devs)
    except Exception:
        log.exception("legacy device migration failed")
