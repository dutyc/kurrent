"""设备登记链路（boot 域，不鉴权）：/devices/report 上报/注册/认领 + /devices/challenge 挑战。

契约要点：
- 注册只在窗口期，且须带有效 TPM AK（SPKI DER）、TPM2B_NAME 与 sb=1
- 存量设备窗口期内带 AK 上报 = 身份认领；AK 不一致拒绝覆盖
- 吊销设备不更新、不复活
"""

MAC_A = "00:11:22:33:44:55"
MAC_B = "00:11:22:33:44:66"
MAC_C = "00:11:22:33:44:77"
MAC_D = "00:11:22:33:44:88"


def _report(client, mac, tpm_identity, **extra):
    params = {"mac": mac, **tpm_identity["report_params"], **extra}
    return client.get("/devices/report", params=params)


class TestDeviceReport:
    """/devices/report：宽松上报（空响应），窗口期注册/认领语义。"""

    def test_invalid_mac_ignored(self, client, auth_headers):
        res = client.get("/devices/report", params={"mac": "zzz-not-a-mac"})
        assert res.status_code == 200
        assert client.get("/devices", headers=auth_headers).json() == []

    def test_unknown_mac_outside_window_not_registered(self, client, auth_headers, tpm_identity):
        # 窗口未开 + 带 AK → 不注册（200 空响应，不阻断引导）
        assert _report(client, MAC_A, tpm_identity).status_code == 200
        assert client.get("/devices", headers=auth_headers).json() == []

    def test_unknown_mac_without_ak_not_registered(self, client, auth_headers):
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        res = client.get("/devices/report", params={"mac": MAC_A, "sb": "1"})
        assert res.status_code == 200
        assert client.get("/devices", headers=auth_headers).json() == []
        ops = client.get("/operations", headers=auth_headers).json()["entries"]
        assert any(e["op"] == "device.register" and e["status"] == "rejected"
                   and e.get("reason") == "no_tpm" and e.get("mac") == MAC_A for e in ops)

    def test_secure_boot_disabled_not_registered(self, client, auth_headers, tpm_identity):
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        assert _report(client, MAC_A, tpm_identity, sb="0").status_code == 200
        assert client.get("/devices", headers=auth_headers).json() == []
        ops = client.get("/operations", headers=auth_headers).json()["entries"]
        assert any(e["op"] == "device.register" and e["status"] == "rejected"
                   and e.get("reason") == "sb_disabled" for e in ops)

    def test_register_in_window_with_ak(self, client, auth_headers, tpm_identity):
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        assert _report(client, MAC_A, tpm_identity).status_code == 200
        dev = client.get(f"/devices/{MAC_A}", headers=auth_headers).json()
        assert dev["state"] == "pooled"
        assert dev["identity"]["kind"] == "tpm_ak"
        assert dev["identity"]["ak_public"] == tpm_identity["ak_public"]
        assert dev["identity"]["ak_name"] == tpm_identity["ak_name"]
        assert dev["attestation"]["state"] == "unproven"
        assert dev["source"] == "ipxe"
        assert dev["bound_worker_id"] is None
        ops = client.get("/operations", headers=auth_headers).json()["entries"]
        assert any(e["op"] == "device.register" and e["status"] == "ok" and e.get("mac") == MAC_A for e in ops)
        assert any(e["op"] == "device.ak.register" and e.get("mac") == MAC_A for e in ops)

    def test_invalid_ak_not_registered(self, client, auth_headers):
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        for bad in ("abc", "00" * 91):
            res = client.get("/devices/report", params={
                "mac": MAC_A, "ak_public": bad, "ak_name": "00" * 34,
                "ak_type": "ecc_p256", "sb": "1"})
            assert res.status_code == 200
        assert client.get("/devices", headers=auth_headers).json() == []

    def test_claim_existing_device_in_window(self, client, auth_headers, tpm_identity):
        # 手动注册（无 AK）→ 窗口期内带 AK 上报 → 认领
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        assert _report(client, MAC_A, tpm_identity).status_code == 200
        dev = client.get(f"/devices/{MAC_A}", headers=auth_headers).json()
        assert dev["identity"]["ak_public"] == tpm_identity["ak_public"]
        ops = client.get("/operations", headers=auth_headers).json()["entries"]
        assert any(e["op"] == "device.claim" and e["status"] == "ok" and e.get("mac") == MAC_A for e in ops)

    def test_ak_mismatch_rejected(self, client, auth_headers, tpm_identity, register_claimed_device):
        register_claimed_device(MAC_A)
        # 另一把 AK → 拒绝覆盖（identity 不变 + 审计 rejected）
        from cryptography.hazmat.primitives.asymmetric import ec as ecm
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        other = ecm.generate_private_key(ecm.SECP256R1())
        other_hex = other.public_key().public_bytes(
            Encoding.DER, PublicFormat.SubjectPublicKeyInfo).hex()
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        res = client.get("/devices/report", params={
            "mac": MAC_A, "ak_public": other_hex, "ak_name": "00" * 34,
            "ak_type": "ecc_p256", "sb": "1"})
        assert res.status_code == 200
        dev = client.get(f"/devices/{MAC_A}", headers=auth_headers).json()
        assert dev["identity"]["ak_public"] == tpm_identity["ak_public"]
        ops = client.get("/operations", headers=auth_headers).json()["entries"]
        assert any(e["op"] == "device.claim" and e["status"] == "rejected"
                   and e.get("reason") == "ak_mismatch" and e.get("mac") == MAC_A for e in ops)

    def test_revoked_device_not_updated_or_resurrected(self, client, auth_headers, tpm_identity):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.delete(f"/devices/{MAC_A}", headers=auth_headers)  # revoked
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        assert _report(client, MAC_A, tpm_identity, manufacturer="Hacker Inc").status_code == 200
        dev = client.get(f"/devices/{MAC_A}", headers=auth_headers).json()
        assert dev["state"] == "revoked"
        assert dev["identity"] is None  # 吊销不复活、不认领
        assert dev["fingerprint"].get("manufacturer") is None

    def test_report_updates_fingerprint(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A, "manufacturer": "Dell"}, headers=auth_headers)
        res = client.get("/devices/report", params={"mac": MAC_A, "manufacturer": "Lenovo",
                                                    "serial": "SN-123", "mem-total": "0x1000"})
        assert res.status_code == 200
        dev = client.get(f"/devices/{MAC_A}", headers=auth_headers).json()
        assert dev["fingerprint"]["manufacturer"] == "Lenovo"
        assert dev["fingerprint"]["serial"] == "SN-123"
        assert dev["fingerprint"]["mem_total"] == 4096  # 0x1000 宽松解析
        assert dev["last_seen"] is not None

    def test_report_cannot_overwrite_uuid(self, client, auth_headers):
        """无鉴权上报只能补空 UUID，不能覆盖已有绑定比对依据。"""
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.get("/devices/report", params={"mac": MAC_A, "uuid": "first-uuid"})
        assert client.get(f"/devices/{MAC_A}", headers=auth_headers).json()["uuid"] == "first-uuid"

        client.get("/devices/report", params={"mac": MAC_A, "uuid": "spoofed-uuid"})
        assert client.get(f"/devices/{MAC_A}", headers=auth_headers).json()["uuid"] == "first-uuid"

    def test_report_requires_no_token(self, client, auth_headers, tpm_identity):
        # boot 域不鉴权：无 Authorization 也能上报
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        assert _report(client, MAC_A, tpm_identity).status_code == 200


