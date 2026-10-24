"""信任根验签链路（boot 域）：/boot-vars 挑战-响应 + 强制开关拦截 + 注入投影。

链路：设备认领（key_hash）→ challenge 取 nonce → 私钥签名（nonce||mac）→
boot-vars 携带 nonce+sig 验签放行。强制开关开启后无密钥设备拒绝下发（注入四条件第 4 条）。
"""

import pytest

from control_plane.app.config import settings

MAC_A = "00:11:22:33:44:55"
MAC_B = "00:11:22:33:44:66"
MAC_C = "00:11:22:33:44:77"

EMPTY_SCRIPT = "# no per-worker boot vars found"


def _claim_and_bind(client, auth_headers, ec_keypair, mac=MAC_A, worker_id="worker-01"):
    """认领设备 → 创建绑定 worker → 返回 hostname。"""
    client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
    assert client.get("/devices/report", params={"mac": mac, "pubkey": ec_keypair["pubkey_hex"]}).status_code == 200
    res = client.post("/workers", json={"worker_id": worker_id, "mac": mac}, headers=auth_headers)
    assert res.status_code == 201, res.text
    return res.json()["hostname"]


def _bind_without_key(client, auth_headers, mac=MAC_B, worker_id="worker-02"):
    """无密钥设备（手动注册）→ 创建绑定 worker → 返回 hostname。"""
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


def _boot_vars(client, *, mac=None, hostname=None, nonce=None, sig=None, format_="ipxe"):
    params = {"format": format_}
    if mac:
        params["mac"] = mac
    if hostname:
        params["hostname"] = hostname
    if nonce:
        params["nonce"] = nonce
    if sig:
        params["sig"] = sig
    return client.get("/boot-vars", params=params)


class TestSignatureChain:
    """挑战-响应验签：正确签名放行，重放/错签拒绝。"""

    def test_valid_signature_allows_boot_vars(self, client, auth_headers, ec_keypair):
        hostname = _claim_and_bind(client, auth_headers, ec_keypair)
        nonce = _challenge(client)
        sig = ec_keypair["sign"](MAC_A, nonce)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce, sig=sig)
        assert res.status_code == 200
        assert "set menu-default reboot" in res.text  # 无盘 worker → 默认 reboot 循环
        assert "set menu-timeout" in res.text

    def test_replay_nonce_rejected(self, client, auth_headers, ec_keypair):
        hostname = _claim_and_bind(client, auth_headers, ec_keypair)
        nonce = _challenge(client)
        sig = ec_keypair["sign"](MAC_A, nonce)
        assert _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce, sig=sig).status_code == 200
        # 同一 nonce+sig 重放 → 拒绝（nonce 一次性，取出即删）
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce, sig=sig)
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text
        ops = client.get("/operations", params={"mac": MAC_A}, headers=auth_headers).json()["entries"]
        assert any(e["op"] == "boot_vars.credential" and e["status"] == "rejected"
                   and e.get("reason") == "nonce_invalid" for e in ops)

    def test_new_challenge_keeps_outstanding_nonce_valid(self, client, auth_headers, ec_keypair):
        """无鉴权 challenge 不能再顶掉设备已持有的 nonce（引导 DoS 回归）。"""
        hostname = _claim_and_bind(client, auth_headers, ec_keypair)
        first = _challenge(client)
        second = _challenge(client)
        assert first != second
        # 第一次挑战拿到的 nonce 仍然可用
        sig = ec_keypair["sign"](MAC_A, first)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=first, sig=sig)
        assert "set menu-default reboot" in res.text
        # 第二次挑战的 nonce 未被牵连
        sig2 = ec_keypair["sign"](MAC_A, second)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=second, sig=sig2)
        assert "set menu-default reboot" in res.text

    def test_bogus_nonce_does_not_clear_pending_nonce(self, client, auth_headers, ec_keypair):
        """错误 nonce 只失败自身，不得清空设备的待用 nonce。"""
        hostname = _claim_and_bind(client, auth_headers, ec_keypair)
        nonce = _challenge(client)
        bogus = "00" * 32
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=bogus, sig="AA==")
        assert EMPTY_SCRIPT in res.text
        sig = ec_keypair["sign"](MAC_A, nonce)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce, sig=sig)
        assert "set menu-default reboot" in res.text

    def test_wrong_signature_rejected(self, client, auth_headers, ec_keypair):
        hostname = _claim_and_bind(client, auth_headers, ec_keypair)
        nonce = _challenge(client)
        # 用另一把私钥签名（错误密钥）
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec as ecm
        import base64
        other = ecm.generate_private_key(ecm.SECP256R1())
        data = f"{nonce}{MAC_A}".encode("utf-8")
        bad_sig = base64.b64encode(other.sign(data, ecm.ECDSA(hashes.SHA256()))).decode()
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce, sig=bad_sig)
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text
        ops = client.get("/operations", params={"mac": MAC_A}, headers=auth_headers).json()["entries"]
        assert any(e["op"] == "boot_vars.credential" and e["status"] == "rejected"
                   and e.get("reason") == "verify_failed" for e in ops)

    def test_signature_for_other_mac_rejected(self, client, auth_headers, ec_keypair):
        _claim_and_bind(client, auth_headers, ec_keypair)
        nonce = _challenge(client)
        # 用 MAC_B 的签名数据请求 MAC_A：nonce 绑定 MAC_A，验签失败
        sig = ec_keypair["sign"](MAC_B, nonce)
        res = _boot_vars(client, mac=MAC_A, nonce=nonce, sig=sig)
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text
        ops = client.get("/operations", params={"mac": MAC_A}, headers=auth_headers).json()["entries"]
        assert any(e["op"] == "boot_vars.credential" and e["status"] == "rejected"
                   and e.get("reason") == "verify_failed" for e in ops)


