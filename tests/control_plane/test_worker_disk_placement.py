"""系统盘放置：候选筛选（角色/排空/数据面就绪/candidates 限定/母盘本地存在）、
最少盘优先均摊、pins 覆盖、dry_run 与执行同一份计划、targets 向后兼容。

放置是系统决策（见 control_plane/app/placement.py）：调用方只声明「哪些 Worker + 哪块母盘」，
位置由控制面算，预览与执行共用同一实现。
"""

from types import SimpleNamespace

import pytest

from control_plane.app.stores import agents as agents_store

MASTER = "ubuntu-24.04_tpl_.img"


class AgentSpec:
    """单个 fake Agent 的行为描述。"""

    def __init__(self, *, disk=True, cd=False, enabled=True, health=None,
                 masters=(MASTER,), luns=0, caps=None):
        self.disk = disk
        self.cd = cd
        self.enabled = enabled
        self.health = health or {"status": "ok"}
        self.masters = list(masters)
        self.luns = luns
        self.caps = caps or {"base_nqn": "nqn.2026-07.com.test", "cd": False, "backend": "stgt"}


@pytest.fixture()
def fake_agents(monkeypatch):
    """按 agent id 返回定制 fake client，并统计每个方法被调用的 (agent, 次数)。

    用法：register(client, auth_headers, fake_agents, "ag-01", AgentSpec(luns=2))
    """
    specs: dict[str, AgentSpec] = {}
    calls: dict[str, list[str]] = {"healthz": [], "capabilities": [], "list_masters": [], "list_luns": []}
    cache: dict[str, SimpleNamespace] = {}

    def _client(agent):
        spec = specs[agent.id]
        if agent.id not in cache:
            cache[agent.id] = SimpleNamespace(
                healthz=lambda: (calls["healthz"].append(agent.id), spec.health)[1],
                capabilities=lambda: (calls["capabilities"].append(agent.id), spec.caps)[1],
                list_masters=lambda: (calls["list_masters"].append(agent.id),
                                      {"masters": [{"name": n} for n in spec.masters]})[1],
                list_luns=lambda: (calls["list_luns"].append(agent.id),
                                   [{"nqn": f"nqn.2026-07.com.test:x{i}"} for i in range(spec.luns)])[1],
                create_disk=lambda iqn, filename, **kw: {"iqn": iqn, "backing": f"/srv/iscsi/{filename}"},
                create_cd=lambda iso, iqn: {"iqn": iqn, "backing": iso},
                delete_lun=lambda iqn, delete_file=False: {"deleted": iqn},
                scan=lambda: {"created": [], "skipped": []},
                set_credential=lambda worker_id, secret, sub_nqns, host_nqns: {"worker_id": worker_id},
            )
        return cache[agent.id]

    monkeypatch.setattr(agents_store, "client", _client)
    return {"specs": specs, "calls": calls, "client": _client}


def register(client, auth_headers, fake_agents, agent_id, spec: AgentSpec):
    fake_agents["specs"][agent_id] = spec
    res = client.post("/agents", json={
        "id": agent_id, "base_url": f"http://{agent_id}:8000",
        "role": {"disk": spec.disk, "cd": spec.cd}, "enabled": spec.enabled,
    }, headers=auth_headers)
    assert res.status_code == 201, res.text
    return res.json()


def make_workers(client, auth_headers, ids):
    for wid in ids:
        assert client.post("/workers", json={"worker_id": wid}, headers=auth_headers).status_code == 201


def batch(client, auth_headers, **body):
    payload = {"type": "master", "os": "ubuntu", "name": MASTER}
    payload.update(body)
    return client.post("/workers/luns/disk/batch", json=payload, headers=auth_headers)


