"""Worker 状态投影与数据面 target 操作（路由与迁移共用）。

职责：设备台账反查、就绪度与运行态投影、Agent target 删除/匹配；
不定义 HTTP 路由，供 routes_workers / routes_worker_disks 复用。
"""

import copy
from typing import Any

from fastapi import HTTPException

from .client import AgentAPIError
from ..core.stores import agents, devices, dnsmasq
from ..core.utils import find_disk_by_tag, worker_disks


# menu.ipxe 主菜单中可手工选择的 item ID；boot-os 由默认盘推导，不在校验集合内
MENU_ITEMS = {"config", "shell", "reboot", "exit"}


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


def _delete_one_disk(
    data: dict[str, Any],
    worker_id: str,
    os_tag: str,
    *,
    delete_file: bool,
    ignore_missing: bool,
) -> dict[str, Any]:
    """删除一块系统盘并联动清理默认盘/菜单/状态（单删与批量共用）。

    调用方持有 store 锁并负责 save_workers 与推送凭据；返回被删盘的 {os, iqn, agent} 供审计。
    盘或 worker 不存在抛 HTTPException(404)。"""
    record_ = data["workers"].get(worker_id)
    if not record_:
        raise HTTPException(404, f"worker not found: {worker_id}")
    disk = find_disk_by_tag(record_, os_tag)
    if not disk:
        raise HTTPException(404, f"worker {worker_id} has no system disk with os_tag {os_tag}")
    os_name = str(disk.get("os", ""))
    _delete_target(disk, delete_file=delete_file, ignore_missing=ignore_missing)

    disks = record_.get("disks")
    if disks is not None:
        disks[:] = [d for d in disks if d is not disk]
    else:
        record_.pop("disk", None)  # 旧台账单盘字段

    # 被删盘正是默认启动盘时清除 default_disk；menu_default 是显式覆盖，删盘不动它
    if str(record_.get("default_disk", "")).lower() == os_tag:
        record_.pop("default_disk", None)

    # 只剩一块盘时它就是默认启动盘
    remaining = worker_disks(record_)
    if len(remaining) == 1 and remaining[0].get("os_tag"):
        record_["default_disk"] = str(remaining[0]["os_tag"])

    # 无盘时状态回退 registered，等待重新建盘
    if not remaining and record_.get("state") == "ready":
        record_["state"] = "registered"
    return {"os": os_name, "iqn": disk.get("iqn"), "agent": disk.get("agent")}


def _match_disk(record_: dict[str, Any], os_name: str, os_version: str) -> tuple[dict[str, Any] | None, str]:
    """按 os[/os_version] 匹配一块系统盘：os_version 空时要求该 os 下唯一。返回 (disk, error)。"""
    candidates = [d for d in worker_disks(record_)
                  if str(d.get("os", "")) == os_name
                  and (not os_version or str(d.get("os_version", "")) == os_version)]
    label = f"{os_name} {os_version}".strip()
    if not candidates:
        return None, f"no disk for {label}"
    if len(candidates) > 1:
        return None, f"ambiguous: multiple disks for {label}"
    return candidates[0], ""


def _batch_default_decision(
    data: dict[str, Any], worker_id: str, os_tag: str | None, *, error: str,
) -> tuple[str, str]:
    """批量设默认的逐项判定：返回 (action, reason)，action ∈ {set, skipped, failed}。"""
    if error or os_tag is None:
        return "failed", error or "match failed"
    record_ = data["workers"].get(worker_id)
    if not record_:
        return "failed", "worker not found"
    if not find_disk_by_tag(record_, os_tag):
        return "failed", "no such disk"
    if str(record_.get("default_disk", "")).lower() == os_tag:
        return "skipped", "already default"
    return "set", ""


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
    # MAC 以设备台账（bound_worker_id）为权威；dnsmasq 绑定只是派生投影
    bound_device = _bound_device_mac_for(worker_id)
    item["mac"] = bound_device
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


