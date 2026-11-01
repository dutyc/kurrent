"""Worker 域（Bearer 鉴权）：CRUD / 批量 / MAC 换绑 / 系统盘（mock agent）/ 默认启动 / 删除。"""

import re

import pytest

MAC_A = "00:11:22:33:44:55"
MAC_B = "00:11:22:33:44:66"
MAC_C = "00:11:22:33:44:77"


@pytest.fixture()
def register_agent(client, auth_headers, mock_agent_client):
    """注册磁盘角色 Agent（mock client 已就位，无网络）。"""
    res = client.post("/agents", json={
        "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
        "role": {"disk": True, "cd": False}, "tags": ["storage", "stgt"],
    }, headers=auth_headers)
    assert res.status_code == 201, res.text
    return res.json()


class TestWorkerCreate:
    """Worker 创建：空转 / 带 mac 绑定 / 冲突校验。"""

    def test_create_idle_worker(self, client, auth_headers):
        res = client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        assert res.status_code == 201
        body = res.json()
        assert body["hostname"] == "worker-01"  # hostname 缺省 = worker_id
        assert body["state"] == "registered"
        assert body["mac"] is None
        assert body["bound_device"] is None
        assert body["readiness"] == "idle"

    def test_create_with_arch_and_boot(self, client, auth_headers):
        res = client.post("/workers", json={
            "worker_id": "worker-01", "arch": "x86_64",
            "boot": {"menu_default": "windows", "menu_timeout": 5},
        }, headers=auth_headers)
        assert res.status_code == 201
        body = res.json()
        assert body["arch"] == "x86_64"
        assert body["boot"] == {"menu_default": "windows", "menu_timeout": 5}

    def test_create_with_mac_binds_device(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        res = client.post("/workers", json={"worker_id": "worker-01", "mac": MAC_A}, headers=auth_headers)
        assert res.status_code == 201, res.text
        assert res.json()["bound_device"] == MAC_A
        dev = client.get(f"/devices/{MAC_A}", headers=auth_headers).json()
        assert dev["state"] == "bound"
        assert dev["bound_worker_id"] == "worker-01"
        # dnsmasq 绑定
        from control_plane.app.config import settings
        assert MAC_A in settings.dnsmasq_hosts_file.read_text(encoding="utf-8")

    def test_create_duplicate_409(self, client, auth_headers):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        res = client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        assert res.status_code == 409

    def test_create_invalid_id_400(self, client, auth_headers):
        res = client.post("/workers", json={"worker_id": "Bad ID!"}, headers=auth_headers)
        assert res.status_code == 400

    def test_create_hostname_conflict_409(self, client, auth_headers):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        res = client.post("/workers", json={"worker_id": "worker-02", "hostname": "worker-01"},
                          headers=auth_headers)
        assert res.status_code == 409

    def test_create_mac_not_in_pool_409(self, client, auth_headers):
        res = client.post("/workers", json={"worker_id": "worker-01", "mac": MAC_A}, headers=auth_headers)
        assert res.status_code == 409
        assert "not in pool" in res.json()["detail"]

    def test_requires_token(self, client, monkeypatch):
        """无凭据 401（关闭开发模式；认证器链行为见 test_auth.py）。"""
        monkeypatch.setenv("KURRENT_CP_INSECURE", "")
        assert client.post("/workers", json={"worker_id": "worker-01"}).status_code == 401
        assert client.get("/workers").status_code == 401


class TestWorkerBatch:
    """批量创建：逐项独立、幂等重跑。"""

    def test_batch_create(self, client, auth_headers):
        res = client.post("/workers/batch", json={"count": 3, "name_prefix": "worker-"},
                          headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert [i["worker_id"] for i in body["succeeded"]] == ["worker-01", "worker-02", "worker-03"]
        # 重跑 → 全部 skipped
        res = client.post("/workers/batch", json={"count": 3, "name_prefix": "worker-"},
                          headers=auth_headers)
        body = res.json()
        assert len(body["succeeded"]) == 0
        assert all(i["reason"] == "already exists" for i in body["skipped"])

    def test_batch_create_with_macs(self, client, auth_headers):
        for mac in (MAC_A, MAC_B, MAC_C):
            client.post("/devices", json={"mac": mac}, headers=auth_headers)
        res = client.post("/workers/batch", json={
            "count": 3, "name_prefix": "worker-",
            "macs": [MAC_A, MAC_B, MAC_C],
        }, headers=auth_headers)
        assert res.status_code == 200
        assert len(res.json()["succeeded"]) == 3
        assert len(client.get("/devices", params={"state": "bound"}, headers=auth_headers).json()) == 3

    def test_batch_macs_length_mismatch_400(self, client, auth_headers):
        res = client.post("/workers/batch", json={
            "count": 2, "name_prefix": "worker-", "macs": [MAC_A],
        }, headers=auth_headers)
        assert res.status_code == 400

    def test_batch_device_conflict_item_failed(self, client, auth_headers):
        # 设备已被其他 worker 占用 → 该项 failed 且不创建（worker 重名则 skipped，不在此用例范围）
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/devices", json={"mac": MAC_B}, headers=auth_headers)
        client.post("/workers", json={"worker_id": "worker-01", "mac": MAC_A}, headers=auth_headers)
        res = client.post("/workers/batch", json={
            "count": 2, "name_prefix": "node-",
            "macs": [MAC_A, MAC_B],
        }, headers=auth_headers)
        body = res.json()
        assert len(body["failed"]) == 1
        assert body["failed"][0]["worker_id"] == "node-01"
        assert "already bound" in body["failed"][0]["error"]
        assert len(body["succeeded"]) == 1
        assert body["succeeded"][0]["worker_id"] == "node-02"


class TestWorkerMac:
    """MAC 换绑：hostname 不变，旧设备回池，新设备绑定。"""

    def test_update_mac_rebind(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/devices", json={"mac": MAC_B}, headers=auth_headers)
        client.post("/workers", json={"worker_id": "worker-01", "mac": MAC_A}, headers=auth_headers)

        res = client.put("/workers/worker-01/mac", json={"mac": MAC_B}, headers=auth_headers)
        assert res.status_code == 200, res.text
        assert res.json()["mac"] == MAC_B
        # 旧设备回池、新设备绑定
        old = client.get(f"/devices/{MAC_A}", headers=auth_headers).json()
        assert old["state"] == "pooled"
        new = client.get(f"/devices/{MAC_B}", headers=auth_headers).json()
        assert new["state"] == "bound"
        assert new["bound_worker_id"] == "worker-01"
        # dnsmasq：hostname 仍指向新 mac
        from control_plane.app.config import settings
        hosts = settings.dnsmasq_hosts_file.read_text(encoding="utf-8")
        assert MAC_B in hosts and MAC_A not in hosts

    def test_update_mac_same_unchanged(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/workers", json={"worker_id": "worker-01", "mac": MAC_A}, headers=auth_headers)
        res = client.put("/workers/worker-01/mac", json={"mac": MAC_A}, headers=auth_headers)
        assert res.status_code == 200
        assert res.json()["mac"] == MAC_A

    def test_update_mac_without_dnsmasq_binding(self, client, auth_headers):
        """回归：全新 Worker 尚无 dnsmasq 绑定也能直接换绑（绑定是派生投影，不参与判定）。"""
        from control_plane.app.config import settings
        client.post("/devices", json={"mac": MAC_B}, headers=auth_headers)
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        assert (not settings.dnsmasq_hosts_file.exists()
                or "worker-01" not in settings.dnsmasq_hosts_file.read_text(encoding="utf-8"))

        res = client.put("/workers/worker-01/mac", json={"mac": MAC_B}, headers=auth_headers)
        assert res.status_code == 200, res.text
        assert res.json()["mac"] == MAC_B
        dev = client.get(f"/devices/{MAC_B}", headers=auth_headers).json()
        assert dev["state"] == "bound"
        assert dev["bound_worker_id"] == "worker-01"
        assert f"{MAC_B},worker-01" in settings.dnsmasq_hosts_file.read_text(encoding="utf-8")

    def test_update_mac_reads_ledger_not_dnsmasq(self, client, auth_headers):
        """旧设备从设备台账反查：dnsmasq 绑定丢失时仍能正确回池旧设备并补写新绑定。"""
        from control_plane.app.config import settings
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/devices", json={"mac": MAC_B}, headers=auth_headers)
        client.post("/workers", json={"worker_id": "worker-01", "mac": MAC_A}, headers=auth_headers)
        settings.dnsmasq_hosts_file.write_text("", encoding="utf-8")  # 模拟投影丢失

        res = client.put("/workers/worker-01/mac", json={"mac": MAC_B}, headers=auth_headers)
        assert res.status_code == 200, res.text
        assert client.get(f"/devices/{MAC_A}", headers=auth_headers).json()["state"] == "pooled"
        new = client.get(f"/devices/{MAC_B}", headers=auth_headers).json()
        assert new["state"] == "bound"
        assert new["bound_worker_id"] == "worker-01"
        hosts = settings.dnsmasq_hosts_file.read_text(encoding="utf-8")
        assert f"{MAC_B},worker-01" in hosts and MAC_A not in hosts

    def test_update_mac_same_repairs_missing_binding(self, client, auth_headers):
        """台账已绑定、dnsmasq 投影缺失时，同 MAC 调用补写投影而不是 409。"""
        from control_plane.app.config import settings
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/workers", json={"worker_id": "worker-01", "mac": MAC_A}, headers=auth_headers)
        settings.dnsmasq_hosts_file.write_text("", encoding="utf-8")

        res = client.put("/workers/worker-01/mac", json={"mac": MAC_A}, headers=auth_headers)
        assert res.status_code == 200, res.text
        assert f"{MAC_A},worker-01" in settings.dnsmasq_hosts_file.read_text(encoding="utf-8")

    def test_update_mac_not_in_pool_409(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/workers", json={"worker_id": "worker-01", "mac": MAC_A}, headers=auth_headers)
        res = client.put("/workers/worker-01/mac", json={"mac": MAC_C}, headers=auth_headers)
        assert res.status_code == 409


class TestWorkerDisks:
    """系统盘：建盘（mock agent）/ 批量建盘 / 删盘 / default_disk 联动。"""

    def test_create_disk_ready(self, client, auth_headers, register_agent):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        res = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
        }, headers=auth_headers)
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["state"] == "ready"
        disk = body["disks"][0]
        assert disk["os"] == "ubuntu"
        assert disk["os_version"] == ""  # 无版本 → ''（库表形态）
        assert re.fullmatch(r"[0-9a-f]{12}", disk["os_tag"])  # 盘级随机标识（数据面唯一键）
        # 盘标识权威 = NQN：后缀带 os_tag（worker_id.os.<os_tag>），IQN/文件名由其派生
        assert disk["nqn"] == f"nqn.2026-07.com.test:worker-01.ubuntu.{disk['os_tag']}"
        assert disk["iqn"] == "iqn." + disk["nqn"][4:]
        assert disk["filename"] == f"worker-01.ubuntu.{disk['os_tag']}.img"
        assert disk["agent"] == "a1b2c3d4e5f6"
        assert disk["source"] == {"type": "master", "name": "ubuntu-24.04-master"}
        # 只有一块盘 → 它就是默认启动盘（菜单不再落到 reboot）
        assert body["default_disk"] == disk["os_tag"]

    def test_second_disk_keeps_existing_default(self, client, auth_headers, register_agent):
        """多盘并存不改变已选默认盘：只有首块盘自动成为默认。"""
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        first = client.post("/workers/worker-01/luns/disk", json={
            "type": "empty", "os": "ubuntu", "size": "40G",
        }, headers=auth_headers).json()
        res = client.post("/workers/worker-01/luns/disk", json={
            "type": "empty", "os": "debian", "os_version": "12", "size": "40G",
        }, headers=auth_headers)
        assert res.status_code == 201, res.text
        assert res.json()["default_disk"] == first["disks"][0]["os_tag"]

    def test_delete_default_promotes_remaining_disk(self, client, auth_headers, register_agent):
        """删掉默认盘后只剩一块盘：剩余盘成为默认启动盘。"""
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        first = client.post("/workers/worker-01/luns/disk", json={
            "type": "empty", "os": "ubuntu", "size": "40G",
        }, headers=auth_headers).json()["disks"][0]
        second = client.post("/workers/worker-01/luns/disk", json={
            "type": "empty", "os": "debian", "os_version": "12", "size": "40G",
        }, headers=auth_headers).json()["disks"][1]
        assert first["os_tag"] != second["os_tag"]

        body = client.delete(f"/workers/worker-01/luns/disk/{first['os_tag']}",
                             headers=auth_headers).json()
        assert body["default_disk"] == second["os_tag"]

    def test_migration_sets_default_for_legacy_single_disk(self, client, auth_headers, register_agent):
        """存量迁移：单盘却未设 default_disk 的 Worker 在启动迁移后补上默认盘。"""
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        disk = client.post("/workers/worker-01/luns/disk", json={
            "type": "empty", "os": "ubuntu", "size": "40G",
        }, headers=auth_headers).json()["disks"][0]
        client.put("/workers/worker-01/default-disk", json={"disk": None}, headers=auth_headers)

        from control_plane.app.routers.workers import migrate_single_disk_default
        migrate_single_disk_default()
        migrate_single_disk_default()  # 幂等

        body = client.get("/workers/worker-01", headers=auth_headers).json()
        assert body["default_disk"] == disk["os_tag"]

    def test_create_disk_disabled_agent_409(self, client, auth_headers, register_agent):
        """enabled=排空语义：显式点名停用节点建盘 → 409（不碰存量是另一条边界，见 trust_chain）。"""
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        res = client.put("/agents/a1b2c3d4e5f6", json={
            "base_url": "http://a1b2c3d4e5f6:8000", "role": {"disk": True, "cd": False},
            "tags": ["storage"], "enabled": False,
        }, headers=auth_headers)
        assert res.status_code == 200
        res = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
            "disk_agent": "a1b2c3d4e5f6",
        }, headers=auth_headers)
        assert res.status_code == 409
        assert "disabled" in res.json()["detail"]

    def test_batch_disk_disabled_and_missing_agent(self, client, auth_headers, register_agent):
        """批量建盘逐项独立：停用节点 → 409 文案；不存在的 agent → 清晰 404 文案
        （回归：agents.get 抛 KeyError，原死代码 `if not disk_agent` 使其落成 500 "'xx'"）。"""
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        client.post("/workers", json={"worker_id": "worker-02"}, headers=auth_headers)
        res = client.put("/agents/a1b2c3d4e5f6", json={
            "base_url": "http://a1b2c3d4e5f6:8000", "role": {"disk": True, "cd": False},
            "tags": ["storage"], "enabled": False,
        }, headers=auth_headers)
        assert res.status_code == 200
        res = client.post("/workers/luns/disk/batch", json={
            "type": "empty", "os": "ubuntu", "size": "40G",
            "targets": [
                {"worker_id": "worker-01", "agent": "a1b2c3d4e5f6"},
                {"worker_id": "worker-02", "agent": "0123456789ab"},
            ],
        }, headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert body["succeeded"] == []
        failed = {item["worker_id"]: item["error"] for item in body["failed"]}
        assert "disabled" in failed["worker-01"]
        assert failed["worker-02"] == "agent not found: 0123456789ab"

    def test_create_disk_duplicate_os_409(self, client, auth_headers, register_agent):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        body = {"type": "empty", "os": "ubuntu", "size": "40G"}
        assert client.post("/workers/worker-01/luns/disk", json=body, headers=auth_headers).status_code == 201
        res = client.post("/workers/worker-01/luns/disk", json=body, headers=auth_headers)
        assert res.status_code == 409
        assert "already has a ubuntu system disk" in res.json()["detail"]

    def test_create_disk_free_os_accepted(self, client, auth_headers, register_agent):
        """OS_ITEMS 已退役（2026-08-30）：os 为自由字符串（备注性质），无白名单。"""
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        for os_name in ("freedos", "alpine", "kali-linux"):
            res = client.post("/workers/worker-01/luns/disk", json={
                "type": "empty", "os": os_name, "size": "40G",
            }, headers=auth_headers)
            assert res.status_code == 201, res.text

    def test_create_disk_invalid_os_version_400(self, client, auth_headers, register_agent):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        res = client.post("/workers/worker-01/luns/disk", json={
            "type": "empty", "os": "ubuntu", "os_version": "bad version!", "size": "40G",
        }, headers=auth_headers)
        assert res.status_code == 400
        assert "invalid os_version" in res.json()["detail"]

    def test_create_disk_same_os_multi_version_coexist(self, client, auth_headers, register_agent):
        """同系统多版本并存：唯一键 = (os, os_version)，不同版本各一块，os_tag 区分。"""
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        for version in ("22.04", "24.04"):
            res = client.post("/workers/worker-01/luns/disk", json={
                "type": "empty", "os": "ubuntu", "os_version": version, "size": "40G",
            }, headers=auth_headers)
            assert res.status_code == 201, res.text
        disks = client.get("/workers/worker-01", headers=auth_headers).json()["disks"]
        assert len(disks) == 2
        assert {d["os_version"] for d in disks} == {"22.04", "24.04"}
        assert len({d["os_tag"] for d in disks}) == 2  # 盘标识各不相同

    def test_create_disk_duplicate_os_version_409(self, client, auth_headers, register_agent):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        body = {"type": "empty", "os": "ubuntu", "os_version": "24.04", "size": "40G"}
        assert client.post("/workers/worker-01/luns/disk", json=body, headers=auth_headers).status_code == 201
        res = client.post("/workers/worker-01/luns/disk", json=body, headers=auth_headers)
        assert res.status_code == 409
        assert "already has a ubuntu 24.04 system disk" in res.json()["detail"]

    def test_batch_create_disks_sets_default_disk(self, client, auth_headers, register_agent):
        client.post("/workers/batch", json={"count": 2, "name_prefix": "node-"}, headers=auth_headers)
        res = client.post("/workers/luns/disk/batch", json={
            "type": "empty", "os": "debian", "os_version": "12", "size": "40G",
            "targets": [{"worker_id": "node-01", "agent": "a1b2c3d4e5f6"},
                        {"worker_id": "node-02", "agent": "a1b2c3d4e5f6"}],
        }, headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert len(body["succeeded"]) == 2
        # 批量建盘约定：自动设为默认启动盘（default_disk = 新建盘的 os_tag）
        for wid in ("node-01", "node-02"):
            w = client.get(f"/workers/{wid}", headers=auth_headers).json()
            assert re.fullmatch(r"[0-9a-f]{12}", w["default_disk"])
            assert w["default_disk"] == w["disks"][0]["os_tag"]
            assert w["state"] == "ready"

    def test_batch_disk_skips_existing(self, client, auth_headers, register_agent):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        client.post("/workers/worker-01/luns/disk", json={
            "type": "empty", "os": "ubuntu", "size": "40G",
        }, headers=auth_headers)
        res = client.post("/workers/luns/disk/batch", json={
            "type": "empty", "os": "ubuntu", "size": "40G",
            "targets": [{"worker_id": "worker-01", "agent": "a1b2c3d4e5f6"}],
        }, headers=auth_headers)
        body = res.json()
        assert len(body["succeeded"]) == 0
        assert body["skipped"][0]["reason"] == "already has a ubuntu system disk"

    def test_delete_disk_regress_state(self, client, auth_headers, register_agent):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        disk = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "m",
        }, headers=auth_headers).json()["disks"][0]
        os_tag = disk["os_tag"]
        client.put("/workers/worker-01/default-disk", json={"disk": os_tag}, headers=auth_headers)
        res = client.delete(f"/workers/worker-01/luns/disk/{os_tag}", headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert body["state"] == "registered"  # 无盘回退
        assert body["disks"] == []
        assert "default_disk" not in body  # 默认启动联动清除

    def test_delete_disk_missing_tag_404(self, client, auth_headers, register_agent):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        res = client.delete("/workers/worker-01/luns/disk/0d26b6f33a89", headers=auth_headers)
        assert res.status_code == 404
        assert "no system disk with os_tag" in res.json()["detail"]


class TestDefaultBoot:
    """默认启动配置：disk=盘 os_tag（精确引用具体盘）；menu_default/menu_timeout 严格校验。"""

    def _create_ubuntu_disk(self, client, auth_headers, os_version="") -> str:
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        body = {"type": "empty", "os": "ubuntu", "size": "40G"}
        if os_version:
            body["os_version"] = os_version
        res = client.post("/workers/worker-01/luns/disk", json=body, headers=auth_headers)
        assert res.status_code == 201, res.text
        return res.json()["disks"][0]["os_tag"]

    def test_set_default_disk_requires_existing_disk(self, client, auth_headers, register_agent):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        res = client.put("/workers/worker-01/default-disk", json={"disk": "0d26b6f33a89"},
                         headers=auth_headers)
        assert res.status_code == 400
        assert "no system disk with os_tag" in res.json()["detail"]

    def test_set_default_disk_invalid_tag_400(self, client, auth_headers, register_agent):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        res = client.put("/workers/worker-01/default-disk", json={"disk": "not-a-tag"},
                         headers=auth_headers)
        assert res.status_code == 400
        assert "invalid os_tag" in res.json()["detail"]

    def test_set_invalid_menu_default_400(self, client, auth_headers):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        res = client.put("/workers/worker-01/default-disk", json={"menu_default": "hack"},
                         headers=auth_headers)
        assert res.status_code == 400

    def test_set_menu_timeout_negative_400(self, client, auth_headers):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        res = client.put("/workers/worker-01/default-disk", json={"menu_timeout": -1},
                         headers=auth_headers)
        assert res.status_code == 400

    def test_default_disk_points_to_specific_disk(self, client, auth_headers, register_agent):
        """default_disk 精确引用某块盘：多盘并存时可任意切换指向。"""
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        disk = client.post("/workers/worker-01/luns/disk", json={
            "type": "empty", "os": "ubuntu", "size": "40G",
        }, headers=auth_headers).json()["disks"][0]
        debian = client.post("/workers/worker-01/luns/disk", json={
            "type": "empty", "os": "debian", "os_version": "12", "size": "40G",
        }, headers=auth_headers).json()["disks"][0]
        res = client.put("/workers/worker-01/default-disk", json={"disk": disk["os_tag"]},
                         headers=auth_headers)
        assert res.status_code == 200
        assert res.json()["default_disk"] == disk["os_tag"]
        # 切换指向另一块盘
        res = client.put("/workers/worker-01/default-disk", json={"disk": debian["os_tag"]},
                         headers=auth_headers)
        assert res.json()["default_disk"] == debian["os_tag"]

    def test_clear_default_disk(self, client, auth_headers, register_agent):
        os_tag = self._create_ubuntu_disk(client, auth_headers)
        client.put("/workers/worker-01/default-disk", json={"disk": os_tag}, headers=auth_headers)
        res = client.put("/workers/worker-01/default-disk", json={"disk": None}, headers=auth_headers)
        assert res.status_code == 200
        assert "default_disk" not in res.json()


class TestWorkerDelete:
    """删除：台账移除 + 设备回池 + dnsmasq 清理；批量逐项独立。"""

    def test_delete_worker(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/workers", json={"worker_id": "worker-01", "mac": MAC_A}, headers=auth_headers)
        res = client.delete("/workers/worker-01", headers=auth_headers)
        assert res.status_code == 200
        assert res.json()["deleted"] == "worker-01"
        # 台账移除 + 设备回池 + dnsmasq 清理
        assert client.get("/workers/worker-01", headers=auth_headers).status_code == 404
        dev = client.get(f"/devices/{MAC_A}", headers=auth_headers).json()
        assert dev["state"] == "pooled"
        from control_plane.app.config import settings
        assert "worker-01" not in settings.dnsmasq_hosts_file.read_text(encoding="utf-8")

    def test_delete_missing_404(self, client, auth_headers):
        assert client.delete("/workers/worker-01", headers=auth_headers).status_code == 404

    def test_batch_delete(self, client, auth_headers):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        client.post("/workers", json={"worker_id": "worker-02"}, headers=auth_headers)
        res = client.post("/workers/delete/batch", json={
            "worker_ids": ["worker-01", "worker-02", "worker-99"],
        }, headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert [i["worker_id"] for i in body["succeeded"]] == ["worker-01", "worker-02"]
        assert body["failed"][0]["worker_id"] == "worker-99"
        assert len(client.get("/workers", headers=auth_headers).json()) == 0

    def test_delete_worker_with_disk(self, client, auth_headers, register_agent):
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "m",
        }, headers=auth_headers)
        res = client.delete("/workers/worker-01", params={"delete_disk": "true"}, headers=auth_headers)
        assert res.status_code == 200

    def test_delete_worker_with_deregistered_agent(self, client, auth_headers, register_agent):
        """登记缺失韧性（删除语义 = 仅清台账）：节点登记删除后，其上的 Worker 仍可删——
        凭据推送跳过、target 视为已缺失；不带 ignore_missing 时报清晰 404 而非 500。"""
        client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
        res = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "m",
        }, headers=auth_headers)
        assert res.status_code == 201, res.text
        assert client.delete("/agents/a1b2c3d4e5f6", headers=auth_headers).status_code == 200
        # 回归：原 KeyError（agents.get / _delete_target）落成 500 "'a1b2c3d4e5f6'"
        res = client.delete("/workers/worker-01", headers=auth_headers)
        assert res.status_code == 404
        assert "agent not registered" in res.json()["detail"]["error"]
        # 带 ignore_missing：删除成功
        res = client.delete("/workers/worker-01", params={"ignore_missing_target": "true"},
                            headers=auth_headers)
        assert res.status_code == 200
        assert client.get("/workers/worker-01", headers=auth_headers).status_code == 404
