"""平台密钥注册（Secure Boot 网络注册）：窗口开关、阶段脚本、上报与已注册台账。"""

MAC = "52:54:00:50:00:21"


class TestPlatformEnrollment:
    """/settings/platform-enrollment 与引导侧 /boot/phase、/devices/enroll-report。"""

    def test_window_closed_by_default(self, client, auth_headers):
        body = client.get("/settings/platform-enrollment", headers=auth_headers).json()
        assert body == {"open": False, "opened_at": None, "enrolled": []}

    def test_open_and_close_window(self, client, auth_headers):
        res = client.post("/settings/platform-enrollment", headers=auth_headers)
        assert res.status_code == 201
        body = res.json()
        assert body["open"] is True
        assert body["opened_at"]
        assert client.post("/settings/platform-enrollment", headers=auth_headers).status_code == 409

        assert client.delete("/settings/platform-enrollment", headers=auth_headers).status_code == 200
        assert client.get("/settings/platform-enrollment", headers=auth_headers).json()["open"] is False
        assert client.delete("/settings/platform-enrollment", headers=auth_headers).status_code == 409

    def test_requires_token(self, client, monkeypatch):
        """无凭据 401（关闭开发模式）。"""
        monkeypatch.setenv("KURRENT_CP_INSECURE", "")
        assert client.get("/settings/platform-enrollment").status_code == 401
        assert client.post("/settings/platform-enrollment").status_code == 401
        assert client.delete("/settings/platform-enrollment").status_code == 401

    def test_phase_script_follows_window(self, client, auth_headers):
        """窗口关闭返回空脚本；开启下发 sbenroll 与上报，不自行 reboot（由上报结果决定）。"""
        closed = client.get("/boot/phase")
        assert closed.status_code == 200
        assert closed.text.strip() == "#!ipxe"

        client.post("/settings/platform-enrollment", headers=auth_headers)
        body = client.get("/boot/phase").text
        assert "sbenroll" in body
        assert "devices/enroll-report?mac=${netX/mac}&result=${sbenroll-result}" in body
        assert "reboot" not in body

    def test_first_ok_report_reboots_and_registers_once(self, client, auth_headers):
        client.post("/settings/platform-enrollment", headers=auth_headers)
        first = client.get("/devices/enroll-report", params={"mac": MAC, "result": "ok"})
        assert first.status_code == 200
        assert first.text.strip().splitlines()[-1] == "reboot"

        # 重复上报：不再重启，继续引导（窗期内的已注册设备不应形成重启循环）
        second = client.get("/devices/enroll-report", params={"mac": MAC, "result": "ok"})
        assert second.text.strip() == "#!ipxe"

        body = client.get("/settings/platform-enrollment", headers=auth_headers).json()
        assert [item["mac"] for item in body["enrolled"]] == [MAC]
        assert body["enrolled"][0]["at"]

        # 关闭窗口后清单保留
        client.delete("/settings/platform-enrollment", headers=auth_headers)
        body = client.get("/settings/platform-enrollment", headers=auth_headers).json()
        assert body["open"] is False
        assert [item["mac"] for item in body["enrolled"]] == [MAC]

    def test_failed_report_does_not_register(self, client, auth_headers):
        for result in ("foreign", "failed"):
            res = client.get("/devices/enroll-report", params={"mac": MAC, "result": result})
            assert res.status_code == 200
            assert res.text.strip() == "#!ipxe"

        body = client.get("/settings/platform-enrollment", headers=auth_headers).json()
        assert body["enrolled"] == []
        ops = client.get("/operations", params={"op": "platform.enroll"},
                         headers=auth_headers).json()["entries"]
        assert [entry["status"] for entry in ops] == ["failed", "failed"]
        assert [entry["result"] for entry in ops] == ["foreign", "failed"]

    def test_invalid_report_is_ignored(self, client, auth_headers):
        assert client.get("/devices/enroll-report", params={"mac": "not-a-mac", "result": "ok"}).text.strip() == "#!ipxe"
        assert client.get("/devices/enroll-report", params={"mac": MAC, "result": "bogus"}).text.strip() == "#!ipxe"
        ops = client.get("/operations", params={"op": "platform.enroll"},
                         headers=auth_headers).json()["entries"]
        assert ops == []
