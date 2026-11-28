"""Worker 系统盘与默认启动端点：单盘/批量建删、默认盘切换（含菜单覆盖）。"""

from typing import Any

from fastapi import Depends, HTTPException, Query, Request

from .client import AgentAPIError
from ..auth.auth import authenticate, authorize_request
from ..core.models import BatchCreateWorkerDiskRequest, BatchDeleteWorkerDiskRequest, BatchDiskTarget, BatchSetDefaultBootRequest, CreateWorkerDiskRequest, DiskPlacementRequest, SetWorkerDefaultBootRequest
from ..core.stores import agents, record, store
from ..core.utils import add_worker_disk, build_disk_filename, build_nqn, canonical_id, canonical_os, canonical_os_tag, canonical_os_version, client_host, disk_source, find_disk_by_os_version, find_disk_by_tag, gen_os_tag, nqn_to_iqn, validate_disk, worker_disks
from .placement import PlacementError, resolve as resolve_placement
from ..auth.principal import PrincipalRouter

from .credentials import _issue_credential, _push_credentials
from .worker_state import MENU_ITEMS, _batch_default_decision, _delete_one_disk, _enrich_worker, _match_disk

router = PrincipalRouter(
    dependencies=[Depends(authenticate), Depends(authorize_request)],
    principal={"human", "automation"},
)


