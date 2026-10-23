"""审计日志域（Bearer 鉴权）：游标分页 + 设备 MAC 过滤。"""

MAC_A = "00:11:22:33:44:55"
MAC_B = "00:11:22:33:44:66"


class TestOperations:
    """/operations：审计读取、分页、mac 过滤。"""

    def test_empty_log(self, client, auth_headers):
        res = client.get("/operations", headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert body["entries"] == []
        assert body["next_cursor"] == 0

    def test_requires_token(self, client, monkeypatch):
        """无凭据 401（关闭开发模式；认证器链行为见 test_auth.py）。"""
        monkeypatch.setenv("KURRENT_CP_INSECURE", "")
        assert client.get("/operations").status_code == 401

    def test_records_operations(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        body = client.get("/operations", headers=auth_headers).json()
        assert len(body["entries"]) == 1
        entry = body["entries"][0]
        assert entry["op"] == "device.register"
        assert entry["status"] == "ok"
        assert entry["mac"] == MAC_A
        assert entry["id"] >= 1
        assert "ts" in entry

    def test_operation_client_uses_nginx_real_ip(self, client, auth_headers):
        """控制面在 UDS 后拿不到 TCP 对端，操作审计的来源取 nginx 覆盖写入的 X-Real-IP。"""
        client.post("/devices", json={"mac": MAC_A},
                    headers={**auth_headers, "X-Real-IP": "203.0.113.9"})
        entry = client.get("/operations", headers=auth_headers).json()["entries"][0]
        assert entry["client"] == "203.0.113.9"

    def test_pagination_since_limit(self, client, auth_headers):
        for mac in (MAC_A, MAC_B):
            client.post("/devices", json={"mac": mac}, headers=auth_headers)
        client.post("/devices", json={"mac": "00:11:22:33:44:99"}, headers=auth_headers)
        # limit=2 游标分页（id 为进程内自增，不断言绝对值）
        page1 = client.get("/operations", params={"limit": 2}, headers=auth_headers).json()
        assert len(page1["entries"]) == 2
        assert page1["next_cursor"] == page1["entries"][-1]["id"]
        page2 = client.get("/operations", params={"since": page1["next_cursor"]}, headers=auth_headers).json()
        assert len(page2["entries"]) == 1
        assert page2["next_cursor"] == page2["entries"][-1]["id"]

    def test_filter_by_mac(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/devices", json={"mac": MAC_B}, headers=auth_headers)
        res = client.get("/operations", params={"mac": MAC_A}, headers=auth_headers)
        assert res.status_code == 200
        body = res.json()
        assert len(body["entries"]) == 1
        assert body["entries"][0]["mac"] == MAC_A
        assert body["next_cursor"] == body["entries"][-1]["id"]

    def test_filter_by_mac_empty(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        body = client.get("/operations", params={"mac": "00:99:88:77:66:55"}, headers=auth_headers).json()
        assert body["entries"] == []
        assert body["next_cursor"] == 0

    def test_mac_normalized(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        # 大小写规范化后匹配（契约：canonical_mac 仅接受冒号分隔格式）
        body = client.get("/operations", params={"mac": MAC_A.upper()}, headers=auth_headers).json()
        assert len(body["entries"]) == 1
