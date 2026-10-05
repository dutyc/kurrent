"""Agent 管理端点（Bearer 鉴权）：注册/更新/探测、LUN 管理、母盘聚合（/masters）。

bootstrap token 签发已移至 POST /pki/tokens（集群级通用引导凭据，不绑节点）；
nvmet-host 组件凭据由 enroll 按能力派生，均不在本模块。"""

from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..agent_client import AgentAPIError, AgentClient, AgentConfig
from ..auth import authenticate, authorize_request
from ..models import (
    CreateAgentRequest,
    CreateCdLunRequest,
    CreateDiskLunRequest,
    MasterTagRequest,
    ProbeAgentRequest,
    UpdateAgentRequest,
)
from ..stores import agents, master_tags, record, store
from ..utils import WORKER_ID_RE, canonical_os, canonical_os_version, client_host, worker_disks

router = APIRouter(dependencies=[Depends(authenticate), Depends(authorize_request)])


@router.get("/agents")
def list_agents(live: bool = True):
    return agents.list_public(live=live)


@router.post("/agents", status_code=201)
def create_agent(req: CreateAgentRequest, request: Request):
    """注册新 Agent：写入 agents.yml（重复 id 返回 409）。
    base_url 须 http(s):// 开头；
    role 决定磁盘/光驱角色；storager_ip 为数据面地址（缺省用 base_url 主机名）。"""
    agent_id = req.id.strip().lower()
    if not WORKER_ID_RE.match(agent_id):  # Agent id 与 worker id 同一命名规则
        raise HTTPException(400, f"invalid agent id: {req.id}")
    base_url = req.base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(400, "base_url must start with http:// or https://")
    storager_ip = req.storager_ip.strip() if req.storager_ip else None
    try:
        agents.get(agent_id)
        raise HTTPException(409, f"agent already exists: {agent_id}")
    except KeyError:
        pass

    with store.locked():
        agents.add(
            agent_id,
            base_url,
            role_disk=req.role.disk,
            role_cd=req.role.cd,
            storager_ip=storager_ip,
            enabled=req.enabled,
            tags=tuple(t.strip() for t in req.tags if t.strip()),
            remark=req.remark.strip(),
        )
    record("agent.register", "ok", agent=agent_id, client=client_host(request))
    return agents.get(agent_id).public_dict()


@router.put("/agents/{agent_id}")
def update_agent(agent_id: str, req: UpdateAgentRequest, request: Request):
    """更新已有 Agent：覆盖 agents.yml 中对应条目（id 不可改，走路径参数）。
    enabled=false 停用（排空语义：不接新增盘，存量盘照常服务）。
    base_url 归属：自动登记（tags 含 auto）的节点其地址由节点声明管理——
    控制面侧改动会在下次 renew 被声明值冲掉，故拒绝并指路；手工登记节点仍可改。"""
    agent_id = agent_id.strip().lower()
    base_url = req.base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(400, "base_url must start with http:// or https://")
    storager_ip = req.storager_ip.strip() if req.storager_ip else None
    try:
        current = agents.get(agent_id)
    except KeyError:
        raise HTTPException(404, f"agent not found: {agent_id}") from None
    if base_url != current.base_url and "auto" in current.tags:
        raise HTTPException(
            409,
            f"base_url is managed by the node declaration (storager/kurrent.yaml "
            f"spec.agent.advertiseUrl): edit the declaration on the node and re-run "
            f"'kurrent join'; renew will sync it (current: {current.base_url})",
        )

    with store.locked():
        agents.update(
            agent_id,
            base_url,
            role_disk=req.role.disk,
            role_cd=req.role.cd,
            storager_ip=storager_ip,
            enabled=req.enabled,
            tags=tuple(t.strip() for t in req.tags if t.strip()),
            remark=req.remark.strip(),
        )
    record("agent.update", "ok", agent=agent_id, client=client_host(request))
    return agents.get(agent_id).public_dict()


