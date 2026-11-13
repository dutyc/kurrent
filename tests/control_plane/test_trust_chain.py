"""信任根验签链路（boot 域）：/boot-vars TPM 证明 + 强制开关拦截 + 注入投影。

链路：设备认领 AK → challenge 取 nonce → TPM quote（nonce 作为 extraData）→
boot-vars 携带 quote 验签放行。强制开关开启后未登记 AK 的设备拒绝下发。
"""

import pytest

from control_plane.app.config import settings

MAC_A = "00:11:22:33:44:55"
MAC_B = "00:11:22:33:44:66"
MAC_C = "00:11:22:33:44:77"

EMPTY_SCRIPT = "# no per-worker boot vars found"


def _claim_and_bind(client, auth_headers, tpm_identity, mac=MAC_A, worker_id="worker-01"):
    """认领设备（上报 AK）→ 创建绑定 worker → 返回 hostname。"""
    client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
    res = client.get("/devices/report", params={"mac": mac, **tpm_identity["report_params"]})
    assert res.status_code == 200
    res = client.post("/workers", json={"worker_id": worker_id, "mac": mac}, headers=auth_headers)
    assert res.status_code == 201, res.text
    return res.json()["hostname"]


def _bind_without_ak(client, auth_headers, mac=MAC_B, worker_id="worker-02"):
    """无 AK 设备（手动注册）→ 创建绑定 worker → 返回 hostname。"""
    assert client.post("/devices", json={"mac": mac}, headers=auth_headers).status_code == 201
    res = client.post("/workers", json={"worker_id": worker_id, "mac": mac}, headers=auth_headers)
    assert res.status_code == 201, res.text
    return res.json()["hostname"]


def _challenge(client, mac=MAC_A) -> str:
    res = client.get("/devices/challenge", params={"mac": mac})
    assert res.status_code == 200, res.text
    # 响应为 #!ipxe 脚本体：set nonce <64hex>（iPXE chain 可直接消费）
    lines = [l for l in res.text.strip().splitlines() if l.startswith("set nonce ")]
    assert len(lines) == 1, res.text
    return lines[0][len("set nonce "):]


def _quote(tpm_identity, nonce: str) -> dict:
    signed = tpm_identity["quote"](nonce)
    return {"quote": signed["quote"], "quote_sig": signed["quote_sig"]}


def _boot_vars(client, *, mac=None, hostname=None, nonce=None, quote=None,
               quote_sig=None, format_="ipxe"):
    params = {"format": format_}
    if mac:
        params["mac"] = mac
    if hostname:
        params["hostname"] = hostname
    if nonce:
        params["nonce"] = nonce
    if quote:
        params["quote"] = quote
    if quote_sig:
        params["quote_sig"] = quote_sig
    return client.get("/boot-vars", params=params)


