"""控制面单测夹具：TestClient + 每用例状态隔离 + TPM 身份与高层场景。"""

import hashlib
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from control_plane.app import ratelimit, trust as trust_mod
from control_plane.app.config import settings
from control_plane.app.main import app

TEST_TOKEN = "test-control-token"

# 共享 fake Agent 上宣告存在的母盘：放置策略要求候选节点本地确有该母盘（克隆在节点本地完成），
# 所以用例里用来克隆的母盘名必须在这里列出。
FAKE_MASTERS = ["ubuntu-24.04-master", "m", "unlabeled-master"]

# 常用测试 MAC（规范冒号格式）
MAC_A = "00:11:22:33:44:55"
MAC_B = "00:11:22:33:44:66"
MAC_C = "00:11:22:33:44:77"
MAC_D = "00:11:22:33:44:88"


@pytest.fixture()
def client():
    """TestClient：每个用例前清空全部状态文件 + 挑战 nonce 存储，保证用例互不污染。"""
    for f in (
        settings.agents_file,
        settings.workers_file,
        settings.devices_file,
        settings.operations_file,
        settings.settings_file,
        settings.dnsmasq_hosts_file,
        settings.credentials_file,
        settings.credential_push_file,
        settings.masters_file,
        settings.sessions_file,
    ):
        f.unlink(missing_ok=True)
    trust_mod._nonce_store.clear()
    ratelimit.reset()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_headers():
    """控制面 API 鉴权头：测试环境为开发模式（KURRENT_CP_INSECURE=1，见 tests/conftest.py），
    该头不被校验；保留以贴近真实请求形态。认证器链本身的断言见 test_auth.py。"""
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def _tpm2b(payload: bytes) -> bytes:
    return len(payload).to_bytes(2, "big") + payload


def build_quote(extra_data: bytes, digest: bytes) -> bytes:
    """按 TPMS_ATTEST 构造 quote（PCR 0/2/4/7，SHA-256）。"""
    bitmap = bytes([0x95, 0x00, 0x00])
    body = (0xFF544347).to_bytes(4, "big") + (0x8018).to_bytes(2, "big")
    body += _tpm2b(b"\x00\x0b" + b"\xaa" * 32)
    body += _tpm2b(extra_data)
    body += bytes(17)
    body += (0).to_bytes(8, "big")
    body += (1).to_bytes(4, "big")
    body += (0x000B).to_bytes(2, "big") + bytes([len(bitmap)]) + bitmap
    body += _tpm2b(digest)
    return body


@pytest.fixture()
def tpm_identity():
    """TPM AK 测试身份：SPKI 公钥 + TPM2B_NAME + 按 TPM 2.0 结构构造的 quote 签名。

    quote 用真实 ECDSA 密钥签名，结构按 TPMS_ATTEST 逐字节构造；真实 TPM 的端到端
    验证在 swtpm 环境单独进行。
    """
    priv = ec.generate_private_key(ec.SECP256R1())
    ak_public = priv.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo).hex()
    ak_name = (b"\x00\x0b" + hashlib.sha256(bytes.fromhex(ak_public)).digest()).hex()
    default_digest = "ab" * 32

    def quote(nonce: str, digest: str = default_digest) -> dict:
        blob = build_quote(nonce.encode(), bytes.fromhex(digest))
        signature = priv.sign(blob, ec.ECDSA(hashes.SHA256()))
        return {"quote": blob.hex(), "quote_sig": signature.hex(), "pcr_digest": digest}

    return {
        "ak_public": ak_public,
        "ak_name": ak_name,
        "ak_type": "ecc_p256",
        "digest": default_digest,
        "quote": quote,
        "report_params": {
            "ak_public": ak_public,
            "ak_name": ak_name,
            "ak_type": "ecc_p256",
            "sb": "1",
        },
    }


@pytest.fixture()
def signed_boot_vars(client, tpm_identity):
    """按真实引导链取 nonce → TPM quote 签名 → 请求 /boot-vars（凭据注入必须验签）。"""
    def _get(mac: str, hostname: str | None = None, format_: str = "ipxe",
             digest: str | None = None):
        res = client.get("/devices/challenge", params={"mac": mac})
        assert res.status_code == 200, res.text
        nonce = next(l[len("set nonce "):] for l in res.text.splitlines()
                     if l.startswith("set nonce "))
        signed = tpm_identity["quote"](nonce, digest or tpm_identity["digest"])
        params = {"mac": mac, "nonce": nonce, "quote": signed["quote"],
                  "quote_sig": signed["quote_sig"], "format": format_}
        if hostname:
            params["hostname"] = hostname
        return client.get("/boot-vars", params=params)
    return _get


@pytest.fixture()
def register_claimed_device(client, auth_headers, tpm_identity):
    """高层场景：开注册窗口 → report 上报（带 TPM AK）→ 设备认领入池。"""
    def _register(mac: str) -> dict:
        res = client.post("/settings/registration-window", json={"ttl_minutes": 30}, headers=auth_headers)
        assert res.status_code == 201
        res = client.get("/devices/report",
                         params={"mac": mac, **tpm_identity["report_params"]})
        assert res.status_code == 200
        dev = client.get(f"/devices/{mac}", headers=auth_headers)
        assert dev.status_code == 200
        assert dev.json()["state"] == "pooled"
        assert dev.json()["identity"]["ak_public"] == tpm_identity["ak_public"]
        return dev.json()
    return _register


def fake_push_result(worker_id, secret, sub_nqns, host_nqns, tls_psk=None,
                     generation=0) -> dict:
    """测试用 Agent 推送回执：按 sub × host 回报 TLS 应用数量。"""
    expected = len(sub_nqns) * len(host_nqns) if (tls_psk or secret is None) else 0
    return {
        "worker_id": worker_id,
        "secret": bool(secret),
        "tls_applied": expected if secret else 0,
        "tls_expected": expected,
        "tls_revoked": expected if secret is None else 0,
    }


@pytest.fixture()
def mock_agent_client(monkeypatch):
    """外置 Agent 客户端：patch AgentRegistry.client → 内存 fake client。

    用例内先 POST /agents 注册（role.disk=true）即可走完整建盘/删盘/放置链路
    （放置策略内部 healthz/capabilities/list_masters/list_luns 同样命中 fake）。
    """
    from control_plane.app.stores import agents

    fake = SimpleNamespace(
        healthz=lambda: {"status": "ok"},
        capabilities=lambda: {"base_nqn": "nqn.2026-07.com.test", "cd": False, "backend": "stgt"},
        create_disk=lambda iqn, filename, **kw: {"iqn": iqn, "backing": f"/srv/iscsi/{filename}"},
        create_cd=lambda iso, iqn: {"iqn": iqn, "backing": iso},
        delete_lun=lambda iqn, delete_file=False: {"deleted": iqn},
        list_luns=lambda: [],
        list_masters=lambda: {"masters": [{"name": name} for name in FAKE_MASTERS]},
        scan=lambda: {"created": [], "skipped": []},
        set_credential=fake_push_result,
        credential_status=lambda worker_id, sub_nqns, host_nqns: {
            "worker_id": worker_id,
            "keys": [{"sub_nqn": s, "host_nqn": h, "present": True}
                     for s in sub_nqns for h in host_nqns],
            "present_all": bool(sub_nqns and host_nqns),
        },
    )
    monkeypatch.setattr(agents, "client", lambda agent: fake)
    return fake