@router.post("/workers/{worker_id}/luns/disk", status_code=201)
def create_worker_disk(worker_id: str, req: CreateWorkerDiskRequest, request: Request):
    """给指定 Worker 创建系统盘 LUN：传 master 走母盘克隆，传 size 建空白盘。
    os/os_version 为系统备注（人类理解用），同 (os, os_version) 至多一块；
    盘唯一标识 = os_tag（随机 12 hex），进盘 NQN/文件名（数据面）。
    首块盘自动设为默认启动盘（多盘时用 default-disk 端点切换）。"""
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
                # 批量部署约定：创建成功即设为默认启动盘（单盘接口在首块盘时同样自动设置）
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
    被删盘若是默认启动，清除 default_disk（menu_default 是显式覆盖，保留不动）；若只剩一块盘，
    它成为默认启动盘；无盘时状态回退 registered。"""
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
            info = _delete_one_disk(data, worker_id, os_tag,
                                    delete_file=delete_file, ignore_missing=ignore_missing_target)
            record("agent.delete_disk", "ok", worker_id=worker_id, agent=info["agent"],
                   iqn=info["iqn"], delete_file=delete_file)
            store.save_workers(data)
            record("workers.disk.delete", "ok", worker_id=worker_id, os=info["os"], os_tag=os_tag)
            record("worker.disk.delete", "succeeded", worker_id=worker_id, client=client_ip)
            _push_credentials(worker_id)  # 盘已删：同步移除 Agent 侧 hosts 登记
            return _enrich_worker(worker_id, record_)
        except AgentAPIError as exc:
            record("worker.disk.delete", "failed", worker_id=worker_id, client=client_ip, error=exc.detail)
            raise HTTPException(exc.status_code, {"agent": exc.agent_id, "error": exc.detail}) from exc
        except Exception as exc:
            record("worker.disk.delete", "failed", worker_id=worker_id, client=client_ip, error=str(exc))
            raise HTTPException(500, str(exc)) from exc


@router.post("/workers/luns/disk/batch-delete")
def batch_delete_worker_disks(req: BatchDeleteWorkerDiskRequest, request: Request):
    """批量删除系统盘：targets 逐项点名 或 workers（删除这些 Worker 的全部盘）二选一。
    dry_run=true 只返回计划不删盘；delete_file/ignore_missing_target 与单删一致。
    逐项独立执行，返回 succeeded/skipped/failed 汇总（盘或 worker 已不存在按 skipped）。"""
    if req.targets and req.workers:
        raise HTTPException(400, "specify either targets or workers, not both")
    if not req.targets and not req.workers:
        raise HTTPException(400, "targets or workers must not be empty")

    data = store.load_workers()
    items: list[tuple[str, str]] = []
    worker_missing: list[dict[str, Any]] = []
    if req.targets:
        items = [(canonical_id(t.worker_id), canonical_os_tag(t.os_tag)) for t in req.targets]
    else:
        for w in req.workers:
            wid = canonical_id(w)
            record_ = data["workers"].get(wid)
            if not record_:
                worker_missing.append({"worker_id": wid, "os_tag": None, "reason": "worker not found"})
                continue
            for d in worker_disks(record_):
                if d.get("os_tag"):
                    items.append((wid, str(d["os_tag"])))

    if req.dry_run:
        plan: list[dict[str, Any]] = []
        for wid, tag in items:
            record_ = data["workers"].get(wid)
            if not record_:
                action, reason = "error", "worker not found"
            elif not find_disk_by_tag(record_, tag):
                action, reason = "skip", "no such disk"
            else:
                action, reason = "delete", ""
            plan.append({"worker_id": wid, "os_tag": tag, "action": action, "reason": reason})
        plan += [{"worker_id": m["worker_id"], "os_tag": None,
                  "action": "error", "reason": m["reason"]} for m in worker_missing]
        return {"dry_run": True, "plan": plan}

    client_ip = client_host(request)
    succeeded: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = list(worker_missing)
    failed: list[dict[str, Any]] = []
    touched: set[str] = set()
    with store.locked():
        data = store.load_workers()
        for wid, tag in items:
            try:
                info = _delete_one_disk(data, wid, tag, delete_file=req.delete_file,
                                        ignore_missing=req.ignore_missing_target)
            except HTTPException as exc:
                skipped.append({"worker_id": wid, "os_tag": tag, "reason": str(exc.detail)})
                continue
            except AgentAPIError as exc:
                failed.append({"worker_id": wid, "os_tag": tag, "error": exc.detail})
                continue
            except Exception as exc:
                failed.append({"worker_id": wid, "os_tag": tag, "error": str(exc)})
                continue
            record("agent.delete_disk", "ok", worker_id=wid, agent=info["agent"],
                   iqn=info["iqn"], delete_file=req.delete_file)
            store.save_workers(data)
            record("workers.disk.delete", "ok", worker_id=wid, os=info["os"], os_tag=tag)
            record("worker.disk.delete", "batch", worker_id=wid, os_tag=tag, client=client_ip)
            touched.add(wid)
            succeeded.append({"worker_id": wid, "os_tag": tag, "os": info["os"]})
    for wid in touched:
        _push_credentials(wid)
    record("workers.disk.delete.batch", "ok", succeeded=len(succeeded),
           skipped=len(skipped), failed=len(failed), client=client_ip)
    return {"succeeded": succeeded, "skipped": skipped, "failed": failed}


@router.put("/workers/{worker_id}/default-disk")
def set_worker_default_boot(worker_id: str, req: SetWorkerDefaultBootRequest, request: Request):
    """设置 Worker 默认启动配置：disk=默认启动盘（os_tag，须为已挂系统盘）；
    menu_default/menu_timeout=菜单项覆盖；传 null 清除对应项。
    推导链：boot.menu_default（显式覆盖，优先）> default_disk > reboot。"""
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


@router.put("/workers/default-disk/batch")
def batch_set_default_disk(req: BatchSetDefaultBootRequest, request: Request):
    """批量设默认启动盘：targets 逐项 (worker_id, os_tag) 或 workers + 按 os[/os_version] 匹配 二选一。
    只改 default_disk（菜单覆盖不在批量范围）。dry_run=true 只返回计划。
    逐项独立执行，返回 succeeded/skipped/failed 汇总（已是默认按 skipped，匹配失败/盘不存在按 failed）。"""
    if req.targets and req.workers:
        raise HTTPException(400, "specify either targets or workers, not both")
    if not req.targets and not req.workers:
        raise HTTPException(400, "targets or workers must not be empty")

    os_name = canonical_os(req.os) if req.workers else ""
    os_version = canonical_os_version(req.os_version) if req.workers else ""
    data = store.load_workers()

    items: list[dict[str, Any]] = []
    if req.targets:
        for t in req.targets:
            items.append({"worker_id": canonical_id(t.worker_id),
                          "os_tag": canonical_os_tag(t.os_tag), "error": ""})
    else:
        for w in req.workers:
            wid = canonical_id(w)
            record_ = data["workers"].get(wid)
            if not record_:
                items.append({"worker_id": wid, "os_tag": None, "error": "worker not found"})
                continue
            disk, err = _match_disk(record_, os_name, os_version)
            items.append({"worker_id": wid,
                          "os_tag": str(disk["os_tag"]) if disk else None, "error": err})

    if req.dry_run:
        plan = []
        for it in items:
            action, reason = _batch_default_decision(
                data, it["worker_id"], it.get("os_tag"), error=it.get("error", ""))
            plan.append({"worker_id": it["worker_id"], "os_tag": it.get("os_tag"),
                         "action": {"set": "set", "skipped": "skip", "failed": "error"}[action],
                         "reason": reason})
        return {"dry_run": True, "plan": plan}

    client_ip = client_host(request)
    succeeded: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    with store.locked():
        data = store.load_workers()
        for it in items:
            wid, tag = it["worker_id"], it.get("os_tag")
            action, reason = _batch_default_decision(data, wid, tag, error=it.get("error", ""))
            if action == "failed":
                failed.append({"worker_id": wid, "os_tag": tag, "error": reason})
                continue
            if action == "skipped":
                skipped.append({"worker_id": wid, "os_tag": tag, "reason": reason})
                continue
            data["workers"][wid]["default_disk"] = tag
            store.save_workers(data)
            record("worker.boot.set", "batch", worker_id=wid, client=client_ip,
                   changes=f"default_disk:{tag}")
            succeeded.append({"worker_id": wid, "os_tag": tag})
    record("workers.default_disk.batch", "ok", succeeded=len(succeeded),
           skipped=len(skipped), failed=len(failed), client=client_ip)
    return {"succeeded": succeeded, "skipped": skipped, "failed": failed}