class TestAttestationChain:
    """TPM quote 验签：有效证明放行，重放/错签/跨 MAC 拒绝。"""

    def test_valid_quote_allows_boot_vars(self, client, auth_headers, tpm_identity):
        hostname = _claim_and_bind(client, auth_headers, tpm_identity)
        nonce = _challenge(client)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce,
                         **_quote(tpm_identity, nonce))
        assert res.status_code == 200
        assert "set menu-default reboot" in res.text  # 无盘 worker → 默认 reboot 循环
        assert "set menu-timeout" in res.text

    def test_replay_nonce_rejected(self, client, auth_headers, tpm_identity):
        hostname = _claim_and_bind(client, auth_headers, tpm_identity)
        nonce = _challenge(client)
        params = _quote(tpm_identity, nonce)
        assert _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce,
                          **params).status_code == 200
        # 同一 nonce + quote 重放 → 拒绝（nonce 一次性，取出即删）
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce, **params)
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text
        ops = client.get("/operations", params={"mac": MAC_A}, headers=auth_headers).json()["entries"]
        assert any(e["op"] == "boot_vars.credential" and e["status"] == "rejected"
                   and e.get("reason") == "nonce_invalid" for e in ops)

    def test_new_challenge_keeps_outstanding_nonce_valid(self, client, auth_headers, tpm_identity):
        """无鉴权 challenge 不能再顶掉设备已持有的 nonce（引导 DoS 回归）。"""
        hostname = _claim_and_bind(client, auth_headers, tpm_identity)
        first = _challenge(client)
        second = _challenge(client)
        assert first != second
        # 第一次挑战拿到的 nonce 仍然可用
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=first,
                         **_quote(tpm_identity, first))
        assert "set menu-default reboot" in res.text
        # 第二次挑战的 nonce 未被牵连
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=second,
                         **_quote(tpm_identity, second))
        assert "set menu-default reboot" in res.text

    def test_bogus_nonce_does_not_clear_pending_nonce(self, client, auth_headers, tpm_identity):
        """错误 nonce 只失败自身，不得清空设备的待用 nonce。"""
        hostname = _claim_and_bind(client, auth_headers, tpm_identity)
        nonce = _challenge(client)
        bogus = "00" * 32
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=bogus,
                         **_quote(tpm_identity, bogus))
        assert EMPTY_SCRIPT in res.text
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce,
                         **_quote(tpm_identity, nonce))
        assert "set menu-default reboot" in res.text

    def test_wrong_signature_rejected(self, client, auth_headers, tpm_identity):
        hostname = _claim_and_bind(client, auth_headers, tpm_identity)
        nonce = _challenge(client)
        # 用另一把私钥对同一 quote 签名（错误密钥）
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec as ecm
        signed = _quote(tpm_identity, nonce)
        other = ecm.generate_private_key(ecm.SECP256R1())
        bad_sig = other.sign(bytes.fromhex(signed["quote"]), ecm.ECDSA(hashes.SHA256())).hex()
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce,
                         quote=signed["quote"], quote_sig=bad_sig)
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text
        ops = client.get("/operations", params={"mac": MAC_A}, headers=auth_headers).json()["entries"]
        assert any(e["op"] == "boot_vars.credential" and e["status"] == "rejected"
                   and e.get("reason") == "quote_invalid" for e in ops)

    def test_quote_for_other_mac_rejected(self, client, auth_headers, tpm_identity):
        _claim_and_bind(client, auth_headers, tpm_identity)
        client.get("/devices/report", params={"mac": MAC_B, **tpm_identity["report_params"]})
        nonce_b = _challenge(client, MAC_B)
        # 用 MAC_B 的 nonce 请求 MAC_A：nonce 绑定 MAC，跨设备不可用
        res = _boot_vars(client, mac=MAC_A, nonce=nonce_b, **_quote(tpm_identity, nonce_b))
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text
        ops = client.get("/operations", params={"mac": MAC_A}, headers=auth_headers).json()["entries"]
        assert any(e["op"] == "boot_vars.credential" and e["status"] == "rejected"
                   and e.get("reason") == "nonce_invalid" for e in ops)