class TestDeviceChallenge:
    """/devices/challenge：一次性 nonce 签发（绑定 mac、短 TTL）。"""

    def test_challenge_unregistered_404(self, client):
        res = client.get("/devices/challenge", params={"mac": MAC_A})
        assert res.status_code == 404

    def test_challenge_unclaimed_404(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)  # 无 AK
        res = client.get("/devices/challenge", params={"mac": MAC_A})
        assert res.status_code == 404

    def test_challenge_invalid_mac_400(self, client):
        res = client.get("/devices/challenge", params={"mac": "zzz"})
        assert res.status_code == 400

    def test_challenge_claimed_returns_nonce(self, client, auth_headers, register_claimed_device):
        register_claimed_device(MAC_A)
        res = client.get("/devices/challenge", params={"mac": MAC_A})
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/plain")
        # 响应为 #!ipxe 脚本体：set nonce <64hex>（iPXE chain 可直接消费）
        lines = res.text.strip().splitlines()
        assert lines[0] == "#!ipxe"
        assert len(lines) == 2 and lines[1].startswith("set nonce ")
        nonce = lines[1][len("set nonce "):]
        assert len(nonce) == 64
        assert int(nonce, 16) >= 0  # 64 hex 字符

    def test_challenge_no_token_required(self, client, auth_headers, register_claimed_device):
        register_claimed_device(MAC_A)
        assert client.get("/devices/challenge", params={"mac": MAC_A}).status_code == 200


