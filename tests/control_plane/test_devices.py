"""设备池域（Bearer 鉴权）：CRUD / 导入 / 绑定 / 换绑 / 批量 / 撤销。"""

import pytest

from control_plane.app.config import settings

MAC_A = "00:11:22:33:44:55"
MAC_B = "00:11:22:33:44:66"
MAC_C = "00:11:22:33:44:77"
MAC_D = "00:11:22:33:44:88"


@pytest.fixture()
def worker(client, auth_headers):
    """空转 worker 台账（不绑定设备）。"""
    def _make(worker_id="worker-01"):
        res = client.post("/workers", json={"worker_id": worker_id}, headers=auth_headers)
        assert res.status_code == 201, res.text
        return res.json()
    return _make


class TestDeviceCrud:
    """设备池 CRUD。"""

    def test_create_device(self, client, auth_headers):
        res = client.post("/devices", json={"mac": MAC_A, "manufacturer": "Dell", "serial": "SN-1"},
                          headers=auth_headers)
        assert res.status_code == 201
        body = res.json()
        assert body["state"] == "pooled"
        assert body["source"] == "manual"
        assert body["fingerprint"]["manufacturer"] == "Dell"

    def test_create_duplicate_409(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        res = client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        assert res.status_code == 409

    def test_create_invalid_mac_400(self, client, auth_headers):
        res = client.post("/devices", json={"mac": "zzz"}, headers=auth_headers)
        assert res.status_code == 400

    def test_list_and_filter(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/devices", json={"mac": MAC_B}, headers=auth_headers)
        all_devs = client.get("/devices", headers=auth_headers).json()
        assert len(all_devs) == 2
        assert client.get("/devices", params={"state": "bound"}, headers=auth_headers).json() == []
        assert len(client.get("/devices", params={"state": "pooled"}, headers=auth_headers).json()) == 2
        assert client.get("/devices", params={"state": "bogus"}, headers=auth_headers).status_code == 400

    def test_get_device_404(self, client, auth_headers):
        assert client.get(f"/devices/{MAC_A}", headers=auth_headers).status_code == 404

    def test_requires_token(self, client, monkeypatch):
        """无凭据 401（关闭开发模式；认证器链行为见 test_auth.py）。"""
        monkeypatch.setenv("KURRENT_CP_INSECURE", "")
        assert client.get("/devices").status_code == 401
        assert client.post("/devices", json={"mac": MAC_A}).status_code == 401


class TestDeviceImport:
    """批量导入：逐项独立，重复跳过，非法计 failed。"""

    def test_import_devices(self, client, auth_headers):
        res = client.post("/devices/import", json={"entries": [
            {"mac": MAC_A, "manufacturer": "Dell"},
            {"mac": MAC_B},
            {"mac": "bad-mac"},
        ]}, headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert [e["mac"] for e in body["created"]] == [MAC_A, MAC_B]
        assert body["failed"] == [{"mac": "bad-mac", "reason": "invalid mac"}]
        assert len(client.get("/devices", headers=auth_headers).json()) == 2

    def test_import_duplicates_skipped(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        res = client.post("/devices/import", json={"entries": [
            {"mac": MAC_A},
            {"mac": MAC_B},
        ]}, headers=auth_headers)
        body = res.json()
        assert [e["mac"] for e in body["skipped"]] == [MAC_A]
        assert [e["mac"] for e in body["created"]] == [MAC_B]

    def test_import_empty_400(self, client, auth_headers):
        assert client.post("/devices/import", json={"entries": []}, headers=auth_headers).status_code == 400

    def test_import_revoked_failed(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.delete(f"/devices/{MAC_A}", headers=auth_headers)
        res = client.post("/devices/import", json={"entries": [{"mac": MAC_A}]}, headers=auth_headers)
        assert res.json()["failed"] == [{"mac": MAC_A, "reason": "device revoked"}]


class TestDeviceBinding:
    """绑定/解绑/换绑：设备↔worker 一对一授权。"""

    def test_bind_unbind(self, client, auth_headers, worker):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        worker("worker-01")
        res = client.post(f"/devices/{MAC_A}/bind", params={"worker_id": "worker-01"}, headers=auth_headers)
        assert res.status_code == 200
        dev = res.json()
        assert dev["state"] == "bound"
        assert dev["bound_worker_id"] == "worker-01"
        # dnsmasq 实际绑定
        from control_plane.app.config import settings
        hosts = settings.dnsmasq_hosts_file.read_text(encoding="utf-8")
        assert "worker-01" in hosts and MAC_A in hosts
        # worker 投影含绑定设备
        w = client.get("/workers/worker-01", headers=auth_headers).json()
        assert w["bound_device"] == MAC_A
        assert w["readiness"] == "partial"  # 绑定无盘

        # 解绑 → 回池 + dnsmasq 清理
        res = client.delete(f"/devices/{MAC_A}/bind", headers=auth_headers)
        assert res.status_code == 200
        assert res.json()["state"] == "pooled"
        hosts = settings.dnsmasq_hosts_file.read_text(encoding="utf-8")
        assert "worker-01" not in hosts

    def test_bind_not_found_404(self, client, auth_headers, worker):
        res = client.post(f"/devices/{MAC_A}/bind", params={"worker_id": "worker-01"}, headers=auth_headers)
        assert res.status_code == 404

    def test_bind_conflict_409(self, client, auth_headers, worker):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/devices", json={"mac": MAC_B}, headers=auth_headers)
        worker("worker-01")
        worker("worker-02")
        client.post(f"/devices/{MAC_A}/bind", params={"worker_id": "worker-01"}, headers=auth_headers)
        # 设备已绑定 → 409
        res = client.post(f"/devices/{MAC_A}/bind", params={"worker_id": "worker-02"}, headers=auth_headers)
        assert res.status_code == 409
        # worker 已绑定 → 409
        res = client.post(f"/devices/{MAC_B}/bind", params={"worker_id": "worker-01"}, headers=auth_headers)
        assert res.status_code == 409

    def test_bind_force_rebind(self, client, auth_headers, worker):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/devices", json={"mac": MAC_B}, headers=auth_headers)
        worker("worker-01")
        worker("worker-02")
        client.post(f"/devices/{MAC_A}/bind", params={"worker_id": "worker-01"}, headers=auth_headers)
        # force 原子换绑：MAC_A → worker-02；worker-01 释放
        res = client.post(f"/devices/{MAC_A}/bind", params={"worker_id": "worker-02", "force": "true"},
                          headers=auth_headers)
        assert res.status_code == 200
        assert res.json()["bound_worker_id"] == "worker-02"
        w1 = client.get("/workers/worker-01", headers=auth_headers).json()
        assert w1["bound_device"] is None
        # worker-01 可再绑定 MAC_B
        res = client.post(f"/devices/{MAC_B}/bind", params={"worker_id": "worker-01"}, headers=auth_headers)
        assert res.status_code == 200

    def test_bind_idempotent(self, client, auth_headers, worker):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        worker("worker-01")
        client.post(f"/devices/{MAC_A}/bind", params={"worker_id": "worker-01"}, headers=auth_headers)
        res = client.post(f"/devices/{MAC_A}/bind", params={"worker_id": "worker-01"}, headers=auth_headers)
        assert res.status_code == 200  # 幂等：重复绑定同 worker 直接返回

    def test_unbind_not_bound_409(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        res = client.delete(f"/devices/{MAC_A}/bind", headers=auth_headers)
        assert res.status_code == 409


class TestBatchBinding:
    """批量绑定预览 + 执行（manifest / sequential）。"""

    def test_batch_preview_matched(self, client, auth_headers, worker):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/devices", json={"mac": MAC_B}, headers=auth_headers)
        worker("worker-01")
        worker("worker-02")
        res = client.post("/devices/bind/batch/preview", json={
            "mode": "manifest",
            "pairs": [{"mac": MAC_A, "worker_id": "worker-01"},
                      {"mac": MAC_B, "worker_id": "worker-02"}],
        }, headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert len(body["matched"]) == 2
        assert body["summary"] == {"total": 2, "ok": 2, "conflict": 0, "not_found": 0}
        # 预览无写入副作用
        assert len(client.get("/devices", params={"state": "bound"}, headers=auth_headers).json()) == 0

    def test_batch_preview_conflicts_and_not_found(self, client, auth_headers, worker):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        worker("worker-01")
        res = client.post("/devices/bind/batch/preview", json={
            "mode": "manifest",
            "pairs": [
                {"mac": MAC_A, "worker_id": "worker-01"},
                {"mac": MAC_A, "worker_id": "worker-01"},  # 清单内重复
                {"mac": MAC_B, "worker_id": "worker-01"},  # 设备不在池
                {"mac": "bad", "worker_id": "worker-01"},  # 非法 mac
            ],
        }, headers=auth_headers)
        body = res.json()
        assert len(body["matched"]) == 1
        assert len(body["conflicts"]) == 1
        assert len(body["not_found"]) == 2
        assert body["not_found"][0]["reason"] == "device not in pool"
        assert body["not_found"][1]["reason"] == "invalid mac"

    def test_batch_bind_execute_and_idempotent(self, client, auth_headers, worker):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/devices", json={"mac": MAC_B}, headers=auth_headers)
        worker("worker-01")
        worker("worker-02")
        res = client.post("/devices/bind/batch", json={
            "mode": "sequential",
            "macs": [MAC_A, MAC_B],
            "worker_ids": ["worker-01", "worker-02"],
        }, headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert len(body["succeeded"]) == 2
        assert len(client.get("/devices", params={"state": "bound"}, headers=auth_headers).json()) == 2
        # 重跑 → 幂等 skipped
        res = client.post("/devices/bind/batch", json={
            "mode": "sequential",
            "macs": [MAC_A, MAC_B],
            "worker_ids": ["worker-01", "worker-02"],
        }, headers=auth_headers)
        body = res.json()
        assert len(body["succeeded"]) == 0
        assert all(i["reason"] == "already bound" for i in body["skipped"])

    def test_batch_bind_pool_missing_failed(self, client, auth_headers, worker):
        worker("worker-01")
        res = client.post("/devices/bind/batch", json={
            "mode": "manifest",
            "pairs": [{"mac": MAC_A, "worker_id": "worker-01"}],
        }, headers=auth_headers)
        body = res.json()
        assert len(body["failed"]) == 1
        assert "not found" in body["failed"][0]["reason"]


class TestDeviceRevoke:
    """吊销：pooled → revoked；已绑定需先解绑；已吊销 409。"""

    def test_revoke_pooled(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        res = client.delete(f"/devices/{MAC_A}", headers=auth_headers)
        assert res.status_code == 200
        assert res.json()["state"] == "revoked"
        assert client.get(f"/devices/{MAC_A}", headers=auth_headers).json()["state"] == "revoked"
        assert f"{MAC_A},ignore" in settings.dnsmasq_hosts_file.read_text(encoding="utf-8")

    def test_report_clears_stale_dhcp_block(self, client, auth_headers, register_claimed_device):
        settings.dnsmasq_hosts_file.write_text(f"{MAC_A},ignore\n", encoding="utf-8")
        register_claimed_device(MAC_A)
        assert f"{MAC_A},ignore" not in settings.dnsmasq_hosts_file.read_text(encoding="utf-8")

    def test_revoke_bound_409(self, client, auth_headers, worker):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        worker("worker-01")
        client.post(f"/devices/{MAC_A}/bind", params={"worker_id": "worker-01"}, headers=auth_headers)
        res = client.delete(f"/devices/{MAC_A}", headers=auth_headers)
        assert res.status_code == 409
        assert "unbind first" in res.json()["detail"]

    def test_revoke_twice_409(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.delete(f"/devices/{MAC_A}", headers=auth_headers)
        res = client.delete(f"/devices/{MAC_A}", headers=auth_headers)
        assert res.status_code == 409

    def test_purge_revoked_only(self, client, auth_headers):
        """清除已吊销设备：dry_run 只预览；pooled 不受影响；重复清除幂等。"""
        for mac in (MAC_A, MAC_B):
            client.post("/devices", json={"mac": mac}, headers=auth_headers)
        client.delete(f"/devices/{MAC_A}", headers=auth_headers)

        preview = client.delete("/devices/revoked", params={"dry_run": True},
                                headers=auth_headers).json()
        assert preview == {"purged": [MAC_A], "count": 1, "dry_run": True}
        assert client.get(f"/devices/{MAC_A}", headers=auth_headers).status_code == 200

        res = client.delete("/devices/revoked", headers=auth_headers)
        assert res.json() == {"purged": [MAC_A], "count": 1, "dry_run": False}
        assert client.get(f"/devices/{MAC_A}", headers=auth_headers).status_code == 404
        assert client.get(f"/devices/{MAC_B}", headers=auth_headers).status_code == 200
        assert '"device.purge"' in settings.operations_file.read_text(encoding="utf-8")

        assert client.delete("/devices/revoked", headers=auth_headers).json()["count"] == 0

    def test_purge_can_be_scoped_to_given_macs(self, client, auth_headers):
        """`mac` 可重复给出以限定范围：未列出的 revoked 设备保留，未吊销的忽略。"""
        for item in (MAC_A, MAC_B, MAC_C):
            client.post("/devices", json={"mac": item}, headers=auth_headers)
        for item in (MAC_A, MAC_B):
            client.delete(f"/devices/{item}", headers=auth_headers)

        res = client.delete("/devices/revoked",
                            params=[("mac", MAC_A), ("mac", MAC_C)],
                            headers=auth_headers)
        assert res.json()["purged"] == [MAC_A]
        assert client.get(f"/devices/{MAC_B}", headers=auth_headers).json()["state"] == "revoked"
        assert client.get(f"/devices/{MAC_C}", headers=auth_headers).json()["state"] == "pooled"
        assert client.delete("/devices/revoked", params={"mac": "zzz"},
                             headers=auth_headers).status_code == 400

    def test_purge_leaves_bound_devices(self, client, auth_headers, worker):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/devices", json={"mac": MAC_B}, headers=auth_headers)
        worker("worker-01")
        client.post(f"/devices/{MAC_A}/bind", params={"worker_id": "worker-01"},
                    headers=auth_headers)
        client.delete(f"/devices/{MAC_B}", headers=auth_headers)

        assert client.delete("/devices/revoked", headers=auth_headers).json()["purged"] == [MAC_B]
        assert client.get(f"/devices/{MAC_A}", headers=auth_headers).json()["state"] == "bound"

    def test_revoked_cannot_bind(self, client, auth_headers, worker):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.delete(f"/devices/{MAC_A}", headers=auth_headers)
        worker("worker-01")
        res = client.post(f"/devices/{MAC_A}/bind", params={"worker_id": "worker-01"}, headers=auth_headers)
        assert res.status_code in (404, 409)