@router.delete("/agents/{agent_id}")
def delete_agent(agent_id: str, request: Request):
    """删除 Agent 台账：移除 agents.yml 条目 + 清理其母盘标签（控制面台账）。
    仅台账删除：不触碰节点上的证书/盘文件。
    语义（A2 裁定推论）：删除是**软删除**——节点仍带声明在跑，下次 renew 会按声明
    自动重新登记；真正下线一台节点须在节点侧操作（停容器/撤声明）。
    响应附 disks_hosted / affected_workers（仅告知，不拦截）：登记删除后，这些 Worker
    挂在该节点上的盘在节点重新登记前无法投影引导地址（登记即认知）。"""
    agent_id = agent_id.strip().lower()
    _agent_or_404(agent_id)
    with store.locked():
        agents.delete(agent_id)
        affected_workers, disks_hosted = _workers_hosted_by(agent_id)
    with master_tags.locked():
        data = master_tags.load()
        removed = bool(data.get("masters", {}).pop(agent_id, None))
        if removed:
            master_tags.save(data)
    record("agent.delete", "ok", agent=agent_id, client=client_host(request),
           disks_hosted=disks_hosted, affected_workers=affected_workers)
    return {"deleted": agent_id, "master_tags_removed": removed,
            "disks_hosted": disks_hosted, "affected_workers": affected_workers}


def _workers_hosted_by(agent_id: str) -> tuple[list[str], int]:
    """扫描 workers.yml：仍把系统盘/光驱挂在该 Agent 上的 Worker 列表与盘数
    （删除响应告知用，派生自台账，不另存状态）。"""
    data = store.load_workers()
    affected: list[str] = []
    count = 0
    for worker_id, record_ in data["workers"].items():
        hosted = [d for d in worker_disks(record_) if d.get("agent") == agent_id]
        cd = record_.get("cd")
        if cd and cd.get("agent") == agent_id:
            hosted.append(cd)
        if hosted:
            affected.append(worker_id)
            count += len(hosted)
    return affected, count


@router.post("/agents/probe")
def probe_agent(req: ProbeAgentRequest, request: Request):
    """探测 Agent：调 /healthz + /capabilities，自动推导注册参数预览（不写任何文件）。
    推导规则：role.disk 恒真（Agent 即存储节点）、role.cd 取 capabilities.cd；
    tags = [storage]（纯人工标签——后端类型不进 tags，单独以 backend 字段返回，
    由 /boot-vars 实时查 /capabilities 判定）；
    storager_ip 缺省回退 base_url 主机名。"""
    base_url = req.base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(400, "base_url must start with http:// or https://")

    # 临时 AgentConfig 探测（不落盘，不进入注册表）
    probe_cfg = AgentConfig(
        id="_probe",
        base_url=base_url,
        role_disk=True,
        role_cd=False,
    )
    client = AgentClient(probe_cfg, agents.timeout)
    try:
        client.healthz()
    except Exception as exc:
        record("agent.probe", "failed", agent=base_url, client=client_host(request), error=str(exc))
        raise HTTPException(502, f"agent unreachable: {exc}") from exc
    try:
        caps = client.capabilities()
    except AgentAPIError as exc:
        record("agent.probe", "failed", agent=base_url, client=client_host(request), error=exc.detail)
        raise HTTPException(502, {"agent": base_url, "error": exc.detail}) from exc

    backend = str(caps.get("backend", "stgt")).lower()
    record("agent.probe", "ok", agent=base_url, client=client_host(request), backend=backend)
    return {
        "base_url": base_url,
        "role": {"disk": True, "cd": bool(caps.get("cd", False))},
        "tags": ["storage"],
        "storager_ip": urlparse(base_url).hostname or base_url,
        "enabled": True,
        "backend": backend,
        "fs_type": caps.get("fs_type", ""),
        "base_nqn": caps.get("base_nqn", ""),
        "clone": caps.get("clone", ""),
        "empty_disk": caps.get("empty_disk", ""),
        "persistent": caps.get("persistent", ""),
    }


@router.get("/agents/{agent_id}/luns")
def list_agent_luns(agent_id: str, request: Request):
    """列出指定 Agent 上的全部 iSCSI target/LUN。"""
    client = _agent_client_or_404(agent_id)
    try:
        result = client.list_luns()
    except AgentAPIError as exc:
        record("lun.list", "failed", agent=agent_id, client=client_host(request), error=exc.detail)
        raise HTTPException(exc.status_code, {"agent": exc.agent_id, "error": exc.detail}) from exc
    record("lun.list", "ok", agent=agent_id, client=client_host(request), count=len(result))
    return result


