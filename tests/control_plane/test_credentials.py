"""NVMe-oF 认证凭据端点测试（C1）：DHHC-1 自检矩阵、CRUD、幂等、鉴权、审计不泄露密钥本体。"""

import base64
import json
import zlib

import pytest

from control_plane.app.config import settings
from control_plane.app.routers.workers import (
    _derive_tls_psk,
    _generate_dhhc1,
    _secret_hash,
    _validate_dhhc1,
)

MAC_A = "00:11:22:33:44:55"


def make_secret(key: bytes = b"0123456789abcdef0123456789abcdef") -> str:
    """生成合法 DHHC-1 密钥（蓝图 2.1 契约：DHHC-1:01:<base64(key+CRC32 小端终值)>）。"""
    return "DHHC-1:01:" + base64.b64encode(key + zlib.crc32(key).to_bytes(4, "little")).decode()


def _ok_push(w, s, subs, hosts, tls_psk=None):
    expected = len(subs) * len(hosts) if (tls_psk or s is None) else 0
    return {"worker_id": w, "tls_applied": expected if s else 0,
            "tls_expected": expected, "tls_revoked": expected if s is None else 0}


def _create_worker(client, auth_headers, worker_id: str = "worker-01") -> None:
    res = client.post("/workers", json={"worker_id": worker_id}, headers=auth_headers)
    assert res.status_code == 201


# ============================ DHHC-1 自检矩阵 ============================


def test_validate_dhhc1_accepts_valid():
    _validate_dhhc1(make_secret())
    # 64 字节密钥（SHA-512 形态，68 总长）同样合法
    _validate_dhhc1(make_secret(b"k" * 64))


@pytest.mark.parametrize(
    "bad",
    [
        "DHHC-2:01:AAAA",            # 前缀错误
        "DHHC-1:01",                 # 无 base64 段
        "DHHC-1:1:" + base64.b64encode(b"x" * 36).decode(),   # 类型一位数
        "DHHC-1:01:" + base64.b64encode(b"x" * 16).decode(),  # 长度错（16 字节）
        "DHHC-1:01:" + base64.b64encode(b"x" * 40).decode(),  # 长度错（40 字节）
        "DHHC-1:01:@@@",             # base64 非法字符
        "DHHC-1:01:" + base64.b64encode(b"x" * 36).decode(),  # CRC 非终值
        "not-a-secret",              # 无前缀
    ],
)
def test_validate_dhhc1_rejects_invalid(bad):
    with pytest.raises(ValueError):
        _validate_dhhc1(bad)


def test_secret_hash_prefix_audit_only():
    secret = make_secret()
    h = _secret_hash(secret)
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


# ============================ API 端点 ============================