class TestAutoPlacement:
    def test_balances_by_existing_load(self, client, auth_headers, fake_agents):
        """最少盘优先：ag-01 已有 2 块、ag-02 空 → 3 个 Worker 落成 ag-02, ag-02, ag-01。"""
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(luns=2))
        register(client, auth_headers, fake_agents, "ag-02", AgentSpec(luns=0))
        make_workers(client, auth_headers, ["w-01", "w-02", "w-03"])

        res = batch(client, auth_headers, workers=["w-01", "w-02", "w-03"])
        assert res.status_code == 200, res.text
        placed = [item["agent"] for item in res.json()["succeeded"]]
        assert placed == ["ag-02", "ag-02", "ag-01"]

    def test_candidates_restrict_auto_placement(self, client, auth_headers, fake_agents):
        """candidates 只让 ag-02 参与 → 全部落 ag-02（未列入的节点连探测都跳过）。"""
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(luns=0))
        register(client, auth_headers, fake_agents, "ag-02", AgentSpec(luns=0))
        make_workers(client, auth_headers, ["w-01", "w-02"])

        res = batch(client, auth_headers, workers=["w-01", "w-02"],
                    placement={"candidates": ["ag-02"]})
        assert [i["agent"] for i in res.json()["succeeded"]] == ["ag-02", "ag-02"]
        assert fake_agents["calls"]["healthz"] == ["ag-02"]

    def test_master_missing_on_node_is_excluded(self, client, auth_headers, fake_agents):
        """母盘必须在该节点本地存在（克隆在节点本地完成）→ 缺母盘的节点被排除并给出原因。"""
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(masters=[MASTER]))
        register(client, auth_headers, fake_agents, "ag-02", AgentSpec(masters=["other_tpl_.img"]))
        make_workers(client, auth_headers, ["w-01", "w-02"])

        res = batch(client, auth_headers, workers=["w-01", "w-02"])
        body = res.json()
        assert [i["agent"] for i in body["succeeded"]] == ["ag-01", "ag-01"]

    def test_empty_disk_type_ignores_master_check(self, client, auth_headers, fake_agents):
        """空白盘不需要母盘 → 缺母盘的节点照样是候选。"""
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(masters=[]))
        make_workers(client, auth_headers, ["w-01"])

        res = batch(client, auth_headers, type="empty", size="40G", name=None, workers=["w-01"])
        assert res.status_code == 200, res.text
        assert [i["agent"] for i in res.json()["succeeded"]] == ["ag-01"]

    def test_no_eligible_agent_returns_503_with_reasons(self, client, auth_headers, fake_agents):
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(masters=["other_tpl_.img"]))
        make_workers(client, auth_headers, ["w-01"])

        res = batch(client, auth_headers, workers=["w-01"])
        assert res.status_code == 503
        detail = res.json()["detail"]
        assert detail["error"] == f"no eligible disk agent has master {MASTER}"
        assert detail["excluded"] == [{"agent": "ag-01", "reason": f"does not have master {MASTER}"}]

    def test_excluded_reasons_for_role_and_drain(self, client, auth_headers, fake_agents):
        register(client, auth_headers, fake_agents, "ag-cd", AgentSpec(disk=False, cd=True))
        register(client, auth_headers, fake_agents, "ag-off", AgentSpec(enabled=False))
        make_workers(client, auth_headers, ["w-01"])

        res = batch(client, auth_headers, workers=["w-01"])
        assert res.status_code == 503
        reasons = {e["agent"]: e["reason"] for e in res.json()["detail"]["excluded"]}
        assert reasons["ag-cd"] == "not a disk-role agent"
        assert "draining" in reasons["ag-off"]

    def test_degraded_backend_excluded(self, client, auth_headers, fake_agents):
        """agent 进程活着但数据面未就绪（backend_ready=false）→ 不放新盘。"""
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(
            health={"status": "ok", "backend": "nvmet", "backend_ready": False,
                    "backend_detail": "configfs not mounted"}))
        make_workers(client, auth_headers, ["w-01"])

        res = batch(client, auth_headers, workers=["w-01"])
        assert res.status_code == 503
        assert res.json()["detail"]["excluded"][0]["reason"] == \
            "data plane not ready: configfs not mounted"


class TestPins:
    def test_pin_overrides_auto_placement(self, client, auth_headers, fake_agents):
        """pin 是显式覆盖：w-02 钉到 ag-02（自动放置本来会给 ag-01）。"""
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(luns=0))
        register(client, auth_headers, fake_agents, "ag-02", AgentSpec(luns=0))
        make_workers(client, auth_headers, ["w-01", "w-02"])

        res = batch(client, auth_headers, workers=["w-01", "w-02"],
                    placement={"pins": {"w-02": "ag-02"}})
        placed = {i["worker_id"]: i["agent"] for i in res.json()["succeeded"]}
        assert placed["w-02"] == "ag-02"
        assert placed["w-01"] == "ag-01"

    def test_pin_outside_candidates_is_still_honoured(self, client, auth_headers, fake_agents):
        """candidates 只约束自动放置；被 pin 的节点即使不在 candidates 内也要可用。"""
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(luns=0))
        register(client, auth_headers, fake_agents, "ag-02", AgentSpec(luns=0))
        make_workers(client, auth_headers, ["w-01"])

        res = batch(client, auth_headers, workers=["w-01"],
                    placement={"candidates": ["ag-01"], "pins": {"w-01": "ag-02"}})
        assert [i["agent"] for i in res.json()["succeeded"]] == ["ag-02"]

    def test_pin_to_unusable_node_is_503(self, client, auth_headers, fake_agents):
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(luns=0))
        register(client, auth_headers, fake_agents, "ag-02", AgentSpec(enabled=False))
        make_workers(client, auth_headers, ["w-01"])

        res = batch(client, auth_headers, workers=["w-01"], placement={"pins": {"w-01": "ag-02"}})
        assert res.status_code == 503
        assert "pinned agent ag-02 is not usable for w-01" in res.json()["detail"]["error"]
        assert [e["agent"] for e in res.json()["detail"]["excluded"]] == ["ag-02"]