@router.post("/agents/{agent_id}/luns/disk", status_code=201)
def create_agent_disk_lun(agent_id: str, req: CreateDiskLunRequest, request: Request):
    """在指定 Agent 上创建磁盘 LUN：传 master 走母盘克隆，传 size 建空白盘。"""
    if not req.master and not req.size:
        raise HTTPException(400, "need master (clone) or size (empty disk)")
    agent = _agent_or_404(agent_id)
    if not agent.role_disk:
        raise HTTPException(400, f"agent {agent_id} not configured for disk role")
    if not agent.enabled:
        # enabled = 排空语义（只拦新增，不碰存量：已有 LUN 照常服务）
        raise HTTPException(409, f"agent {agent_id} disabled: not accepting new LUNs")
    client = agents.client(agent)
    try:
        result = client.create_disk(req.iqn, req.filename or "", master=req.master, size=req.size)
    except AgentAPIError as exc:
        record("lun.create_disk", "failed", agent=agent_id, client=client_host(request), iqn=req.iqn, error=exc.detail)
        raise HTTPException(exc.status_code, {"agent": exc.agent_id, "error": exc.detail}) from exc
    record("lun.create_disk", "ok", agent=agent_id, client=client_host(request),
           iqn=result.get("iqn", req.iqn), backing=result.get("backing"))
    return result


@router.post("/agents/{agent_id}/luns/cd", status_code=201)
def create_agent_cd_lun(agent_id: str, req: CreateCdLunRequest, request: Request):
    """在指定 Agent 上创建 CD（ISO 虚拟光驱）LUN，仅 stgt 后端支持。"""
    agent = _agent_or_404(agent_id)
    if not agent.role_cd:
        raise HTTPException(400, f"agent {agent_id} not configured for cd role")
    if not agent.enabled:
        # enabled = 排空语义（只拦新增，不碰存量）
        raise HTTPException(409, f"agent {agent_id} disabled: not accepting new LUNs")
    client = agents.client(agent)
    try:
        result = client.create_cd(req.iso, req.iqn or "")
    except AgentAPIError as exc:
        record("lun.create_cd", "failed", agent=agent_id, client=client_host(request), iso=req.iso, error=exc.detail)
        raise HTTPException(exc.status_code, {"agent": exc.agent_id, "error": exc.detail}) from exc
    record("lun.create_cd", "ok", agent=agent_id, client=client_host(request),
           iqn=result.get("iqn", req.iqn), iso=req.iso)
    return result


@router.delete("/agents/{agent_id}/luns")
def delete_agent_lun(
    agent_id: str,
    request: Request,
    iqn: str = Query(..., description="IQN of the target to delete"),
    delete_file: bool = Query(False, description="Delete the backing .img/.iso file as well."),
    ignore_missing: bool = Query(False, description="Ignore 404 from Agent while deleting the target."),
):
    """删除指定 Agent 上的一个 LUN/target。"""
    client = _agent_client_or_404(agent_id)
    try:
        result = client.delete_lun(iqn, delete_file=delete_file)
    except AgentAPIError as exc:
        if ignore_missing and exc.status_code == 404:
            record("lun.delete", "ok", agent=agent_id, client=client_host(request), iqn=iqn,
                   delete_file=delete_file, ignored_missing=True)
            return {"deleted": iqn, "delete_file": delete_file, "ignored_missing": True}
        record("lun.delete", "failed", agent=agent_id, client=client_host(request), iqn=iqn,
               delete_file=delete_file, error=exc.detail)
        raise HTTPException(exc.status_code, {"agent": exc.agent_id, "error": exc.detail}) from exc
    record("lun.delete", "ok", agent=agent_id, client=client_host(request), iqn=iqn, delete_file=delete_file)
    return result


@router.post("/agents/{agent_id}/luns/scan")
def scan_agent_luns(agent_id: str, request: Request):
    """触发 Agent 扫描镜像目录，为缺失文件重建 target（文件即真相）。"""
    client = _agent_client_or_404(agent_id)
    try:
        result = client.scan()
    except AgentAPIError as exc:
        record("lun.scan", "failed", agent=agent_id, client=client_host(request), error=exc.detail)
        raise HTTPException(exc.status_code, {"agent": exc.agent_id, "error": exc.detail}) from exc
    record("lun.scan", "ok", agent=agent_id, client=client_host(request),
           created=len(result.get("created", [])), skipped=len(result.get("skipped", [])))
    return result