def test_set_get_delete_roundtrip(client, auth_headers):
    _create_worker(client, auth_headers)
    secret = make_secret()

    res = client.put("/workers/worker-01/credential", json={"secret": secret}, headers=auth_headers)
    assert res.status_code == 200
    meta = res.json()
    assert meta["exists"] is True
    assert meta["secret_hash"].startswith("sha256:")
    assert "secret" not in meta

    # GET 不返回明文，只给哈希前缀
    res = client.get("/workers/worker-01/credential", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["secret_hash"] == meta["secret_hash"]
    assert secret not in res.text

    # 落盘文件含明文（注入必需）与哈希
    data = settings.credentials_file.read_text(encoding="utf-8")
    assert secret in data
    assert "sha256:" in data

    res = client.delete("/workers/worker-01/credential", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["deleted"] is True

    res = client.get("/workers/worker-01/credential", headers=auth_headers)
    assert res.status_code == 200
    body = res.json()
    assert body["worker_id"] == "worker-01" and body["exists"] is False
    # 无盘 worker：无推送目标（push 投影 result=none）
    assert body["push"] == {"result": "none", "updated_at": None, "agents": []}

    # 删除后再删 → 404
    res = client.delete("/workers/worker-01/credential", headers=auth_headers)
    assert res.status_code == 404


def test_set_credential_worker_not_found(client, auth_headers):
    res = client.put("/workers/nope/credential", json={"secret": make_secret()}, headers=auth_headers)
    assert res.status_code == 404


def test_set_credential_invalid_rejected(client, auth_headers):
    _create_worker(client, auth_headers)
    res = client.put("/workers/worker-01/credential", json={"secret": "DHHC-1:01:AAAA"}, headers=auth_headers)
    assert res.status_code == 422
    # 非法格式不落盘
    if settings.credentials_file.exists():
        assert "worker-01" not in settings.credentials_file.read_text(encoding="utf-8")


def test_set_credential_idempotent(client, auth_headers):
    _create_worker(client, auth_headers)
    secret = make_secret()
    first = client.put("/workers/worker-01/credential", json={"secret": secret}, headers=auth_headers).json()
    second = client.put("/workers/worker-01/credential", json={"secret": secret}, headers=auth_headers).json()
    assert second["updated_at"] == first["updated_at"]
    assert second["created_at"] == first["created_at"]

    # 换新密钥 → updated_at 变化，created_at 保留
    new_secret = make_secret(b"y" * 32)
    third = client.put("/workers/worker-01/credential", json={"secret": new_secret}, headers=auth_headers).json()
    assert third["updated_at"] != first["updated_at"]
    assert third["created_at"] == first["created_at"]


def test_credential_requires_auth(client, auth_headers, monkeypatch):
    """无凭据 401（关闭开发模式；认证器链行为见 test_auth.py）。"""
    _create_worker(client, auth_headers)
    monkeypatch.setenv("KURRENT_CP_INSECURE", "")
    res = client.put("/workers/worker-01/credential", json={"secret": make_secret()})
    assert res.status_code == 401


def test_credential_audit_no_secret(client, auth_headers):
    _create_worker(client, auth_headers)
    secret = make_secret()
    client.put("/workers/worker-01/credential", json={"secret": secret}, headers=auth_headers)
    client.get("/workers/worker-01/credential", headers=auth_headers)
    client.delete("/workers/worker-01/credential", headers=auth_headers)

    ops = settings.operations_file.read_text(encoding="utf-8")
    assert "credential.set" in ops
    assert "credential.get" in ops
    assert "credential.revoke" in ops
    assert secret not in ops  # 审计不记密钥本体


# ============================ C2：/boot-vars 注入（按 Worker 跟盘） ============================


def _bound_worker(client, auth_headers, register_claimed_device, worker_id="worker-01", mac=MAC_A):
    """设备认领入池 + 绑定 worker（一对一）。"""
    register_claimed_device(mac)
    res = client.post("/workers", json={"worker_id": worker_id, "mac": mac}, headers=auth_headers)
    assert res.status_code == 201
    return worker_id


def _bind_existing_worker(client, auth_headers, register_claimed_device, worker_id="worker-01", mac=MAC_A):
    """设备认领入池 + 显式绑定已存在 worker（bind 端点是凭据推送触发点，create_worker 内部绑定不推送）。"""
    register_claimed_device(mac)
    res = client.post(f"/devices/{mac}/bind", params={"worker_id": worker_id}, headers=auth_headers)
    assert res.status_code == 200
    return worker_id


def test_boot_vars_injects_nbft_secret(
    client, auth_headers, register_claimed_device, mock_agent_client, signed_boot_vars,
):
    _bound_worker(client, auth_headers, register_claimed_device)
    # 建盘（Host NQN 与盘 NQN 同域派生需盘 NQN：base = agent 上报的 capabilities.base_nqn）
    client.post("/agents", json={
        "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
        "role": {"disk": True, "cd": False}, "tags": ["storage", "stgt"],
    }, headers=auth_headers)
    res = client.post("/workers/worker-01/luns/disk", json={
        "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
    }, headers=auth_headers)
    assert res.status_code == 201, res.text
    secret = make_secret()
    client.put("/workers/worker-01/credential", json={"secret": secret}, headers=auth_headers)

    res = signed_boot_vars(MAC_A, "worker-01")
    assert res.status_code == 200
    assert f"set nbft-secret {secret}" in res.text
    # Host NQN 与盘 NQN 同域派生（_host_nqn_for，base = 盘 NQN 前缀），与 nvmet hosts/ 登记值一致
    assert "set hostnqn nqn.2026-07.com.test:host.worker-01" in res.text

    # JSON 格式同样投影 nbft_secret / hostnqn
    res = signed_boot_vars(MAC_A, "worker-01", format_="json")
    assert res.json()["nbft_secret"] == secret
    assert res.json()["hostnqn"] == "nqn.2026-07.com.test:host.worker-01"


def _setup_worker_with_disk(
    client, auth_headers, register_claimed_device, mock_agent_client,
    *, backend="nvmet", port=None, credential=True,
):
    """认领入池 + 绑定 worker + 注册 agent + 建默认盘 + 设凭据；返回 secret。

    `port` 非空时作为 `capabilities.port` 上报（模拟 Agent 侧的数据面端口就绪态）；
    `credential=False` 时吊销认证密钥（模拟未绑定/已吊销——建盘会自动签发，须显式撤销）。
    """
    _bound_worker(client, auth_headers, register_claimed_device)
    client.post("/agents", json={
        "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
        "role": {"disk": True, "cd": False}, "tags": ["storage"],
    }, headers=auth_headers)
    caps = {"base_nqn": "nqn.2026-07.com.test", "cd": False, "backend": backend}
    if port is not None:
        caps["port"] = port
    mock_agent_client.capabilities = lambda: caps
    res = client.post("/workers/worker-01/luns/disk", json={
        "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
    }, headers=auth_headers)
    assert res.status_code == 201, res.text
    secret = make_secret()
    if credential:
        client.put("/workers/worker-01/credential", json={"secret": secret}, headers=auth_headers)
    else:
        # 建盘即自动签发（_issue_credential），故「无密钥」要靠显式吊销达成
        client.delete("/workers/worker-01/credential", headers=auth_headers)
    return secret


def test_boot_vars_injects_nbft_tls_when_data_port_ready(
    client, auth_headers, register_claimed_device, mock_agent_client, signed_boot_vars,
):
    """nvmet 后端 + 数据面端口就绪 → 下发 nbft-tls-psk（密钥纯函数派生）与 nbft-tls-port。"""
    secret = _setup_worker_with_disk(
        client, auth_headers, register_claimed_device, mock_agent_client,
        backend="nvmet",
        port={"port_id": "2", "trtype": "tcp", "trsvcid": "4421", "tsas": "tls1.3", "ready": True},
    )

    res = signed_boot_vars(MAC_A, "worker-01")
    assert res.status_code == 200
    assert f"set nbft-tls-psk {_derive_tls_psk(secret)}" in res.text
    assert "set nbft-tls-port 4421" in res.text

    res = signed_boot_vars(MAC_A, "worker-01", format_="json")
    assert res.json()["nbft_tls_psk"] == _derive_tls_psk(secret)
    assert res.json()["nbft_tls_port"] == "4421"


def test_boot_vars_denies_boot_when_port_not_ready(
    client, auth_headers, register_claimed_device, mock_agent_client, signed_boot_vars,
):
    """nvmet 后端但数据面端口未就绪（ready=False）→ 拒绝下发引导变量（默认全链路加密，
    明文口已撤除，放行只会得到一条连不上的 root-path）。"""
    _setup_worker_with_disk(
        client, auth_headers, register_claimed_device, mock_agent_client,
        backend="nvmet",
        port={"port_id": "2", "trtype": "tcp", "trsvcid": "4421", "tsas": "tls1.3", "ready": False},
    )

    res = signed_boot_vars(MAC_A, "worker-01")
    assert res.status_code == 200
    assert "no per-worker boot vars found" in res.text
    assert "nbft-tls-port" not in res.text
    assert "nbft-secret" not in res.text


def test_boot_vars_denies_boot_without_credential(
    client, auth_headers, register_claimed_device, mock_agent_client, signed_boot_vars,
):
    """nvmet 后端但无认证密钥（未绑定/已吊销）→ 同样拒绝引导：无密钥即无 PSK，
    也就没有任何可用的加密路径。"""
    _setup_worker_with_disk(
        client, auth_headers, register_claimed_device, mock_agent_client,
        backend="nvmet", credential=False,
        port={"port_id": "2", "trtype": "tcp", "trsvcid": "4421", "tsas": "tls1.3", "ready": True},
    )

    res = signed_boot_vars(MAC_A, "worker-01")
    assert "no per-worker boot vars found" in res.text
    assert "nbft-secret" not in res.text


def test_boot_vars_no_tls_for_non_nvmet_backend(
    client, auth_headers, register_claimed_device, mock_agent_client, signed_boot_vars,
):
    """非 nvmet 后端（stgt/lio 无 NVMe-oF TLS）→ 不下发 TLS 变量（即使上报了端口也忽略）。"""
    _setup_worker_with_disk(
        client, auth_headers, register_claimed_device, mock_agent_client,
        backend="stgt",
        port={"port_id": "2", "trtype": "tcp", "trsvcid": "4421", "tsas": "tls1.3", "ready": True},
    )

    res = signed_boot_vars(MAC_A, "worker-01")
    assert "nbft-tls-psk" not in res.text
    assert "nbft-tls-port" not in res.text


def test_boot_vars_no_secret_without_credential(
    client, auth_headers, register_claimed_device, signed_boot_vars,
):
    _bound_worker(client, auth_headers, register_claimed_device)
    res = signed_boot_vars(MAC_A, "worker-01")
    assert "nbft-secret" not in res.text


def test_boot_vars_no_secret_unbound(
    client, auth_headers, register_claimed_device, signed_boot_vars,
):
    """设备入池未绑定：即使 worker 有密钥，带 mac 请求被冒领拒绝（空脚本，无注入）。"""
    register_claimed_device(MAC_A)
    client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
    client.put("/workers/worker-01/credential", json={"secret": make_secret()}, headers=auth_headers)

    res = signed_boot_vars(MAC_A, "worker-01")
    assert "nbft-secret" not in res.text


def test_boot_vars_credential_audit(
    client, auth_headers, register_claimed_device, signed_boot_vars,
):
    _bound_worker(client, auth_headers, register_claimed_device)
    secret = make_secret()
    client.put("/workers/worker-01/credential", json={"secret": secret}, headers=auth_headers)
    signed_boot_vars(MAC_A, "worker-01")

    ops = settings.operations_file.read_text(encoding="utf-8")
    assert "boot_vars.credential" in ops
    assert '"injected": true' in ops
    assert secret not in ops  # 审计不记密钥本体
    # I10：injected && !verified 必须为零（注入只发生在验签通过时）
    for line in ops.splitlines():
        if "boot_vars.credential" not in line:
            continue
        entry = json.loads(line)
        assert not (entry.get("injected") and not entry.get("verified")), entry


def test_boot_vars_withholds_credentials_without_signature(
    client, auth_headers, register_claimed_device, mock_agent_client,
):
    """冒领回归：有 mac 但无 nonce/sig → nvmet 凭据不下发，引导被拒绝。"""
    _setup_worker_with_disk(
        client, auth_headers, register_claimed_device, mock_agent_client,
        backend="nvmet",
        port={"port_id": "2", "trtype": "tcp", "trsvcid": "4421", "tsas": "tls1.3", "ready": True},
    )

    res = client.get("/boot-vars", params={"mac": MAC_A, "hostname": "worker-01"})
    assert res.status_code == 200
    assert "nbft-secret" not in res.text
    assert "nbft-tls-psk" not in res.text
    assert "no per-worker boot vars found" in res.text


def test_boot_vars_hostname_only_withholds_credentials(
    client, auth_headers, register_claimed_device, mock_agent_client,
):
    """报告 F-01 回归：仅 hostname 的请求不得拿到 CHAP 密钥或 TLS PSK。"""
    _setup_worker_with_disk(
        client, auth_headers, register_claimed_device, mock_agent_client,
        backend="nvmet",
        port={"port_id": "2", "trtype": "tcp", "trsvcid": "4421", "tsas": "tls1.3", "ready": True},
    )

    res = client.get("/boot-vars", params={"hostname": "worker-01", "format": "json"})
    assert res.status_code == 200
    assert "nbft_secret" not in res.text
    assert "nbft_tls_psk" not in res.text


def test_boot_vars_transitional_omits_secret_for_non_nvmet(
    client, auth_headers, register_claimed_device, mock_agent_client,
):
    """过渡期允许下发菜单变量，但未验签请求仍不得注入 CHAP 密钥。"""
    _setup_worker_with_disk(
        client, auth_headers, register_claimed_device, mock_agent_client, backend="stgt",
    )

    res = client.get("/boot-vars", params={"mac": MAC_A, "hostname": "worker-01"})
    assert res.status_code == 200
    assert "set menu-default" in res.text
    assert "nbft-secret" not in res.text


# ============================ C4：控制面推送驱动（Agent 转调宿主服务） ============================

# Host NQN = worker 维度派生（发起端身份；base = 持盘 Agent 上报的 nqnBase，
# 测试 mock capabilities.base_nqn = nqn.2026-07.com.test）
HOST_NQN = "nqn.2026-07.com.test:host.worker-01"
# 盘标识权威 = NQN（控制面 build_nqn 生成盘 NQN，后缀带 os_tag；IQN 由 NQN 派生；推送 sub_nqns 用 NQN）


def _disk_nqn(worker_record: dict) -> str:
    """从建盘响应提取盘 NQN（带 os_tag 后缀，随机生成不能硬编码）。"""
    return worker_record["disks"][0]["nqn"]


def _setup_agent_and_disk(client, auth_headers, worker_id="worker-01") -> dict:
    """注册 disk agent + 空转 worker + 建系统盘（mock_agent_client 接管 Agent 调用）。"""
    res = client.post("/agents", json={
        "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
        "role": {"disk": True, "cd": False}, "tags": ["storage", "stgt"],
    }, headers=auth_headers)
    assert res.status_code == 201, res.text
    client.post("/workers", json={"worker_id": worker_id}, headers=auth_headers)
    res = client.post(f"/workers/{worker_id}/luns/disk", json={
        "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
    }, headers=auth_headers)
    assert res.status_code == 201, res.text
    return res.json()


def test_push_on_credential_set(client, auth_headers, register_claimed_device, mock_agent_client):
    """PUT credential → 向持盘 Agent 推送 {secret, sub_nqns, host_nqns}（审计不记密钥）。"""
    worker = _setup_agent_and_disk(client, auth_headers)
    disk_nqn = _disk_nqn(worker)
    _bind_existing_worker(client, auth_headers, register_claimed_device)
    secret = make_secret()
    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append((w, s, subs, hosts)) or _ok_push(w, s, subs, hosts, tls_psk)

    res = client.put("/workers/worker-01/credential", json={"secret": secret}, headers=auth_headers)
    assert res.status_code == 200
    assert pushes == [("worker-01", secret, [disk_nqn], [HOST_NQN])]
    ops = settings.operations_file.read_text(encoding="utf-8")
    assert '"credential.push"' in ops
    assert secret not in ops


def test_push_on_credential_revoke(client, auth_headers, register_claimed_device, mock_agent_client):
    """DELETE credential → 推送 secret=None（吊销该 worker 认证）。"""
    worker = _setup_agent_and_disk(client, auth_headers)
    disk_nqn = _disk_nqn(worker)
    _bind_existing_worker(client, auth_headers, register_claimed_device)
    client.put("/workers/worker-01/credential", json={"secret": make_secret()}, headers=auth_headers)
    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append((w, s, subs, hosts)) or _ok_push(w, s, subs, hosts, tls_psk)

    res = client.delete("/workers/worker-01/credential", headers=auth_headers)
    assert res.status_code == 200
    assert pushes == [("worker-01", None, [disk_nqn], [HOST_NQN])]


def test_push_host_nqn_derives_from_worker_id(client, auth_headers, mock_agent_client):
    """Host NQN 按 worker_id 派生（nqn.2026-07.com.kurrent:host.<worker_id>），与设备 UUID 无关。"""
    _setup_agent_and_disk(client, auth_headers)
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    res = client.post("/devices", json={"mac": MAC_A, "uuid": uuid}, headers=auth_headers)
    assert res.status_code == 201
    res = client.post(f"/devices/{MAC_A}/bind", params={"worker_id": "worker-01"}, headers=auth_headers)
    assert res.status_code == 200
    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append((w, s, subs, hosts)) or _ok_push(w, s, subs, hosts, tls_psk)

    client.put("/workers/worker-01/credential", json={"secret": make_secret()}, headers=auth_headers)
    assert pushes[0][3] == [HOST_NQN]


def test_push_on_bind_unbind(client, auth_headers, register_claimed_device, mock_agent_client):
    """绑定/解绑触发推送：host_nqns 按 worker 派生（base = 持盘 Agent 的 nqnBase）。

    注：create_worker 带 mac 走内部 _bind_device（不推送，建 worker 时无凭据可言）；
    显式 bind/unbind 端点才是推送触发点。
    """
    _setup_agent_and_disk(client, auth_headers)
    register_claimed_device(MAC_A)
    secret = make_secret()
    client.put("/workers/worker-01/credential", json={"secret": secret}, headers=auth_headers)
    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append((w, s, subs, hosts)) or _ok_push(w, s, subs, hosts, tls_psk)

    res = client.post(f"/devices/{MAC_A}/bind", params={"worker_id": "worker-01"}, headers=auth_headers)
    assert res.status_code == 200
    assert pushes[-1][3] == [HOST_NQN]  # Host NQN 恒为 worker 派生（设备无 UUID 也不回退）

    res = client.delete(f"/devices/{MAC_A}/bind", headers=auth_headers)
    assert res.status_code == 200
    disk_nqn = _disk_nqn(client.get("/workers/worker-01", headers=auth_headers).json())
    assert pushes[-1] == ("worker-01", secret, [disk_nqn], [HOST_NQN])  # 解绑后 host_nqns 不变（worker 维度恒定）


def test_push_on_disk_create(client, auth_headers, register_claimed_device, mock_agent_client):
    """建盘触发推送：新子系统立即登记 hosts（worker 已有密钥时）。"""
    res = client.post("/agents", json={
        "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
        "role": {"disk": True, "cd": False}, "tags": ["storage", "stgt"],
    }, headers=auth_headers)
    assert res.status_code == 201
    _bound_worker(client, auth_headers, register_claimed_device)
    client.put("/workers/worker-01/credential", json={"secret": make_secret()}, headers=auth_headers)
    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append((w, s, subs, hosts)) or _ok_push(w, s, subs, hosts, tls_psk)

    res = client.post("/workers/worker-01/luns/disk", json={
        "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
    }, headers=auth_headers)
    assert res.status_code == 201, res.text
    disk_nqn = _disk_nqn(res.json())
    assert pushes[-1][0] == "worker-01"
    assert pushes[-1][1] is not None
    assert disk_nqn in pushes[-1][2]


def test_push_failure_nonblocking(client, auth_headers, register_claimed_device, mock_agent_client):
    """Agent 离线/推送失败：仅审计 failed，不阻断凭据设置。"""
    _setup_agent_and_disk(client, auth_headers)
    _bind_existing_worker(client, auth_headers, register_claimed_device)

    def boom(*args, **kwargs):
        raise RuntimeError("agent unreachable")
    mock_agent_client.set_credential = boom

    res = client.put("/workers/worker-01/credential", json={"secret": make_secret()}, headers=auth_headers)
    assert res.status_code == 200
    ops = [json.loads(line) for line in settings.operations_file.read_text(encoding="utf-8").splitlines()]
    assert any(e["op"] == "credential.push" and e["status"] == "failed" for e in ops)
    assert "agent unreachable" in settings.operations_file.read_text(encoding="utf-8")


# ============================ 建盘即签发（nvmet 子系统与密钥同时就位） ============================

NVMET_CAPS = {"base_nqn": "nqn.2026-07.com.test", "cd": False, "backend": "nvmet"}


def _make_nvmet(mock_agent_client) -> None:
    mock_agent_client.capabilities = lambda: dict(NVMET_CAPS)


def _credential_exists(client, auth_headers, worker_id: str = "worker-01") -> bool:
    return client.get(f"/workers/{worker_id}/credential", headers=auth_headers).json()["exists"]


def test_generate_dhhc1_satisfies_contract():
    """服务端生成器产出必过自检（生成与自检互为闭环），且两次生成不同。"""
    first, second = _generate_dhhc1(), _generate_dhhc1()
    assert first.startswith("DHHC-1:01:")
    raw = base64.b64decode(first.split(":", 2)[2], validate=True)
    assert len(raw) == 36  # 32 字节密钥 + CRC32 小端终值
    assert zlib.crc32(raw[:-4]) == int.from_bytes(raw[-4:], "little")
    _validate_dhhc1(first)  # 不抛即合法
    assert first != second


def test_disk_create_issues_credential_on_nvmet(client, auth_headers, mock_agent_client):
    """nvmet 节点建盘即签发密钥并推送：密钥随子系统就位，无需人工设置。

    推送携带明文（注入必需）；审计只记事件与哈希，密钥本体既不进审计也不经 API 回传。"""
    res = client.post("/agents", json={
        "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
        "role": {"disk": True, "cd": False}, "tags": ["storage"],
    }, headers=auth_headers)
    assert res.status_code == 201, res.text
    _create_worker(client, auth_headers)
    _make_nvmet(mock_agent_client)
    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append((w, s, subs, hosts)) or _ok_push(w, s, subs, hosts, tls_psk)

    res = client.post("/workers/worker-01/luns/disk", json={
        "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
    }, headers=auth_headers)
    assert res.status_code == 201, res.text
    disk_nqn = res.json()["disks"][0]["nqn"]

    assert len(pushes) == 1
    worker_id, secret, sub_nqns, _hosts = pushes[0]
    assert worker_id == "worker-01" and secret is not None and sub_nqns == [disk_nqn]
    _validate_dhhc1(secret)

    body = client.get("/workers/worker-01/credential", headers=auth_headers).json()
    assert body["exists"] is True and body["push"]["result"] == "ok"
    assert secret not in client.get("/workers/worker-01/credential", headers=auth_headers).text

    ops = settings.operations_file.read_text(encoding="utf-8")
    assert '"credential.issue"' in ops and secret not in ops
    assert _secret_hash(secret)[:15] == body["secret_hash"]


def test_disk_create_keeps_existing_credential_on_nvmet(client, auth_headers, mock_agent_client):
    """已有密钥的 worker 再建盘不轮换：按 Worker 跟盘，多盘共用一把（updated_at 不动）。"""
    res = client.post("/agents", json={
        "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
        "role": {"disk": True, "cd": False}, "tags": ["storage"],
    }, headers=auth_headers)
    assert res.status_code == 201, res.text
    _create_worker(client, auth_headers)
    secret = make_secret()
    client.put("/workers/worker-01/credential", json={"secret": secret}, headers=auth_headers)
    before = client.get("/workers/worker-01/credential", headers=auth_headers).json()
    _make_nvmet(mock_agent_client)
    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append((w, s, subs, hosts)) or _ok_push(w, s, subs, hosts, tls_psk)

    res = client.post("/workers/worker-01/luns/disk", json={
        "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
    }, headers=auth_headers)
    assert res.status_code == 201, res.text

    assert pushes == [("worker-01", secret, [res.json()["disks"][0]["nqn"]], [HOST_NQN])]
    after = client.get("/workers/worker-01/credential", headers=auth_headers).json()
    assert after["secret_hash"] == before["secret_hash"]
    assert after["created_at"] == before["created_at"]
    assert '"credential.issue"' not in settings.operations_file.read_text(encoding="utf-8")


def test_disk_create_no_credential_on_non_nvmet(client, auth_headers, mock_agent_client):
    """非 nvmet 后端不签发（stgt 默认）：DHHC-1 是 NVMe-oF 专属，iSCSI 链路不受影响。"""
    res = client.post("/agents", json={
        "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
        "role": {"disk": True, "cd": False}, "tags": ["storage"],
    }, headers=auth_headers)
    assert res.status_code == 201, res.text
    _create_worker(client, auth_headers)
    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append((w, s, subs, hosts)) or _ok_push(w, s, subs, hosts, tls_psk)

    res = client.post("/workers/worker-01/luns/disk", json={
        "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
    }, headers=auth_headers)
    assert res.status_code == 201, res.text

    assert pushes == [] and _credential_exists(client, auth_headers) is False
    assert '"credential.issue"' not in settings.operations_file.read_text(encoding="utf-8")


def test_batch_disk_create_issues_credentials(client, auth_headers, mock_agent_client):
    """批量建盘 N 台 = N 份密钥一次到位，各 worker 密钥互不相同。"""
    res = client.post("/agents", json={
        "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
        "role": {"disk": True, "cd": False}, "enabled": True, "tags": ["storage"],
    }, headers=auth_headers)
    assert res.status_code == 201, res.text
    for worker_id in ("w-01", "w-02"):
        _create_worker(client, auth_headers, worker_id)
    _make_nvmet(mock_agent_client)
    pushes = {}
    mock_agent_client.set_credential = (lambda w, s, subs, hosts, tls_psk=None, generation=0:
                                        pushes.__setitem__(w, s) or _ok_push(w, s, subs, hosts, tls_psk))

    res = client.post("/workers/luns/disk/batch", json={
        "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
        "workers": ["w-01", "w-02"],
    }, headers=auth_headers)
    assert res.status_code == 200, res.text
    assert [item["worker_id"] for item in res.json()["succeeded"]] == ["w-01", "w-02"]

    assert sorted(pushes) == ["w-01", "w-02"]
    assert all(secret is not None for secret in pushes.values())
    assert len(set(pushes.values())) == 2
    for worker_id in ("w-01", "w-02"):
        assert _credential_exists(client, auth_headers, worker_id) is True


# ============================ TLS-PSK（由认证密钥派生，随凭据推送下发） ============================


# 已知答案（KAT）：固定 DHHC 密钥 → 固定 TLS-PSK，钉住派生契约（域标签/算法/HMAC 源）
# 目标端 nvmet-host keyring 与发起端 initramfs 必须得到同一串，改动派生即测试红
TLS_PSK_KAT = "b99690bb15d5cecfd8388bede984fa660398e879388215d8019d21c98800d4ab"


def test_derive_tls_psk_known_answer():
    """KAT：make_secret() 默认密钥 → 固定 PSK（契约锁定，非自引用）。"""
    assert _derive_tls_psk(make_secret()) == TLS_PSK_KAT


def test_derive_tls_psk_deterministic_and_distinct():
    """_derive_tls_psk：同密钥同值（纯函数，推送台账指纹无需扩展）、异密钥异值、64 位十六进制。"""
    s1, s2 = make_secret(), make_secret(b"fedcba9876543210fedcba9876543210")
    p1, p2 = _derive_tls_psk(s1), _derive_tls_psk(s2)
    assert p1 == _derive_tls_psk(s1)
    assert p1 != p2
    assert len(p1) == 64 and all(c in "0123456789abcdef" for c in p1)
    assert p1 != s1


def test_push_carries_derived_tls_psk(client, auth_headers, register_claimed_device,
                                      mock_agent_client):
    """凭据推送携带派生 TLS-PSK；吊销推送 tls_psk=None（Authorization 与加密资格同生命周期）。"""
    _setup_agent_and_disk(client, auth_headers)
    _bind_existing_worker(client, auth_headers, register_claimed_device)
    secret = make_secret()
    seen: list = []
    mock_agent_client.set_credential = (
        lambda w, s, subs, hosts, tls_psk=None, generation=0: seen.append(tls_psk) or _ok_push(w, s, subs, hosts, tls_psk))

    assert secret == make_secret()  # 与 KAT 同输入
    assert client.put("/workers/worker-01/credential", json={"secret": secret},
                      headers=auth_headers).status_code == 200
    assert seen == [TLS_PSK_KAT]
    assert client.delete("/workers/worker-01/credential", headers=auth_headers).status_code == 200
    assert seen[-1] is None