class TestDryRun:
    def test_dry_run_plan_matches_execution_and_creates_nothing(self, client, auth_headers, fake_agents):
        """预览与执行是同一份实现：dry_run 给出的落点必须与随后执行的结果一致，且预览不落盘。"""
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(luns=1))
        register(client, auth_headers, fake_agents, "ag-02", AgentSpec(luns=0))
        make_workers(client, auth_headers, ["w-01", "w-02", "w-03"])

        preview = batch(client, auth_headers, workers=["w-01", "w-02", "w-03"], dry_run=True)
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["dry_run"] is True
        assert [row["action"] for row in body["plan"]] == ["create", "create", "create"]
        planned = {row["worker_id"]: row["agent"] for row in body["plan"]}

        # 预览不建盘
        for wid in ("w-01", "w-02", "w-03"):
            assert client.get(f"/workers/{wid}", headers=auth_headers).json()["disks"] == []

        res = batch(client, auth_headers, workers=["w-01", "w-02", "w-03"])
        executed = {i["worker_id"]: i["agent"] for i in res.json()["succeeded"]}
        assert executed == planned

    def test_dry_run_reports_skip_and_missing(self, client, auth_headers, fake_agents):
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(luns=0))
        make_workers(client, auth_headers, ["w-01"])
        client.post("/workers/w-01/luns/disk", json={
            "type": "empty", "os": "ubuntu", "size": "40G", "disk_agent": "ag-01",
        }, headers=auth_headers)

        res = batch(client, auth_headers, type="empty", size="40G", name=None,
                    workers=["w-01", "w-gone"], dry_run=True)
        assert res.status_code == 200, res.text
        plan = res.json()["plan"]
        assert (plan[0]["worker_id"], plan[0]["action"]) == ("w-01", "skip")
        assert (plan[1]["worker_id"], plan[1]["action"]) == ("w-gone", "error")


class TestBackwardCompatibility:
    def test_explicit_targets_still_work(self, client, auth_headers, fake_agents):
        """targets 是原有调用方式（WebUI 现在就在用），必须原样可用。"""
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(luns=0))
        make_workers(client, auth_headers, ["w-01"])

        res = batch(client, auth_headers, targets=[{"worker_id": "w-01", "agent": "ag-01"}])
        assert res.status_code == 200, res.text
        assert [i["agent"] for i in res.json()["succeeded"]] == ["ag-01"]

    def test_targets_bypass_placement(self, client, auth_headers, fake_agents):
        """显式点名不走放置：节点没有母盘也照样提交（由 agent 决定成败）。"""
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(masters=[]))
        make_workers(client, auth_headers, ["w-01"])

        res = batch(client, auth_headers, targets=[{"worker_id": "w-01", "agent": "ag-01"}])
        assert res.status_code == 200, res.text
        assert len(res.json()["succeeded"]) == 1

    def test_targets_and_workers_are_mutually_exclusive(self, client, auth_headers, fake_agents):
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(luns=0))
        make_workers(client, auth_headers, ["w-01"])

        res = batch(client, auth_headers, workers=["w-01"],
                    targets=[{"worker_id": "w-01", "agent": "ag-01"}])
        assert res.status_code == 400
        assert "not both" in res.json()["detail"]

    def test_neither_targets_nor_workers_is_400(self, client, auth_headers, fake_agents):
        res = batch(client, auth_headers)
        assert res.status_code == 400
        assert res.json()["detail"] == "workers or targets must not be empty"

    def test_agent_probed_once_per_request(self, client, auth_headers, fake_agents):
        """放置已探过的 agent 在执行阶段复用（缓存），不按 Worker 数重复 healthz。"""
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(luns=0))
        make_workers(client, auth_headers, ["w-01", "w-02", "w-03", "w-04"])

        res = batch(client, auth_headers, workers=["w-01", "w-02", "w-03", "w-04"])
        assert len(res.json()["succeeded"]) == 4
        assert fake_agents["calls"]["healthz"] == ["ag-01"]


class TestSingleDiskPlacement:
    def test_auto_pick_skips_node_without_master(self, client, auth_headers, fake_agents):
        """单盘不点名节点时同样走放置：缺母盘的节点不会被选中（原先会选中并在 agent 侧失败）。"""
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(masters=["other_tpl_.img"]))
        register(client, auth_headers, fake_agents, "ag-02", AgentSpec(masters=[MASTER]))
        make_workers(client, auth_headers, ["w-01"])

        res = client.post("/workers/w-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": MASTER,
        }, headers=auth_headers)
        assert res.status_code == 201, res.text
        assert res.json()["disks"][0]["agent"] == "ag-02"

    def test_auto_pick_without_master_anywhere_is_503(self, client, auth_headers, fake_agents):
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(masters=[]))
        make_workers(client, auth_headers, ["w-01"])

        res = client.post("/workers/w-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": MASTER,
        }, headers=auth_headers)
        assert res.status_code == 503
        assert res.json()["detail"]["error"] == f"no eligible disk agent has master {MASTER}"

    def test_explicit_disk_agent_still_wins(self, client, auth_headers, fake_agents):
        """显式 disk_agent 不走放置（保持原语义：点名即用，不做母盘存在性预检）。"""
        register(client, auth_headers, fake_agents, "ag-01", AgentSpec(luns=0))
        make_workers(client, auth_headers, ["w-01"])

        res = client.post("/workers/w-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "master-not-listed",
            "disk_agent": "ag-01",
        }, headers=auth_headers)
        assert res.status_code == 201, res.text