class TestEnforcement:
    """强制开关：默认开启，无 AK/无证明设备一律拒绝（已绑定也不放行）；显式关闭时放行。"""

    def test_enforcement_rejects_missing_mac(self, client, auth_headers):
        """仅 hostname、无 mac：强制开启后不再放行（报告 F-01 的入口）。"""
        client.post("/workers", json={"worker_id": "worker-09"}, headers=auth_headers)
        client.put("/settings/enforcement", json={"enabled": True}, headers=auth_headers)
        res = _boot_vars(client, hostname="worker-09")
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text
        ops = client.get("/operations", params={"worker_id": "worker-09"}, headers=auth_headers).json()["entries"]
        assert any(e["op"] == "boot_vars.credential" and e["status"] == "rejected"
                   and e.get("reason") == "missing_mac" for e in ops)

    def test_enforcement_rejects_no_ak_even_bound(self, client, auth_headers):
        _bind_without_ak(client, auth_headers)
        client.put("/settings/enforcement", json={"enabled": True}, headers=auth_headers)
        res = _boot_vars(client, mac=MAC_B)
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text
        ops = client.get("/operations", params={"mac": MAC_B}, headers=auth_headers).json()["entries"]
        assert any(e["op"] == "boot_vars.credential" and e["status"] == "rejected"
                   and e.get("reason") == "ak_unregistered" for e in ops)

    def test_enforcement_rejects_missing_quote(self, client, auth_headers, tpm_identity):
        hostname = _claim_and_bind(client, auth_headers, tpm_identity)
        client.put("/settings/enforcement", json={"enabled": True}, headers=auth_headers)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname)  # 无 nonce/quote
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text
        ops = client.get("/operations", params={"mac": MAC_A}, headers=auth_headers).json()["entries"]
        assert any(e["op"] == "boot_vars.credential" and e["status"] == "rejected"
                   and e.get("reason") == "quote_invalid" for e in ops)

    def test_enforcement_allows_missing_hostname(self, client, auth_headers, tpm_identity):
        """证明只绑定 nonce：DHCP 未提供 hostname 时不再阻断引导。"""
        _claim_and_bind(client, auth_headers, tpm_identity)
        client.put("/settings/enforcement", json={"enabled": True}, headers=auth_headers)
        nonce = _challenge(client)
        res = _boot_vars(client, mac=MAC_A, nonce=nonce, **_quote(tpm_identity, nonce))  # 无 hostname
        assert res.status_code == 200
        assert "set menu-default" in res.text

    def test_enforcement_allows_valid_quote(self, client, auth_headers, tpm_identity):
        hostname = _claim_and_bind(client, auth_headers, tpm_identity)
        client.put("/settings/enforcement", json={"enabled": True}, headers=auth_headers)
        nonce = _challenge(client)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce,
                         **_quote(tpm_identity, nonce))
        assert res.status_code == 200
        assert "set menu-default" in res.text

    def test_disabled_allows_no_ak(self, client, auth_headers):
        # 显式关闭（过渡/排障）：无 AK 已绑定设备照现状放行
        client.put("/settings/enforcement", json={"enabled": False}, headers=auth_headers)
        _bind_without_ak(client, auth_headers)
        res = _boot_vars(client, mac=MAC_B)
        assert res.status_code == 200
        assert "set menu-default" in res.text


class TestBootVarsProjection:
    """boot-vars 注入投影：未注册/池中/绑定 worker 的分支语义。"""

    def test_unregistered_device_gets_empty_script(self, client):
        res = _boot_vars(client, mac="00:99:88:77:66:55")
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text

    def test_pooled_device_gets_reboot_payload(self, client, auth_headers, tpm_identity):
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        client.get("/devices/report", params={"mac": MAC_A, **tpm_identity["report_params"]})
        res = _boot_vars(client, mac=MAC_A)
        assert res.status_code == 200
        assert "set menu-default reboot" in res.text  # 池中未绑定：reboot 循环等待绑定

    def test_revoked_device_gets_empty_script(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.delete(f"/devices/{MAC_A}", headers=auth_headers)
        res = _boot_vars(client, mac=MAC_A)
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text

    def test_unbound_other_device_gets_empty_script(self, client, auth_headers, tpm_identity):
        # 防冒领：绑定 worker-01 的设备是 MAC_A，MAC_C 带 worker-01 hostname 请求 → 拒绝
        hostname = _claim_and_bind(client, auth_headers, tpm_identity, mac=MAC_A, worker_id="worker-01")
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        client.get("/devices/report", params={"mac": MAC_C, **tpm_identity["report_params"]})
        res = _boot_vars(client, mac=MAC_C, hostname=hostname)
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text

    def test_hostname_only_allowed_when_disabled(self, client, auth_headers):
        # 无 mac（仅 hostname）：显式关闭开关时放行菜单变量，但不注入凭据（凭据只认证明）
        client.post("/workers", json={"worker_id": "worker-09"}, headers=auth_headers)
        client.put("/settings/enforcement", json={"enabled": False}, headers=auth_headers)
        res = _boot_vars(client, hostname="worker-09")
        assert res.status_code == 200
        assert "set menu-default reboot" in res.text

    def test_format_validation(self, client):
        assert _boot_vars(client, format_="xml").status_code == 400

    def test_json_format(self, client, auth_headers, tpm_identity):
        hostname = _claim_and_bind(client, auth_headers, tpm_identity)
        nonce = _challenge(client)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce,
                         format_="json", **_quote(tpm_identity, nonce))
        assert res.status_code == 200
        body = res.json()
        assert body["menu_default"] == "reboot"
        assert body["menu_timeout"] > 0