class TestEnforcement:
    """强制开关：开启后无密钥/无签名设备一律拒绝（已绑定也不放行）；过渡期放行。"""

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

    def test_enforcement_rejects_no_key_even_bound(self, client, auth_headers):
        _bind_without_key(client, auth_headers)
        client.put("/settings/enforcement", json={"enabled": True}, headers=auth_headers)
        res = _boot_vars(client, mac=MAC_B)
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text
        ops = client.get("/operations", params={"mac": MAC_B}, headers=auth_headers).json()["entries"]
        assert any(e["op"] == "boot_vars.credential" and e["status"] == "rejected"
                   and e.get("reason") == "no_key" for e in ops)

    def test_enforcement_rejects_missing_sig(self, client, auth_headers, ec_keypair):
        hostname = _claim_and_bind(client, auth_headers, ec_keypair)
        client.put("/settings/enforcement", json={"enabled": True}, headers=auth_headers)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname)  # 无 nonce/sig
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text
        ops = client.get("/operations", params={"mac": MAC_A}, headers=auth_headers).json()["entries"]
        assert any(e["op"] == "boot_vars.credential" and e["status"] == "rejected"
                   and e.get("reason") == "missing_sig" for e in ops)

    def test_enforcement_allows_missing_hostname(self, client, auth_headers, ec_keypair):
        """签名只覆盖 nonce||mac：DHCP 未提供 hostname 时不再阻断引导。"""
        _claim_and_bind(client, auth_headers, ec_keypair)
        client.put("/settings/enforcement", json={"enabled": True}, headers=auth_headers)
        nonce = _challenge(client)
        sig = ec_keypair["sign"](MAC_A, nonce)
        res = _boot_vars(client, mac=MAC_A, nonce=nonce, sig=sig)  # 无 hostname
        assert res.status_code == 200
        assert "set menu-default" in res.text

    def test_enforcement_allows_valid_signature(self, client, auth_headers, ec_keypair):
        hostname = _claim_and_bind(client, auth_headers, ec_keypair)
        client.put("/settings/enforcement", json={"enabled": True}, headers=auth_headers)
        nonce = _challenge(client)
        sig = ec_keypair["sign"](MAC_A, nonce)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce, sig=sig)
        assert res.status_code == 200
        assert "set menu-default" in res.text

    def test_transition_allows_no_key(self, client, auth_headers):
        # 过渡期（强制关）：无密钥已绑定设备照现状放行
        _bind_without_key(client, auth_headers)
        res = _boot_vars(client, mac=MAC_B)
        assert res.status_code == 200
        assert "set menu-default" in res.text


