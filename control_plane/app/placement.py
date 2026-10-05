"""系统盘放置策略：把「哪些 Worker 要盘」展开成「每个 Worker 落到哪个 Agent」。

放置是**系统决策**：调用方声明意图（哪些 Worker + 哪块母盘）与约束（可用节点范围、
逐项钉死），位置由控制面算。策略住在这里而不是各客户端，理由有二：

1. WebUI / CLI / 未来任何调用方不必各写一遍，行为不会漂移；
2. 预览（dry_run）与执行共用同一份实现，「看到的计划」即「要执行的计划」。

策略：候选 = 磁盘角色 ∧ 启用（非排空）∧ 有 base_url ∧ 数据面就绪 ∧
在显式 candidates 内 ∧（克隆母盘时）母盘在该节点本地存在；
逐个 Worker 取「现有盘数最少」的候选（均摊），显式 pin 覆盖自动放置。
"""

from dataclasses import dataclass, field
from typing import Any

from .agent_client import AgentConfig
from .scheduler import AgentRegistry


class PlacementError(Exception):
    """无可用候选，或 pin 指向不可用节点。detail 给人看，excluded 逐条说明谁被排除、为什么。"""

    def __init__(self, detail: str, excluded: list[dict[str, str]] | None = None):
        super().__init__(detail)
        self.detail = detail
        self.excluded = excluded or []


@dataclass(frozen=True)
class Candidate:
    """通过校验的候选节点：caps 供调用方复用（免去执行时重复 healthz/capabilities）。"""

    agent: AgentConfig
    caps: dict[str, Any]
    load: int


@dataclass
class Plan:
    assignments: dict[str, str] = field(default_factory=dict)          # worker_id -> agent_id
    candidates: dict[str, Candidate] = field(default_factory=dict)     # agent_id -> Candidate
    warnings: list[dict[str, str]] = field(default_factory=list)       # 被排除的候选与原因


def resolve(
    registry: AgentRegistry,
    worker_ids: list[str],
    *,
    disk_type: str,
    master: str | None = None,
    candidates: list[str] | None = None,
    pins: dict[str, str] | None = None,
) -> Plan:
    """把 worker_ids 展开成逐个的节点分配。任一 worker 无落点时抛 PlacementError。

    candidates 只约束**自动放置**；pin 是显式覆盖，不要求在 candidates 内（但须自身可用）。
    """
    pins = pins or {}
    restrict = set(candidates) if candidates else None
    pinned_ids = set(pins.values())

    validated: dict[str, Candidate] = {}
    warnings: list[dict[str, str]] = []
    for agent in registry.load():
        # candidates 只约束自动放置；pin 是显式覆盖，被 pin 的节点即使不在 candidates 内也要校验
        if restrict is not None and agent.id not in restrict and agent.id not in pinned_ids:
            continue
        candidate, reason = _probe(registry, agent, disk_type=disk_type, master=master)
        if candidate is None:
            warnings.append({"agent": agent.id, "reason": reason})
            continue
        validated[agent.id] = candidate

    auto_pool = [c for c in validated.values() if restrict is None or c.agent.id in restrict]

    load = {agent_id: c.load for agent_id, c in validated.items()}
    plan = Plan(candidates=validated, warnings=warnings)

    for worker_id in worker_ids:
        pin = pins.get(worker_id)
        if pin:
            if pin not in validated:
                raise PlacementError(
                    f"pinned agent {pin} is not usable for {worker_id}", warnings)
            plan.assignments[worker_id] = pin
            load[pin] += 1
            continue
        if not auto_pool:
            raise PlacementError(_no_candidate_detail(disk_type, master), warnings)
        # 最少盘优先；同负载按注册顺序（稳定，便于测试与复现）
        best = auto_pool[0]
        for candidate in auto_pool[1:]:
            if load[candidate.agent.id] < load[best.agent.id]:
                best = candidate
        plan.assignments[worker_id] = best.agent.id
        load[best.agent.id] += 1

    return plan


def _probe(
    registry: AgentRegistry,
    agent: AgentConfig,
    *,
    disk_type: str,
    master: str | None,
) -> tuple[Candidate | None, str]:
    """单个节点的可用性判定。返回 (Candidate, "") 或 (None, 排除原因)。"""
    if not agent.role_disk:
        return None, "not a disk-role agent"
    if not agent.enabled:
        return None, "disabled (draining: not accepting new disks)"
    if not agent.base_url:
        return None, "missing base_url"
    client = registry.client(agent)
    try:
        health = client.healthz()
    except Exception as exc:
        return None, f"unreachable: {exc}"
    # agent 进程活着但后端不可用（nvmet-host 不可达 / configfs 未就绪）→ 不放新盘
    if health.get("backend") and health.get("backend_ready") is False:
        return None, f"data plane not ready: {health.get('backend_detail', 'backend not ready')}"
    try:
        caps = client.capabilities()
    except Exception as exc:
        return None, f"capabilities unavailable: {exc}"
    if "base_nqn" not in caps:
        return None, "capabilities missing base_nqn"
    if disk_type == "master":
        try:
            payload = client.list_masters()
        except Exception as exc:
            return None, f"master list unavailable: {exc}"
        names = {str(m.get("name", "")) for m in (payload or {}).get("masters", [])}
        if master not in names:
            return None, f"does not have master {master}"
    try:
        load = len(client.list_luns())
    except Exception as exc:
        return None, f"lun list unavailable: {exc}"
    return Candidate(agent=agent, caps=caps, load=load), ""


def _no_candidate_detail(disk_type: str, master: str | None) -> str:
    if disk_type == "master":
        return f"no eligible disk agent has master {master}"
    return "no eligible disk agent available"