class TestBootVarsWithDisk:
    """绑定 + 建盘后的完整注入投影（mock AgentClient 走全链路）。"""

    def test_bound_worker_with_disk_projects_iscsi(self, client, auth_headers, tpm_identity, mock_agent_client):
        # 注册 agent（role.disk）+ mock client → 建盘 → 验签后投影 iqn/server/sep
        client.post("/agents", json={
            "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
            "role": {"disk": True, "cd": False}, "tags": ["storage", "stgt"],
        }, headers=auth_headers)
        hostname = _claim_and_bind(client, auth_headers, tpm_identity, mac=MAC_A, worker_id="worker-01")
        res = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
        }, headers=auth_headers)
        assert res.status_code == 201, res.text
        assert res.json()["state"] == "ready"
        disk = res.json()["disks"][0]

        nonce = _challenge(client)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce,
                         **_quote(tpm_identity, nonce))
        assert res.status_code == 200
        assert f"set disk-nqn {disk['nqn']}" in res.text  # 完整盘 NQN 原样下发（盘记录权威值）
        assert "set base-nqn" not in res.text  # 不再下发前缀，固件不重拼
        assert "set base-iqn iqn.2026-07.com.test" in res.text  # base-iqn 为前缀，worker 后缀由 iPXE 拼装
        assert "set storager-ip a1b2c3d4e5f6" in res.text  # storager_ip 缺省回退 base_url 主机名
        assert "set iscsi-sep :::1:" in res.text  # stgt 后端差异连接符
        assert "set os ubuntu" in res.text  # 盘 os 投影（默认盘 = 唯一盘）
        assert "set menu-default boot-os" in res.text  # 单盘建盘自动成为默认盘 → 通用 OS 项

    def test_backend_from_capabilities_not_stale_tags(self, client, auth_headers, tpm_identity, mock_agent_client):
        """后端类型以 Agent 自报的能力为准，登记信息里的过期标签不得覆盖实况。

        回归：原实现优先读 agents.yml 的 tags（命中即返回），标签与实况漂移时按过期值拼连接符。
        此处注册时 tags 带陈旧的 nvmet，Agent /capabilities 实报 stgt → 必须按 stgt 拼（发 iscsi-sep）。
        """
        client.post("/agents", json={
            "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
            "role": {"disk": True, "cd": False}, "tags": ["storage", "nvmet"],
        }, headers=auth_headers)
        hostname = _claim_and_bind(client, auth_headers, tpm_identity, mac=MAC_A, worker_id="worker-01")
        res = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
        }, headers=auth_headers)
        assert res.status_code == 201, res.text

        nonce = _challenge(client)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce,
                         **_quote(tpm_identity, nonce))
        assert res.status_code == 200
        assert "set iscsi-sep :::1:" in res.text  # mock capabilities 实报 stgt，压过陈旧的 nvmet 标签

    def test_disabled_agent_still_projects_boot_vars(self, client, auth_headers, tpm_identity, mock_agent_client):
        """enabled=排空语义的边界回归：停用只拦新增，存量盘的引导投影照常
        （storager_ip_for 无 enabled 守卫是裁定的一部分，不是遗漏）。"""
        client.post("/agents", json={
            "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
            "role": {"disk": True, "cd": False}, "tags": ["storage", "stgt"],
        }, headers=auth_headers)
        hostname = _claim_and_bind(client, auth_headers, tpm_identity, mac=MAC_A, worker_id="worker-01")
        res = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
        }, headers=auth_headers)
        assert res.status_code == 201, res.text
        disk = res.json()["disks"][0]
        res = client.put("/agents/a1b2c3d4e5f6", json={
            "base_url": "http://a1b2c3d4e5f6:8000", "role": {"disk": True, "cd": False},
            "tags": ["storage"], "enabled": False,
        }, headers=auth_headers)
        assert res.status_code == 200

        nonce = _challenge(client)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce,
                         **_quote(tpm_identity, nonce))
        assert res.status_code == 200
        assert "set storager-ip a1b2c3d4e5f6" in res.text  # 存量盘照常投影
        assert f"set disk-nqn {disk['nqn']}" in res.text

    def test_default_disk_projects_boot_vars(self, client, auth_headers, tpm_identity, mock_agent_client):
        client.post("/agents", json={
            "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
            "role": {"disk": True, "cd": False}, "tags": ["storage", "stgt"],
        }, headers=auth_headers)
        hostname = _claim_and_bind(client, auth_headers, tpm_identity, mac=MAC_A, worker_id="worker-01")
        disk = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
        }, headers=auth_headers).json()["disks"][0]
        os_tag = disk["os_tag"]
        res = client.put("/workers/worker-01/default-disk", json={"disk": os_tag}, headers=auth_headers)
        assert res.status_code == 200, res.text
        assert res.json()["default_disk"] == os_tag

        nonce = _challenge(client)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce,
                         **_quote(tpm_identity, nonce))
        # 2026-08-30 MAIN MENU 动态化：default_disk 归一到通用项 boot-os，os/disk-nqn 投影默认盘身份
        assert "set menu-default boot-os" in res.text
        assert "set os ubuntu" in res.text
        assert f"set disk-nqn {disk['nqn']}" in res.text
        assert "set os-tag" not in res.text  # NQN 已完整下发，不再暴露后缀
        assert "set os-version" not in res.text  # 无版本 → 不下发
        # default_disk > boot.menu_default 推导链：显式 menu_default 不覆盖 default_disk
        client.put("/workers/worker-01/default-disk", json={"menu_default": "reboot"}, headers=auth_headers)
        nonce2 = _challenge(client)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce2,
                         **_quote(tpm_identity, nonce2))
        assert "set menu-default boot-os" in res.text
        assert "set os ubuntu" in res.text
        assert f"set disk-nqn {disk['nqn']}" in res.text

    def test_default_disk_projects_os_version(self, client, auth_headers, tpm_identity, mock_agent_client):
        """带版本的默认盘：boot-vars 投影 os-version（menu 侧 os-label 拼版本展示）。"""
        client.post("/agents", json={
            "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
            "role": {"disk": True, "cd": False}, "tags": ["storage", "stgt"],
        }, headers=auth_headers)
        hostname = _claim_and_bind(client, auth_headers, tpm_identity, mac=MAC_A, worker_id="worker-01")
        disk = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "os_version": "24.04", "name": "ubuntu-24.04-master",
        }, headers=auth_headers).json()["disks"][0]
        client.put("/workers/worker-01/default-disk", json={"disk": disk["os_tag"]}, headers=auth_headers)

        nonce = _challenge(client)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce,
                         **_quote(tpm_identity, nonce))
        assert "set menu-default boot-os" in res.text
        assert "set os ubuntu" in res.text
        assert "set os-version 24.04" in res.text
        assert f"set disk-nqn {disk['nqn']}" in res.text