class TestRegistrationWindowExpiry:
    """窗口 TTL 到期自动关闭（懒计算）。"""

    def test_expired_window_closed(self, client, auth_headers, tpm_identity):
        import datetime as _dt

        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        # 直接改写 settings.json：opened_at 改为 1 小时前 → TTL 已过
        from control_plane.app.stores import runtime_settings
        with runtime_settings.locked():
            runtime_settings.set("registration_window", {
                "opened_at": (_dt.datetime.now().astimezone() - _dt.timedelta(hours=1)).isoformat(),
                "ttl_minutes": 30,
            })
        body = client.get("/settings/registration-window", headers=auth_headers).json()
        assert body["open"] is False
        assert body["remaining_seconds"] == 0
        # 过期后 report 不再注册新设备
        assert _report(client, MAC_B, tpm_identity).status_code == 200
        assert client.get("/devices", headers=auth_headers).json() == []

    def test_expired_window_reopen_overwrites(self, client, auth_headers):
        import datetime as _dt

        from control_plane.app.stores import runtime_settings
        with runtime_settings.locked():
            runtime_settings.set("registration_window", {
                "opened_at": (_dt.datetime.now().astimezone() - _dt.timedelta(hours=1)).isoformat(),
                "ttl_minutes": 30,
            })
        res = client.post("/settings/registration-window", json={"ttl_minutes": 15}, headers=auth_headers)
        assert res.status_code == 201
        assert res.json()["open"] is True
        assert res.json()["ttl_minutes"] == 15