class TestBootVarsProjection:
    """boot-vars 注入投影：未注册/池中/绑定 worker 的分支语义。"""

    def test_unregistered_device_gets_empty_script(self, client):
        res = _boot_vars(client, mac="00:99:88:77:66:55")
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text

    def test_pooled_device_gets_reboot_payload(self, client, auth_headers, ec_keypair):
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        client.get("/devices/report", params={"mac": MAC_A, "pubkey": ec_keypair["pubkey_hex"]})
        res = _boot_vars(client, mac=MAC_A)
        assert res.status_code == 200
        assert "set menu-default reboot" in res.text  # 池中未绑定：reboot 循环等待绑定

    def test_revoked_device_gets_empty_script(self, client, auth_headers):
        client.post("/devices", json={"mac": MAC_A}, headers=auth_headers)
        client.delete(f"/devices/{MAC_A}", headers=auth_headers)
        res = _boot_vars(client, mac=MAC_A)
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text

    def test_unbound_other_device_gets_empty_script(self, client, auth_headers, ec_keypair):
        # 防冒领：绑定 worker-01 的设备是 MAC_A，MAC_C 带 worker-01 hostname 请求 → 拒绝
        hostname = _claim_and_bind(client, auth_headers, ec_keypair, mac=MAC_A, worker_id="worker-01")
        client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        client.get("/devices/report", params={"mac": MAC_C, "pubkey": ec_keypair["pubkey_hex"]})
        res = _boot_vars(client, mac=MAC_C, hostname=hostname)
        assert res.status_code == 200
        assert EMPTY_SCRIPT in res.text

    def test_hostname_only_request_allowed(self, client, auth_headers):
        # 无 mac（仅 hostname）：过渡期放行菜单变量，但不注入凭据（凭据只认验签）
        client.post("/workers", json={"worker_id": "worker-09"}, headers=auth_headers)
        res = _boot_vars(client, hostname="worker-09")
        assert res.status_code == 200
        assert "set menu-default reboot" in res.text

    def test_format_validation(self, client):
        assert _boot_vars(client, format_="xml").status_code == 400

    def test_json_format(self, client, auth_headers, ec_keypair):
        hostname = _claim_and_bind(client, auth_headers, ec_keypair)
        nonce = _challenge(client)
        sig = ec_keypair["sign"](MAC_A, nonce)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce, sig=sig, format_="json")
        assert res.status_code == 200
        body = res.json()
        assert body["menu_default"] == "reboot"
        assert body["menu_timeout"] > 0


