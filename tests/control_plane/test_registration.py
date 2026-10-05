"""注册窗口 + 强制开关冒烟用例（T3 最小链路，t9 完整用例在此基础上扩展）。"""

import pytest


class TestRegistrationWindowSmoke:
    """注册窗口：开启 / 查询 / 关闭 / TTL 约束 / 鉴权（t3 端点）。"""

    def test_get_window_closed_by_default(self, client, auth_headers):
        res = client.get("/settings/registration-window", headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert body["open"] is False
        assert "opened_at" in body and "ttl_minutes" in body and "closes_at" in body and "remaining_seconds" in body

    def test_open_window(self, client, auth_headers):
        res = client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        assert res.status_code == 201
        body = res.json()
        assert body["open"] is True
        assert body["ttl_minutes"] == 30
        assert body["remaining_seconds"] > 0

    def test_open_window_ttl_bounds(self, client, auth_headers):
        for bad in (0, 61):
            res = client.post("/settings/registration-window", json={"ttl_minutes": bad}, headers=auth_headers)
            assert res.status_code == 400

    def test_reopen_conflict(self, client, auth_headers):
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        res = client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        assert res.status_code == 409

    def test_close_window(self, client, auth_headers):
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        res = client.delete("/settings/registration-window", headers=auth_headers)
        assert res.status_code == 200
        assert res.json()["open"] is False

    def test_close_without_window_conflict(self, client, auth_headers):
        res = client.delete("/settings/registration-window", headers=auth_headers)
        assert res.status_code == 409

    def test_requires_token(self, client, monkeypatch):
        """无凭据 401（关闭开发模式；认证器链行为见 test_auth.py）。"""
        monkeypatch.setenv("KURRENT_CP_INSECURE", "")
        assert client.get("/settings/registration-window").status_code == 401
        assert client.post("/settings/registration-window", json={"ttl_minutes": 30}).status_code == 401
        assert client.delete("/settings/registration-window").status_code == 401


class TestEnforcementSmoke:
    """设备身份验签强制开关（t4 端点）。"""

    def test_enforcement_default_off(self, client, auth_headers):
        res = client.get("/settings/enforcement", headers=auth_headers)
        assert res.status_code == 200
        assert res.json() == {"enabled": False}

    def test_enforcement_toggle(self, client, auth_headers):
        res = client.put("/settings/enforcement", json={"enabled": True}, headers=auth_headers)
        assert res.status_code == 200
        assert res.json() == {"enabled": True}
        res = client.put("/settings/enforcement", json={"enabled": False}, headers=auth_headers)
        assert res.json() == {"enabled": False}

    def test_enforcement_requires_token(self, client, monkeypatch):
        """无凭据 401（关闭开发模式；认证器链行为见 test_auth.py）。"""
        monkeypatch.setenv("KURRENT_CP_INSECURE", "")
        assert client.get("/settings/enforcement").status_code == 401
        assert client.put("/settings/enforcement", json={"enabled": True}).status_code == 401