class TestRegistrationWindowQuota:
    """窗口放行额度（max_devices）：只约束未知设备自动入池，不约束 AK 认领与手动登记。
    已放行数派生自设备台账（窗口期内 source=ipxe 且 first_seen >= 开启时刻的条目），不另存计数器。"""

    def test_quota_stops_admission(self, client, auth_headers, tpm_identity):
        # 额度 2：前两台入池，第三台被拒（200 空响应，不阻断引导）
        res = client.post(
            "/settings/registration-window", json={"ttl_minutes": 30, "max_devices": 2}, headers=auth_headers
        )
        assert res.status_code == 201
        for mac in (MAC_A, MAC_B):
            assert _report(client, mac, tpm_identity).status_code == 200
        assert _report(client, MAC_C, tpm_identity).status_code == 200
        macs = [d["mac"] for d in client.get("/devices", headers=auth_headers).json()]
        assert sorted(macs) == sorted([MAC_A, MAC_B])
        entries = client.get("/operations", headers=auth_headers).json()["entries"]
        assert any(e.get("mac") == MAC_C and e["status"] == "rejected"
                   and e.get("reason") == "window_closed" for e in entries)

    def test_status_projection_counts_admitted(self, client, auth_headers, tpm_identity):
        client.post(
            "/settings/registration-window", json={"ttl_minutes": 30, "max_devices": 2}, headers=auth_headers
        )
        body = client.get("/settings/registration-window", headers=auth_headers).json()
        assert body["max_devices"] == 2
        assert body["admitted"] == 0
        assert body["remaining_devices"] == 2
        assert body["quota_exhausted"] is False

        _report(client, MAC_A, tpm_identity)
        body = client.get("/settings/registration-window", headers=auth_headers).json()
        assert body["admitted"] == 1
        assert body["remaining_devices"] == 1
        assert body["quota_exhausted"] is False

        _report(client, MAC_B, tpm_identity)
        body = client.get("/settings/registration-window", headers=auth_headers).json()
        assert body["admitted"] == 2
        assert body["remaining_devices"] == 0
        assert body["quota_exhausted"] is True
        # 额度用尽不主动清窗口记录：时间维度仍为开启（等待管理员关闭或 TTL 到期）
        assert body["open"] is True

    def test_quota_omitted_means_unlimited(self, client, auth_headers, tpm_identity):
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        body = client.get("/settings/registration-window", headers=auth_headers).json()
        assert body["max_devices"] is None
        assert body["remaining_devices"] is None
        assert body["quota_exhausted"] is False
        for mac in (MAC_A, MAC_B, MAC_C):
            _report(client, mac, tpm_identity)
        assert len(client.get("/devices", headers=auth_headers).json()) == 3
        assert client.get("/settings/registration-window", headers=auth_headers).json()["admitted"] == 3

    def test_manual_registration_not_counted(self, client, auth_headers, tpm_identity):
        # 手动登记（source=manual）是管理员显式动作，不占 PXE 放行额度
        client.post(
            "/settings/registration-window", json={"ttl_minutes": 30, "max_devices": 1}, headers=auth_headers
        )
        res = client.post("/devices", json={"mac": MAC_C}, headers=auth_headers)
        assert res.status_code == 201
        assert res.json()["source"] == "manual"
        assert client.get("/settings/registration-window", headers=auth_headers).json()["admitted"] == 0
        # 额度 1 仍完整可用
        _report(client, MAC_A, tpm_identity)
        assert client.get(f"/devices/{MAC_A}", headers=auth_headers).status_code == 200
        assert client.get("/settings/registration-window", headers=auth_headers).json()["quota_exhausted"] is True
        # 用尽后额度内不再放行第二台 PXE 设备
        _report(client, MAC_B, tpm_identity)
        assert client.get(f"/devices/{MAC_B}", headers=auth_headers).status_code == 404

    def test_claim_not_blocked_by_exhausted_quota(self, client, auth_headers, tpm_identity):
        # 存量设备（手动登记，无 AK）的身份认领只看窗口期，不受额度约束
        client.post("/devices", json={"mac": MAC_C}, headers=auth_headers)
        client.post(
            "/settings/registration-window", json={"ttl_minutes": 30, "max_devices": 1}, headers=auth_headers
        )
        _report(client, MAC_A, tpm_identity)  # 额度 1 被 PXE 新设备用掉
        assert _report(client, MAC_C, tpm_identity).status_code == 200
        dev = client.get(f"/devices/{MAC_C}", headers=auth_headers).json()
        assert dev["identity"]["ak_public"] == tpm_identity["ak_public"]  # 认领成功
        assert dev["source"] == "manual"  # 认领不改来源，也不计入放行数
        assert client.get("/settings/registration-window", headers=auth_headers).json()["admitted"] == 1

    def test_reopen_resets_quota(self, client, auth_headers, tpm_identity):
        # 额度用尽 → 关闭 → 重开：新窗口的额度从零起算（已放行数按开启时刻重新派生）
        client.post(
            "/settings/registration-window", json={"ttl_minutes": 30, "max_devices": 1}, headers=auth_headers
        )
        _report(client, MAC_A, tpm_identity)
        _report(client, MAC_B, tpm_identity)
        assert client.get(f"/devices/{MAC_B}", headers=auth_headers).status_code == 404
        client.delete("/settings/registration-window", headers=auth_headers)
        client.post(
            "/settings/registration-window", json={"ttl_minutes": 30, "max_devices": 1}, headers=auth_headers
        )
        assert client.get("/settings/registration-window", headers=auth_headers).json()["admitted"] == 0
        _report(client, MAC_B, tpm_identity)
        assert client.get(f"/devices/{MAC_B}", headers=auth_headers).status_code == 200

    def test_max_devices_bounds(self, client, auth_headers):
        for bad in (0, -1, 1001):
            res = client.post(
                "/settings/registration-window", json={"ttl_minutes": 30, "max_devices": bad}, headers=auth_headers
            )
            assert res.status_code == 400
        # 边界值合法
        assert client.post(
            "/settings/registration-window", json={"ttl_minutes": 30, "max_devices": 1}, headers=auth_headers
        ).status_code == 201
        assert client.delete("/settings/registration-window", headers=auth_headers).status_code == 200
        assert client.post(
            "/settings/registration-window", json={"ttl_minutes": 30, "max_devices": 1000}, headers=auth_headers
        ).status_code == 201