class TestBootVarsWithDisk:
    """绑定 + 建盘后的完整注入投影（mock AgentClient 走全链路）。"""

    def test_bound_worker_with_disk_projects_iscsi(self, client, auth_headers, ec_keypair, mock_agent_client):
        # 注册 agent（role.disk）+ mock client → 建盘 → 验签后投影 iqn/server/sep
        client.post("/agents", json={
            "id": "ag-01", "base_url": "http://ag-01:8000",
            "role": {"disk": True, "cd": False}, "tags": ["storage", "stgt"],
        }, headers=auth_headers)
        hostname = _claim_and_bind(client, auth_headers, ec_keypair, mac=MAC_A, worker_id="worker-01")
        res = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
        }, headers=auth_headers)
        assert res.status_code == 201, res.text
        assert res.json()["state"] == "ready"
        disk = res.json()["disks"][0]

        nonce = _challenge(client)
        sig = ec_keypair["sign"](MAC_A, nonce)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce, sig=sig)
        assert res.status_code == 200
        assert f"set disk-nqn {disk['nqn']}" in res.text  # 完整盘 NQN 原样下发（盘记录权威值）
        assert "set base-nqn" not in res.text  # 不再下发前缀，固件不重拼
        assert "set base-iqn iqn.2026-07.com.test" in res.text  # base-iqn 为前缀，worker 后缀由 iPXE 拼装
        assert "set storager-ip ag-01" in res.text  # storager_ip 缺省回退 base_url 主机名
        assert "set iscsi-sep :::1:" in res.text  # stgt 后端差异连接符
        assert "set os ubuntu" in res.text  # 盘 os 投影（默认盘 = 唯一盘）
        assert "set menu-default reboot" in res.text  # 单盘建盘不自动设 default_disk → reboot

    def test_backend_from_capabilities_not_stale_tags(self, client, auth_headers, ec_keypair, mock_agent_client):
        """后端类型以 Agent 自报的能力为准，登记信息里的过期标签不得覆盖实况。

        回归：原实现优先读 agents.yml 的 tags（命中即返回），标签与实况漂移时按过期值拼连接符。
        此处注册时 tags 带陈旧的 nvmet，Agent /capabilities 实报 stgt → 必须按 stgt 拼（发 iscsi-sep）。
        """
        client.post("/agents", json={
            "id": "ag-01", "base_url": "http://ag-01:8000",
            "role": {"disk": True, "cd": False}, "tags": ["storage", "nvmet"],
        }, headers=auth_headers)
        hostname = _claim_and_bind(client, auth_headers, ec_keypair, mac=MAC_A, worker_id="worker-01")
        res = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
        }, headers=auth_headers)
        assert res.status_code == 201, res.text

        nonce = _challenge(client)
        sig = ec_keypair["sign"](MAC_A, nonce)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce, sig=sig)
        assert res.status_code == 200
        assert "set iscsi-sep :::1:" in res.text  # mock capabilities 实报 stgt，压过陈旧的 nvmet 标签

    def test_disabled_agent_still_projects_boot_vars(self, client, auth_headers, ec_keypair, mock_agent_client):
        """enabled=排空语义的边界回归：停用只拦新增，存量盘的引导投影照常
        （storager_ip_for 无 enabled 守卫是裁定的一部分，不是遗漏）。"""
        client.post("/agents", json={
            "id": "ag-01", "base_url": "http://ag-01:8000",
            "role": {"disk": True, "cd": False}, "tags": ["storage", "stgt"],
        }, headers=auth_headers)
        hostname = _claim_and_bind(client, auth_headers, ec_keypair, mac=MAC_A, worker_id="worker-01")
        res = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
        }, headers=auth_headers)
        assert res.status_code == 201, res.text
        disk = res.json()["disks"][0]
        res = client.put("/agents/ag-01", json={
            "base_url": "http://ag-01:8000", "role": {"disk": True, "cd": False},
            "tags": ["storage"], "enabled": False,
        }, headers=auth_headers)
        assert res.status_code == 200

        nonce = _challenge(client)
        sig = ec_keypair["sign"](MAC_A, nonce)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce, sig=sig)
        assert res.status_code == 200
        assert "set storager-ip ag-01" in res.text  # 存量盘照常投影
        assert f"set disk-nqn {disk['nqn']}" in res.text

    def test_default_disk_projects_boot_vars(self, client, auth_headers, ec_keypair, mock_agent_client):
        client.post("/agents", json={
            "id": "ag-01", "base_url": "http://ag-01:8000",
            "role": {"disk": True, "cd": False}, "tags": ["storage", "stgt"],
        }, headers=auth_headers)
        hostname = _claim_and_bind(client, auth_headers, ec_keypair, mac=MAC_A, worker_id="worker-01")
        disk = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
        }, headers=auth_headers).json()["disks"][0]
        os_tag = disk["os_tag"]
        res = client.put("/workers/worker-01/default-disk", json={"disk": os_tag}, headers=auth_headers)
        assert res.status_code == 200, res.text
        assert res.json()["default_disk"] == os_tag

        nonce = _challenge(client)
        sig = ec_keypair["sign"](MAC_A, nonce)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce, sig=sig)
        # 2026-08-30 MAIN MENU 动态化：default_disk 归一到通用项 boot-os，os/disk-nqn 投影默认盘身份
        assert "set menu-default boot-os" in res.text
        assert "set os ubuntu" in res.text
        assert f"set disk-nqn {disk['nqn']}" in res.text
        assert "set os-tag" not in res.text  # NQN 已完整下发，不再暴露后缀
        assert "set os-version" not in res.text  # 无版本 → 不下发
        # default_disk > boot.menu_default 推导链：显式 menu_default 不覆盖 default_disk
        client.put("/workers/worker-01/default-disk", json={"menu_default": "windows"}, headers=auth_headers)
        nonce2 = _challenge(client)
        sig2 = ec_keypair["sign"](MAC_A, nonce2)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce2, sig=sig2)
        assert "set menu-default boot-os" in res.text
        assert "set os ubuntu" in res.text
        assert f"set disk-nqn {disk['nqn']}" in res.text

    def test_default_disk_projects_os_version(self, client, auth_headers, ec_keypair, mock_agent_client):
        """带版本的默认盘：boot-vars 投影 os-version（menu 侧 os-label 拼版本展示）。"""
        client.post("/agents", json={
            "id": "ag-01", "base_url": "http://ag-01:8000",
            "role": {"disk": True, "cd": False}, "tags": ["storage", "stgt"],
        }, headers=auth_headers)
        hostname = _claim_and_bind(client, auth_headers, ec_keypair, mac=MAC_A, worker_id="worker-01")
        disk = client.post("/workers/worker-01/luns/disk", json={
            "type": "master", "os": "ubuntu", "os_version": "24.04", "name": "ubuntu-24.04-master",
        }, headers=auth_headers).json()["disks"][0]
        client.put("/workers/worker-01/default-disk", json={"disk": disk["os_tag"]}, headers=auth_headers)

        nonce = _challenge(client)
        sig = ec_keypair["sign"](MAC_A, nonce)
        res = _boot_vars(client, mac=MAC_A, hostname=hostname, nonce=nonce, sig=sig)
        assert "set menu-default boot-os" in res.text
        assert "set os ubuntu" in res.text
        assert "set os-version 24.04" in res.text
        assert f"set disk-nqn {disk['nqn']}" in res.text


class TestDeviceBindingAuthority:
    """设备台账 bound_worker_id 是绑定权威；hostname/dnsmasq 投影只作兜底，不得反向覆盖。"""

    def test_device_binding_beats_stale_dnsmasq_projection(self, client, auth_headers, ec_keypair):
        # 设备台账（权威）绑定到 worker-01；伪造滞后的 dnsmasq 投影指向 worker-02
        _claim_and_bind(client, auth_headers, ec_keypair, mac=MAC_A, worker_id="worker-01")
        assert client.post("/workers", json={"worker_id": "worker-02"}, headers=auth_headers).status_code == 201
        settings.dnsmasq_hosts_file.write_text(f"{MAC_A},worker-02\n", encoding="utf-8")
        res = _boot_vars(client, mac=MAC_A, hostname="worker-02")
        assert res.status_code == 200
        assert "# boot vars for worker-01" in res.text
