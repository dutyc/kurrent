"""Agent 域（Bearer 鉴权）：注册/更新/探测/LUN/母盘聚合（AgentClient 全 mock，无网络）。"""

import pytest

from control_plane.app.agent_client import AgentClient


@pytest.fixture()
def agent_payload():
    return {
        "id": "a1b2c3d4e5f6",
        "base_url": "http://a1b2c3d4e5f6:8000",
        "role": {"disk": True, "cd": True},
        "tags": ["storage", "stgt"],
        "enabled": True,
        "remark": "主存储节点",
    }


class TestAgentCrud:
    """Agent 注册/列表/更新。"""

    def test_create_agent(self, client, auth_headers, agent_payload):
        res = client.post("/agents", json=agent_payload, headers=auth_headers)
        assert res.status_code == 201
        body = res.json()
        assert body["id"] == "a1b2c3d4e5f6"
        assert body["role"] == {"disk": True, "cd": True}
        assert body["storager_ip"] is None  # 未显式配置 → 缺省空；回退 base_url 主机名仅在 boot-vars 投影时
        assert body["remark"] == "主存储节点"  # 备注（人类识别用，展示性质）
        assert "token" not in body  # 身份由 mTLS 组件证书承载，注册表无 token 字段
        # 列表（live=false：不做健康探测，返回纯台账）
        listing = client.get("/agents", params={"live": "false"}, headers=auth_headers).json()
        assert listing == [body]

    def test_create_duplicate_409(self, client, auth_headers, agent_payload):
        client.post("/agents", json=agent_payload, headers=auth_headers)
        res = client.post("/agents", json=agent_payload, headers=auth_headers)
        assert res.status_code == 409

    @pytest.mark.parametrize("bad_id", [
        "Bad ID!", "storage-01", "3F2A9C1D8B7E", "3f2a9c1d8b", "3f2a9c1d8b7e0",
    ])
    def test_create_invalid_id_400(self, client, auth_headers, agent_payload, bad_id):
        agent_payload["id"] = bad_id
        res = client.post("/agents", json=agent_payload, headers=auth_headers)
        assert res.status_code == 400
        assert "12 lowercase hex" in res.json()["detail"]

    def test_create_invalid_base_url_400(self, client, auth_headers, agent_payload):
        agent_payload["base_url"] = "a1b2c3d4e5f6:8000"  # 缺 scheme
        res = client.post("/agents", json=agent_payload, headers=auth_headers)
        assert res.status_code == 400

    def test_update_agent(self, client, auth_headers, agent_payload):
        client.post("/agents", json=agent_payload, headers=auth_headers)
        res = client.put("/agents/a1b2c3d4e5f6", json={
            "base_url": "http://a1b2c3d4e5f6-new:9000",
            "role": {"disk": True, "cd": False},
            "tags": ["storage", "lio"],
            "enabled": False,
            "remark": "备存储节点",
        }, headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert body["base_url"] == "http://a1b2c3d4e5f6-new:9000"
        assert body["role"] == {"disk": True, "cd": False}
        assert body["tags"] == ["storage", "lio"]
        assert body["enabled"] is False
        assert body["storager_ip"] is None
        assert body["remark"] == "备存储节点"

    def test_update_base_url_refused_for_auto_registered(self, client, auth_headers):
        """自动登记（tags 含 auto）节点：base_url 由节点声明管理，PUT 改地址 → 409 并指路
        （改了也会在下次 renew 被声明值冲掉）；同址提交（WebUI 编辑保存的常规形态）不受限。"""
        client.post("/agents", json={
            "id": "c3d4e5f60123", "base_url": "http://c3d4e5f60123:8000",
            "role": {"disk": True, "cd": False}, "tags": ["auto", "storage"],
        }, headers=auth_headers)
        res = client.put("/agents/c3d4e5f60123", json={
            "base_url": "http://c3d4e5f60123-moved:9000",
            "role": {"disk": True, "cd": False}, "tags": ["auto", "storage"],
        }, headers=auth_headers)
        assert res.status_code == 409
        assert "node declaration" in res.json()["detail"]
        res = client.put("/agents/c3d4e5f60123", json={
            "base_url": "http://c3d4e5f60123:8000",
            "role": {"disk": True, "cd": False}, "tags": ["auto", "storage"],
        }, headers=auth_headers)
        assert res.status_code == 200

    def test_update_base_url_allowed_for_manual_agent(self, client, auth_headers, agent_payload):
        """手工登记节点（无 auto 标签）：无节点声明可依，地址归控制面，仍可改。"""
        client.post("/agents", json=agent_payload, headers=auth_headers)
        res = client.put("/agents/a1b2c3d4e5f6", json={
            "base_url": "http://a1b2c3d4e5f6-moved:9000",
            "role": {"disk": True, "cd": False}, "tags": ["storage"],
        }, headers=auth_headers)
        assert res.status_code == 200
        assert res.json()["base_url"] == "http://a1b2c3d4e5f6-moved:9000"

    def test_update_missing_404(self, client, auth_headers):
        res = client.put("/agents/a1b2c3d4e5f6", json={
            "base_url": "http://a1b2c3d4e5f6:8000", "role": {"disk": True, "cd": False},
        }, headers=auth_headers)
        assert res.status_code == 404

    def test_delete_agent_reports_impacted_workers(self, client, auth_headers, agent_payload, mock_agent_client):
        """删除 = 仅清台账（软删除，节点下次 renew 自动重新登记）：不拦截，但响应告知
        仍挂在该节点上的盘与受影响 Worker（登记即认知——这些盘在重新登记前无法投影引导地址）。"""
        client.post("/agents", json=agent_payload, headers=auth_headers)
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        res = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "m",
        }, headers=auth_headers)
        assert res.status_code == 201, res.text
        res = client.delete("/agents/a1b2c3d4e5f6", headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert body["deleted"] == "a1b2c3d4e5f6"
        assert body["disks_hosted"] == 1
        assert body["affected_workers"] == ["worker-01"]

    def test_requires_token(self, client, monkeypatch, agent_payload):
        """无凭据 401（关闭开发模式；认证器链行为见 test_auth.py）。"""
        monkeypatch.setenv("KURRENT_CP_INSECURE", "")
        assert client.get("/agents").status_code == 401
        assert client.post("/agents", json=agent_payload).status_code == 401


class TestAgentHealthMapping:
    """列表健康探测三态：ok（健康）/ degraded（在线但数据面不可用）/ error（掉线）。"""

    def test_health_ok(self, client, auth_headers, agent_payload, mock_agent_client):
        client.post("/agents", json=agent_payload, headers=auth_headers)
        mock_agent_client.healthz = lambda: {"status": "ok", "backend": "nvmet", "backend_ready": True}
        listing = client.get("/agents", headers=auth_headers).json()
        assert listing[0]["health"] == "ok"

    def test_health_degraded_backend_not_ready(self, client, auth_headers, agent_payload, mock_agent_client):
        """agent 在线但后端未就绪（nvmet-host 不可达 / configfs 未就绪）→ degraded；
        capabilities 随之失败时保留健康探针的具体原因，不覆盖成笼统 error。"""
        client.post("/agents", json=agent_payload, headers=auth_headers)
        mock_agent_client.healthz = lambda: {
            "status": "ok", "backend": "nvmet", "backend_ready": False,
            "backend_detail": "nvmet-host reachable; configfs not ready",
        }

        def caps_fail():
            raise RuntimeError("agent HTTP 503: nvmet host unreachable")

        mock_agent_client.capabilities = caps_fail
        listing = client.get("/agents", headers=auth_headers).json()
        assert listing[0]["health"] == "degraded"
        assert "configfs" in listing[0]["error"]

    def test_health_error_unreachable(self, client, auth_headers, agent_payload, mock_agent_client):
        client.post("/agents", json=agent_payload, headers=auth_headers)

        def boom():
            raise RuntimeError("connection refused")

        mock_agent_client.healthz = boom
        listing = client.get("/agents", headers=auth_headers).json()
        assert listing[0]["health"] == "error"
        assert "connection refused" in listing[0]["error"]


class TestAgentProbe:
    """探测预览：推导注册参数，不落盘（AgentClient 类方法 mock）。"""

    def test_probe_derives_params(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr(AgentClient, "healthz", lambda self: {"status": "ok"})
        monkeypatch.setattr(AgentClient, "capabilities", lambda self: {
            "backend": "lio", "cd": True, "base_nqn": "nqn.2026-07.com.test",
        })
        res = client.post("/agents/probe", json={
            "base_url": "http://ag-probe:8000",
        }, headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert body["role"] == {"disk": True, "cd": True}
        assert body["tags"] == ["storage"]  # tags 纯人工标签：后端类型不进 tags
        assert body["storager_ip"] == "ag-probe"
        assert body["backend"] == "lio"  # 后端类型单独返回（/boot-vars 实时查 capabilities 判定）
        assert body["base_nqn"] == "nqn.2026-07.com.test"
        # 不落盘：agents 列表仍为空
        assert client.get("/agents", headers=auth_headers).json() == []

    def test_probe_unreachable_502(self, client, auth_headers, monkeypatch):
        def boom(self):
            raise RuntimeError("connection refused")
        monkeypatch.setattr(AgentClient, "healthz", boom)
        res = client.post("/agents/probe", json={"base_url": "http://down:8000"}, headers=auth_headers)
        assert res.status_code == 502

    def test_probe_capabilities_error_dict_detail(self, client, auth_headers, monkeypatch):
        """capabilities 失败：502 + {"agent": base_url, "error": ...} dict detail 契约——
        API 文档「LUN 管理」节的 Agent 代理错误透传结构，WebUI extractDetail 与 CLI detailOf 消费。"""
        from control_plane.app.agent_client import AgentAPIError

        monkeypatch.setattr(AgentClient, "healthz", lambda self: {"status": "ok"})

        def boom(self):
            raise AgentAPIError("_probe", 500, "capabilities exploded")

        monkeypatch.setattr(AgentClient, "capabilities", boom)
        # 尾斜杠顺带验证 base_url 规范化（strip + rstrip "/"）
        res = client.post("/agents/probe", json={"base_url": "http://d4e5f6012345:8000/"}, headers=auth_headers)
        assert res.status_code == 502
        assert res.json()["detail"] == {"agent": "http://d4e5f6012345:8000", "error": "capabilities exploded"}

    def test_probe_edit_scenario(self, client, auth_headers, monkeypatch):
        """编辑场景探测：经控制面 mTLS 客户端证书鉴权（鉴权不用 token），
        agent_id 仅作编辑场景标识，无 token 回退逻辑。"""
        monkeypatch.setattr(AgentClient, "healthz", lambda self: {"status": "ok"})
        monkeypatch.setattr(AgentClient, "capabilities", lambda self: {"backend": "stgt"})
        client.post("/agents", json={
            "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
            "role": {"disk": True, "cd": False},
        }, headers=auth_headers)
        res = client.post("/agents/probe", json={
            "base_url": "http://a1b2c3d4e5f6:8000", "agent_id": "a1b2c3d4e5f6",
        }, headers=auth_headers)
        assert res.status_code == 200
        assert res.json()["backend"] == "stgt"


class TestAgentLuns:
    """LUN 管理（agents.client mock）。"""

    def test_list_luns_empty(self, client, auth_headers, agent_payload, mock_agent_client):
        client.post("/agents", json=agent_payload, headers=auth_headers)
        res = client.get("/agents/a1b2c3d4e5f6/luns", headers=auth_headers)
        assert res.status_code == 200
        assert res.json() == []

    def test_luns_missing_agent_404(self, client, auth_headers):
        assert client.get("/agents/a1b2c3d4e5f6/luns", headers=auth_headers).status_code == 404

    def test_create_disk_lun(self, client, auth_headers, agent_payload, mock_agent_client):
        client.post("/agents", json=agent_payload, headers=auth_headers)
        res = client.post("/agents/a1b2c3d4e5f6/luns/disk", json={
            "iqn": "iqn.2026-07.com.test:worker-01.ubuntu",
            "filename": "worker-01.ubuntu.img",
            "master": "ubuntu-24.04-master",
        }, headers=auth_headers)
        assert res.status_code == 201
        assert res.json()["backing"] == "/srv/iscsi/worker-01.ubuntu.img"

    def test_create_cd_lun(self, client, auth_headers, agent_payload, mock_agent_client):
        client.post("/agents", json=agent_payload, headers=auth_headers)
        res = client.post("/agents/a1b2c3d4e5f6/luns/cd", json={"iso": "windows-11.iso"}, headers=auth_headers)
        assert res.status_code == 201

    def test_create_cd_on_non_cd_agent_400(self, client, auth_headers, agent_payload, mock_agent_client):
        agent_payload["role"] = {"disk": True, "cd": False}
        client.post("/agents", json=agent_payload, headers=auth_headers)
        res = client.post("/agents/a1b2c3d4e5f6/luns/cd", json={"iso": "windows-11.iso"}, headers=auth_headers)
        assert res.status_code == 400

    def test_create_on_disabled_agent_409(self, client, auth_headers, agent_payload, mock_agent_client):
        """enabled=排空语义：停用节点不接任何新增（显式点名也拒），查询/删除不受限。"""
        agent_payload["enabled"] = False
        client.post("/agents", json=agent_payload, headers=auth_headers)
        res = client.post("/agents/a1b2c3d4e5f6/luns/disk", json={
            "iqn": "iqn.2026-07.com.test:worker-01.ubuntu",
            "filename": "worker-01.ubuntu.img",
            "master": "ubuntu-24.04-master",
        }, headers=auth_headers)
        assert res.status_code == 409
        assert "disabled" in res.json()["detail"]
        res = client.post("/agents/a1b2c3d4e5f6/luns/cd", json={"iso": "windows-11.iso"}, headers=auth_headers)
        assert res.status_code == 409
        # 非新增入口不受 enabled 限制
        assert client.get("/agents/a1b2c3d4e5f6/luns", headers=auth_headers).status_code == 200
        assert client.post("/agents/a1b2c3d4e5f6/luns/scan", headers=auth_headers).status_code == 200

    def test_scan(self, client, auth_headers, agent_payload, mock_agent_client):
        client.post("/agents", json=agent_payload, headers=auth_headers)
        res = client.post("/agents/a1b2c3d4e5f6/luns/scan", headers=auth_headers)
        assert res.status_code == 200
        assert res.json() == {"created": [], "skipped": []}


class TestMasters:
    """母盘聚合：逐 Agent 独立，失败不阻塞整体。"""

    def test_masters_aggregate(self, client, auth_headers, monkeypatch):
        from types import SimpleNamespace

        from control_plane.app.stores import agents

        def fake_client(agent):
            return SimpleNamespace(list_masters=lambda: {
                "masters": [{"name": "ubuntu-24.04-master", "size": "20G",
                              "path": f"/srv/{agent.id}/x.img"}],
            })
        monkeypatch.setattr(agents, "client", fake_client)
        for aid in ("a1b2c3d4e5f6", "b2c3d4e5f601"):
            client.post("/agents", json={
                "id": aid, "base_url": f"http://{aid}:8000",
                "role": {"disk": True, "cd": False},
            }, headers=auth_headers)
        res = client.get("/masters", headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert len(body["agents"]) == 2
        for entry in body["agents"]:
            assert len(entry["masters"]) == 1
            assert entry["masters"][0]["name"] == "ubuntu-24.04-master"

    def test_masters_partial_failure(self, client, auth_headers, monkeypatch):
        from types import SimpleNamespace

        from control_plane.app.agent_client import AgentAPIError
        from control_plane.app.stores import agents

        def flaky(agent):
            if agent.id == "b2c3d4e5f601":
                raise AgentAPIError(agent.id, 500, "boom")
            return SimpleNamespace(list_masters=lambda: {
                "masters": [{"name": "ubuntu-24.04-master", "size": "20G",
                              "path": f"/srv/{agent.id}/x.img"}],
            })
        monkeypatch.setattr(agents, "client", flaky)
        for aid in ("a1b2c3d4e5f6", "b2c3d4e5f601"):
            client.post("/agents", json={
                "id": aid, "base_url": f"http://{aid}:8000",
                "role": {"disk": True, "cd": False},
            }, headers=auth_headers)
        res = client.get("/masters", headers=auth_headers)
        assert res.status_code == 200  # 单台失败不阻塞整体
        body = res.json()
        failed = [e for e in body["agents"] if e["agent"] == "b2c3d4e5f601"][0]
        assert failed["masters"] == []
        assert failed["error"] == "boom"


class TestMasterTags:
    """母盘标签登记（控制面台账，备注性质）：登记/覆盖/清除/聚合合并/404。"""

    def _register_agent(self, client, auth_headers):
        res = client.post("/agents", json={
            "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
            "role": {"disk": True, "cd": False}, "tags": ["storage", "stgt"],
        }, headers=auth_headers)
        assert res.status_code == 201, res.text

    def test_set_master_tag(self, client, auth_headers):
        self._register_agent(client, auth_headers)
        res = client.put("/agents/a1b2c3d4e5f6/masters/ubuntu-24.04-master/tag",
                         json={"os": "Ubuntu", "os_version": "24.04"}, headers=auth_headers)
        assert res.status_code == 200
        assert res.json() == {
            "agent": "a1b2c3d4e5f6", "name": "ubuntu-24.04-master",
            "os": "ubuntu", "os_version": "24.04", "remark": "",  # os 小写归一
        }
        # 覆盖更新（同键幂等）
        res = client.put("/agents/a1b2c3d4e5f6/masters/ubuntu-24.04-master/tag",
                         json={"os": "ubuntu", "os_version": "24.04.1"}, headers=auth_headers)
        assert res.status_code == 200
        assert res.json()["os_version"] == "24.04.1"

    def test_set_master_tag_version_optional(self, client, auth_headers):
        self._register_agent(client, auth_headers)
        res = client.put("/agents/a1b2c3d4e5f6/masters/debian-master/tag",
                         json={"os": "debian"}, headers=auth_headers)
        assert res.status_code == 200
        assert res.json()["os_version"] == ""  # 缺省 → ''（库表形态：无版本）

    def test_set_master_tag_invalid_os_400(self, client, auth_headers):
        self._register_agent(client, auth_headers)
        res = client.put("/agents/a1b2c3d4e5f6/masters/x/tag",
                         json={"os": "bad os!"}, headers=auth_headers)
        assert res.status_code == 400
        assert "invalid os" in res.json()["detail"]

    def test_clear_master_tag(self, client, auth_headers):
        self._register_agent(client, auth_headers)
        client.put("/agents/a1b2c3d4e5f6/masters/ubuntu-24.04-master/tag",
                   json={"os": "ubuntu", "os_version": "24.04"}, headers=auth_headers)
        res = client.delete("/agents/a1b2c3d4e5f6/masters/ubuntu-24.04-master/tag", headers=auth_headers)
        assert res.status_code == 200
        assert res.json() == {"agent": "a1b2c3d4e5f6", "name": "ubuntu-24.04-master", "removed": True}
        # 再清 → 未登记（removed=false）
        res = client.delete("/agents/a1b2c3d4e5f6/masters/ubuntu-24.04-master/tag", headers=auth_headers)
        assert res.json()["removed"] is False

    def test_master_tag_missing_agent_404(self, client, auth_headers):
        assert client.put("/agents/123456789abc/masters/x/tag", json={"os": "ubuntu"},
                          headers=auth_headers).status_code == 404
        assert client.delete("/agents/123456789abc/masters/x/tag", headers=auth_headers).status_code == 404

    def test_master_tag_requires_token(self, client, monkeypatch, agent_payload):
        """无凭据 401（关闭开发模式；认证器链行为见 test_auth.py）。"""
        monkeypatch.setenv("KURRENT_CP_INSECURE", "")
        assert client.put("/agents/a1b2c3d4e5f6/masters/x/tag", json={"os": "ubuntu"}).status_code == 401
        assert client.delete("/agents/a1b2c3d4e5f6/masters/x/tag").status_code == 401

    def test_masters_aggregate_merges_tags(self, client, auth_headers, monkeypatch):
        """/masters 聚合：已登记的母盘条目附加 os/os_version；未登记的不附加。"""
        from types import SimpleNamespace

        from control_plane.app.stores import agents

        def fake_client(agent):
            return SimpleNamespace(list_masters=lambda: {
                "masters": [
                    {"name": "ubuntu-24.04-master", "size": "20G", "path": "/srv/x1.img"},
                    {"name": "unlabeled-master", "size": "30G", "path": "/srv/x2.img"},
                ],
            })
        monkeypatch.setattr(agents, "client", fake_client)
        self._register_agent(client, auth_headers)
        client.put("/agents/a1b2c3d4e5f6/masters/ubuntu-24.04-master/tag",
                   json={"os": "ubuntu", "os_version": "24.04"}, headers=auth_headers)

        res = client.get("/masters", headers=auth_headers)
        assert res.status_code == 200
        masters = res.json()["agents"][0]["masters"]
        tagged = next(m for m in masters if m["name"] == "ubuntu-24.04-master")
        assert tagged["os"] == "ubuntu"
        assert tagged["os_version"] == "24.04"
        untagged = next(m for m in masters if m["name"] == "unlabeled-master")
        assert "os" not in untagged and "os_version" not in untagged  # 未登记不附加
