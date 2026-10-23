"""PkiClient 引导请求测试（一键加入链路，2026-08-31）：base_url / remark 上报等 payload 契约。"""

import json

import pytest

from storager.agent.app import pki_client


class _FakeResp:
    def __init__(self, payload=None):
        self._payload = payload or {
            "certificates": {
                "client.crt": "FAKE-CLIENT",
                "serving.crt": "FAKE-SERVING",
            },
            "ca_nodes_crt": "FAKE-NODES-CA",
            "ca_control_crt": "FAKE-CONTROL-CA",
        }

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()

    def close(self):
        pass


def _client(monkeypatch, pki_dir, captured, resp_payload=None, **kwargs):
    def fake_urlopen(req, *, context=None, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        captured["auth"] = req.get_header("Authorization")
        return _FakeResp(resp_payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    defaults = dict(agent_id="t-ag-1", component="agent", pki_dir=pki_dir,
                    cp_base="https://cp", cp_ca=None, bootstrap_token=None)
    defaults.update(kwargs)
    return pki_client.PkiClient(**defaults)


@pytest.fixture()
def captured():
    return {}


def test_enroll_payload_carries_advertise_url(monkeypatch, pki_dir, captured):
    """enroll 请求体携带 base_url（KURRENT_ADVERTISE_URL → 控制面自动登记）。"""
    c = _client(monkeypatch, pki_dir, captured,
                bootstrap_token="tok.123", advertise_url="https://192.168.1.50:4840")
    c._enroll(renew=False)
    assert captured["url"] == "https://cp/api/cp/enroll"
    assert captured["auth"] == "Bearer tok.123"
    assert captured["body"]["agent_id"] == "t-ag-1"
    assert captured["body"]["component"] == "agent"
    assert captured["body"]["base_url"] == "https://192.168.1.50:4840"
    assert "csr_client" in captured["body"] and "csr_serving" in captured["body"]


def test_enroll_payload_carries_remark(monkeypatch, pki_dir, captured):
    """enroll 请求体携带 remark（声明 metadata.remark → 控制面自动登记写入台账）。"""
    c = _client(monkeypatch, pki_dir, captured, bootstrap_token="tok.123",
                remark="storage-nvmet-01")
    c._enroll(renew=False)
    assert captured["body"]["remark"] == "storage-nvmet-01"


def test_enroll_payload_remark_empty_by_default(monkeypatch, pki_dir, captured):
    """未声明 remark → 上报空串（兼容默认，控制面按空备注登记）。"""
    c = _client(monkeypatch, pki_dir, captured, bootstrap_token="tok.123")
    c._enroll(renew=False)
    assert captured["body"]["remark"] == ""


def test_renew_payload_without_bearer(monkeypatch, pki_dir, captured):
    """renew 不带 Bearer（凭据 = 现有 client cert，TLS 层 mTLS）。"""
    # renew 走 mTLS：tmp 隔离目录无真实证书，直接假掉 context（urlopen 已被接管）
    monkeypatch.setattr(pki_client.PkiClient, "_cp_context", lambda self, wcc: object())
    c = _client(monkeypatch, pki_dir, captured, bootstrap_token=None)
    c._enroll(renew=True)
    assert captured["url"] == "https://cp/api/cp/enroll/renew"
    assert captured["auth"] is None


def test_no_token_and_no_cert_raises(monkeypatch, pki_dir, captured):
    """无 bootstrap token 且无 client cert → 拒绝引导（阻断启动）。"""
    c = _client(monkeypatch, pki_dir, captured, bootstrap_token=None)
    with pytest.raises(RuntimeError, match="cannot enroll"):
        c._enroll(renew=False)


def test_enroll_writes_trust_anchors(monkeypatch, pki_dir, captured):
    """三分根：enroll 响应落盘 control（入站）与 nodes（同伴校验）两个锚。"""
    c = _client(monkeypatch, pki_dir, captured, bootstrap_token="tok.123")
    c._enroll(renew=False)
    assert (pki_dir / "ca-control.crt").read_text(encoding="utf-8") == "FAKE-CONTROL-CA"
    assert (pki_dir / "ca-nodes.crt").read_text(encoding="utf-8") == "FAKE-NODES-CA"


def test_derived_token_written_when_present(monkeypatch, pki_dir, captured, tmp_path):
    """agent enroll 响应含 nvmet_token（backend=nvmet 派生）→ 落盘共享凭据目录。"""
    derived = tmp_path / "nvmet-host.token"
    c = _client(monkeypatch, pki_dir, captured,
                resp_payload={
                    "certificates": {"client.crt": "C", "serving.crt": "S"},
                    "ca_nodes_crt": "N",
                    "ca_control_crt": "C2",
                    "nvmet_token": "abc123.def4567890123456",
                },
                bootstrap_token="tok.123", derived_token_file=derived)
    c._enroll(renew=False)
    assert derived.read_text(encoding="utf-8").strip() == "abc123.def4567890123456"
    assert (derived.stat().st_mode & 0o777) == 0o600


def test_derived_token_skipped_when_absent(monkeypatch, pki_dir, captured, tmp_path):
    """响应无 nvmet_token（stgt/lio 后端或旧控制面）→ 不写文件、不报错。"""
    derived = tmp_path / "nvmet-host.token"
    c = _client(monkeypatch, pki_dir, captured,
                bootstrap_token="tok.123", derived_token_file=derived)
    c._enroll(renew=False)
    assert not derived.exists()