@router.get("/masters")
def list_masters(request: Request):
    """聚合列出全部启用磁盘角色 Agent 上的母盘（后台扫描缓存），供 WebUI 克隆选盘。
    合并控制面登记的母盘标签（os/os_version，备注性质）；单台 Agent 失败不阻塞整体。"""
    results: list[dict[str, Any]] = []
    total = failed = 0
    for agent in agents.load():
        if not (agent.enabled and agent.role_disk and agent.base_url):
            continue
        total += 1
        entry: dict[str, Any] = {
            "agent": agent.id,
            "storager_ip": agents.storager_ip_for(agent.id),
        }
        try:
            payload = agents.client(agent).list_masters()
        except AgentAPIError as exc:
            failed += 1
            record("master.list", "failed", agent=agent.id, client=client_host(request), error=exc.detail)
            entry["masters"] = []
            entry["error"] = exc.detail
            results.append(entry)
            continue
        masters = payload.get("masters", []) if isinstance(payload, dict) else []
        record("master.list", "ok", agent=agent.id, client=client_host(request), count=len(masters))
        entry["masters"] = _merge_master_tags(agent.id, masters)
        results.append(entry)
    if total > 0 and failed == total:
        raise HTTPException(502, {"agents": results, "error": "all agents failed"})
    return {"agents": results}


@router.put("/agents/{agent_id}/masters/{master_name}/tag")
def set_master_tag(agent_id: str, master_name: str, req: MasterTagRequest, request: Request):
    """登记母盘标签（控制面台账，备注性质）：os 为系统备注，os_version 可空（'' = 无版本）。
    标签不校验母盘存在性（Agent 可离线，台账即权威）；/masters 聚合时合并展示。"""
    agent_id = agent_id.strip().lower()
    master_name = master_name.strip()
    if not master_name:
        raise HTTPException(400, "invalid master name")
    _agent_or_404(agent_id)
    os_name = canonical_os(req.os)
    os_version = canonical_os_version(req.os_version)
    remark = req.remark.strip()
    with master_tags.locked():
        data = master_tags.load()
        agent_tags = data["masters"].setdefault(agent_id, {})
        agent_tags[master_name] = {"os": os_name, "os_version": os_version,
                                    "remark": remark}
        master_tags.save(data)
    record("master.tag", "ok", agent=agent_id, name=master_name, os=os_name,
           os_version=os_version, remark=remark, client=client_host(request))
    return {"agent": agent_id, "name": master_name, "os": os_name,
            "os_version": os_version, "remark": remark}


@router.delete("/agents/{agent_id}/masters/{master_name}/tag")
def clear_master_tag(agent_id: str, master_name: str, request: Request):
    """清除母盘标签（控制面台账）：母盘恢复未登记状态（克隆选盘不显示 os/os_version）。"""
    agent_id = agent_id.strip().lower()
    master_name = master_name.strip()
    if not master_name:
        raise HTTPException(400, "invalid master name")
    _agent_or_404(agent_id)
    with master_tags.locked():
        data = master_tags.load()
        agent_tags = data["masters"].get(agent_id)
        removed = bool(agent_tags and master_name in agent_tags)
        if removed:
            del agent_tags[master_name]
            if not agent_tags:
                del data["masters"][agent_id]
            master_tags.save(data)
    record("master.tag.clear", "ok", agent=agent_id, name=master_name,
           removed=removed, client=client_host(request))
    return {"agent": agent_id, "name": master_name, "removed": removed}


def _agent_or_404(agent_id: str):
    try:
        return agents.get(agent_id)
    except KeyError:
        raise HTTPException(404, f"agent not found: {agent_id}") from None


def _merge_master_tags(agent_id: str, masters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并控制面登记的母盘标签（备注性质）：有登记的条目附加 os/os_version/remark；
    未登记不附加。"""
    with master_tags.locked():
        agent_tags = master_tags.load()["masters"].get(agent_id, {})
    merged = []
    for m in masters:
        tag = agent_tags.get(m.get("name", ""))
        if tag:
            merged.append({**m, "os": tag.get("os", ""),
                           "os_version": tag.get("os_version", ""),
                           "remark": tag.get("remark", "")})
        else:
            merged.append(m)
    return merged


def _agent_client_or_404(agent_id: str):
    return agents.client(_agent_or_404(agent_id))