class TestDeviceBindingAuthority:
    """设备台账 bound_worker_id 是绑定权威；hostname/dnsmasq 投影只作兜底，不得反向覆盖。"""

    def test_device_binding_beats_stale_dnsmasq_projection(self, client, auth_headers, tpm_identity):
        # 设备台账（权威）绑定到 worker-01；伪造滞后的 dnsmasq 投影指向 worker-02
        _claim_and_bind(client, auth_headers, tpm_identity, mac=MAC_A, worker_id="worker-01")
        assert client.post("/workers", json={"worker_id": "worker-02"}, headers=auth_headers).status_code == 201
        settings.dnsmasq_hosts_file.write_text(f"{MAC_A},worker-02\n", encoding="utf-8")
        # 关闭验签强制，单独验证绑定权威（默认开启时该请求会因缺证明被拒）
        client.put("/settings/enforcement", json={"enabled": False}, headers=auth_headers)
        res = _boot_vars(client, mac=MAC_A, hostname="worker-02")
        assert res.status_code == 200
        assert "# boot vars for worker-01" in res.text


class TestBaselineApproval:
    """PCR 基线：首次 TOFU 记录，变化拒绝并留待批摘要，管理员批准后放行。"""

    def _prove(self, client, tpm_identity, hostname, digest=None):
        nonce = _challenge(client)
        signed = tpm_identity["quote"](nonce, digest) if digest else tpm_identity["quote"](nonce)
        return _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce,
                          quote=signed["quote"], quote_sig=signed["quote_sig"])

    def test_tofu_baseline_then_mismatch_then_approve(self, client, auth_headers, tpm_identity):
        hostname = _claim_and_bind(client, auth_headers, tpm_identity)

        res = self._prove(client, tpm_identity, hostname)
        assert "set menu-default reboot" in res.text
        dev = client.get(f"/devices/{MAC_A}", headers=auth_headers).json()
        assert dev["pcr_baseline"]["digest_sha256"] == tpm_identity["digest"]
        assert dev["pcr_baseline"]["selection"] == [0, 2, 4, 7]
        assert dev["attestation"]["state"] == "proven"
        assert dev["attestation"]["last_reason"] is None

        changed = "cd" * 32
        res = self._prove(client, tpm_identity, hostname, digest=changed)
        assert EMPTY_SCRIPT in res.text
        dev = client.get(f"/devices/{MAC_A}", headers=auth_headers).json()
        assert dev["attestation"]["state"] == "mismatch"
        assert dev["attestation"]["last_reason"] == "pcr_mismatch"
        assert dev["attestation"]["pending_digest"] == changed
        assert dev["pcr_baseline"]["digest_sha256"] == tpm_identity["digest"]  # 基线不动
        ops = settings.operations_file.read_text(encoding="utf-8")
        assert '"device.attest", "status": "ok"' in ops
        assert '"device.attest", "status": "failed"' in ops and '"pcr_mismatch"' in ops

        res = client.post(f"/devices/{MAC_A}/approve-baseline", headers=auth_headers)
        assert res.status_code == 200, res.text
        approved = res.json()
        assert approved["pcr_baseline"]["digest_sha256"] == changed
        assert "pending_digest" not in approved["attestation"]
        ops = settings.operations_file.read_text(encoding="utf-8")
        assert '"device.pcr_baseline.approve"' in ops and changed in ops

        res = self._prove(client, tpm_identity, hostname, digest=changed)
        assert "set menu-default reboot" in res.text
        dev = client.get(f"/devices/{MAC_A}", headers=auth_headers).json()
        assert dev["attestation"]["state"] == "proven"

    def test_approve_without_pending_conflicts(self, client, auth_headers, tpm_identity):
        _claim_and_bind(client, auth_headers, tpm_identity)
        res = client.post(f"/devices/{MAC_A}/approve-baseline", headers=auth_headers)
        assert res.status_code == 409

    def test_approve_unknown_device(self, client, auth_headers):
        res = client.post(f"/devices/{MAC_B}/approve-baseline", headers=auth_headers)
        assert res.status_code == 404

    def test_approve_revoked_device(self, client, auth_headers):
        assert client.post("/devices", json={"mac": MAC_A}, headers=auth_headers).status_code == 201
        assert client.delete(f"/devices/{MAC_A}", headers=auth_headers).status_code == 200
        res = client.post(f"/devices/{MAC_A}/approve-baseline", headers=auth_headers)
        assert res.status_code == 403
