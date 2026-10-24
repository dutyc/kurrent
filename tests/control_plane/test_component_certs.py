"""nodes 域组件证书：签发登记、nodes CRL 吊销、API/CLI 端点与 renew 兜底。"""

import re

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from control_plane.app import pki
from control_plane.app.config import settings
from control_plane.app.routers import certs as certs_mod


@pytest.fixture(autouse=True)
def _stub_reload(monkeypatch):
    monkeypatch.setattr(certs_mod, "reload_nginx", lambda: {"status": "ok"})


@pytest.fixture()
def tmp_pki(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "pki_dir", tmp_path)
    return tmp_path


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _csr(cn: str, sans: list[str] | None = None) -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    builder = x509.CertificateSigningRequestBuilder().subject_name(name)
    if sans:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), critical=False)
    return builder.sign(_key(), hashes.SHA256()).public_bytes(serialization.Encoding.PEM)


def _issue(pki_dir, agent_id: str = "node-a") -> dict[str, bytes]:
    ca_cert, ca_key = pki.ensure_ca_set(pki_dir)["nodes"]
    return pki.sign_component_certs(
        ca_cert, ca_key, agent_id, "agent",
        _csr(f"agent-{agent_id}"), _csr(f"agent-{agent_id}", ["localhost"]),
        ["localhost"], pki_dir=pki_dir,
    )


def _serials(path) -> set[int]:
    text = path.read_text(encoding="utf-8")
    blocks = re.findall(r"-----BEGIN X509 CRL-----.*?-----END X509 CRL-----", text, re.S)
    return {entry.serial_number
            for crl in (x509.load_pem_x509_crl(b.encode()) for b in blocks)
            for entry in crl}


def _serial(pem: bytes) -> str:
    return format(x509.load_pem_x509_certificate(pem).serial_number, "x")


def test_issue_registers_client_and_serving(tmp_pki):
    """签发即登记：client + serving 两条，agent_id/kind/cn 正确。"""
    certs = _issue(tmp_pki)
    items = pki.list_component_certs(tmp_pki)
    assert {(i["agent_id"], i["kind"], i["cn"]) for i in items} == {
        ("node-a", "client", "agent-node-a"),
        ("node-a", "serving", "agent-node-a"),
    }
    assert {i["serial"] for i in items} == {_serial(certs["client.crt"]),
                                           _serial(certs["serving.crt"])}
    assert all(i["revoked_at"] == "" and not i["expired"] for i in items)


def test_revoke_writes_nodes_crl(tmp_pki):
    """吊销 → nodes CRL 含两个序列号，且 clients.crl（users+nodes 拼接）里同样可见。"""
    certs = _issue(tmp_pki)
    revoked = pki.revoke_component_cert(tmp_pki, "node-a", "key_compromise")
    assert {e["kind"] for e in revoked} == {"client", "serving"}
    serials = {int(e["serial"], 16) for e in revoked}
    assert serials == {int(_serial(certs["client.crt"]), 16),
                       int(_serial(certs["serving.crt"]), 16)}

    nginx_dir = tmp_pki / "nginx"
    assert serials <= _serials(nginx_dir / "clients.crl")
    text = (nginx_dir / "clients.crl").read_text(encoding="utf-8")
    crls = [x509.load_pem_x509_crl(b.encode()) for b in
            re.findall(r"-----BEGIN X509 CRL-----.*?-----END X509 CRL-----", text, re.S)]
    nodes = [c for c in crls
             if c.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "kurrent-ca-nodes"]
    assert len(nodes) == 1 and {e.serial_number for e in nodes[0]} == serials
    assert pki.list_component_certs(tmp_pki)[0]["reason"] == "key_compromise"


def test_revoke_single_serial_keeps_other(tmp_pki):
    certs = _issue(tmp_pki)
    target = _serial(certs["client.crt"])
    revoked = pki.revoke_component_cert(tmp_pki, "node-a", "superseded", serial=target)
    assert [e["serial"] for e in revoked] == [target]
    listed = {i["serial"]: i for i in pki.list_component_certs(tmp_pki)}
    assert listed[target]["revoked_at"]
    assert listed[_serial(certs["serving.crt"])]["revoked_at"] == ""


def test_revoke_rejects_bad_input(tmp_pki):
    with pytest.raises(ValueError, match="unknown revocation reason"):
        pki.revoke_component_cert(tmp_pki, "node-a", "nope")
    with pytest.raises(ValueError, match="invalid serial"):
        pki.revoke_component_cert(tmp_pki, "node-a", serial="not-hex")


def test_unregistered_legacy_serial_is_not_revoked(tmp_pki):
    """未登记的证书（P4 之前签发、控制面未留存）不会出现在台账里，也不会误判已吊销。"""
    certs = _issue(tmp_pki)
    serial = _serial(certs["client.crt"])
    (tmp_pki / "components" / "issued.yml").unlink()
    assert pki.list_component_certs(tmp_pki) == []
    assert pki.component_cert_revoked(tmp_pki, serial) is False


def test_component_cert_revoked_lookup(tmp_pki):
    certs = _issue(tmp_pki)
    serial = _serial(certs["client.crt"])
    assert pki.component_cert_revoked(tmp_pki, serial) is False
    pki.revoke_component_cert(tmp_pki, "node-a", serial=serial)
    assert pki.component_cert_revoked(tmp_pki, serial) is True
    assert pki.component_cert_revoked(tmp_pki, "not-hex") is False


def test_list_and_revoke_endpoints(client, tmp_pki, auth_headers):
    """API：列表 → 吊销（串行号）→ 列表状态；bootstrap token 一并删除。"""
    certs = _issue(tmp_pki)
    token = pki.issue_bootstrap_token(tmp_pki, agent_id="node-a")
    token_id = token.split(".", 1)[0]

    listed = client.get("/pki/components", headers=auth_headers).json()
    assert len(listed) == 2

    res = client.delete("/pki/components/node-a", headers=auth_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["revoked"] == "node-a"
    assert sorted(body["kinds"]) == ["client", "serving"]
    assert len(body["serials"]) == 2
    assert pki.get_bootstrap_token(tmp_pki, token_id) is None

    listed = client.get("/pki/components", headers=auth_headers).json()
    assert all(i["revoked_at"] for i in listed)
    assert {i["serial"] for i in listed} == {_serial(certs["client.crt"]),
                                             _serial(certs["serving.crt"])}


def test_revoke_endpoint_serial_and_errors(client, tmp_pki, auth_headers):
    certs = _issue(tmp_pki)
    serial = _serial(certs["client.crt"])
    res = client.delete(f"/pki/components/node-a?serial={serial}", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["serials"] == [serial]

    assert client.delete("/pki/components/ghost", headers=auth_headers).status_code == 404
    assert client.delete("/pki/components/node-a?serial=not-hex",
                         headers=auth_headers).status_code == 400


def test_renew_rejects_revoked_serial(client, tmp_pki):
    """renew 兜底：CRL 尚未 reload 时，已吊销序列号也不换新证。"""
    certs = _issue(tmp_pki)
    serial = _serial(certs["client.crt"])
    pki.revoke_component_cert(tmp_pki, "node-a", serial=serial)

    res = client.post("/enroll/renew", json={
        "agent_id": "node-a", "component": "agent",
        "csr_client": "FAKE", "csr_serving": "FAKE",
    }, headers={
        "X-Client-Cert-Dn": "/CN=agent-node-a",
        "X-Client-Cert-Serial": serial,
        "X-Client-Class": "component",
    })
    assert res.status_code == 403
    assert "revoked" in res.json()["detail"]


def _control_cert(tmp_pki, component: str = "control-plane") -> bytes:
    ca_set = pki.ensure_ca_set(tmp_pki)
    cert_path, _ = pki.ensure_control_plane_client_cert(
        tmp_pki, *ca_set["control"], component)
    return cert_path.read_bytes()


def test_control_cert_registered_and_component_crls(tmp_pki):
    """控制面自身证书登记进 control 台账；组件分发同时给出 nodes/control CRL。"""
    cert = _control_cert(tmp_pki)
    items = pki.list_control_certs(tmp_pki)
    assert [(i["cn"], i["serial"]) for i in items] == [("control-plane", _serial(cert))]

    bundle = pki.component_crls(tmp_pki)
    nginx_dir = tmp_pki / "nginx"
    assert (nginx_dir / "nodes.crl").is_file()
    assert (nginx_dir / "control.crl").is_file()
    assert bundle["nodes_crl"].startswith("-----BEGIN X509 CRL-----")
    assert bundle["control_crl"].startswith("-----BEGIN X509 CRL-----")
    assert bundle["next_update"]


def test_revoke_control_cert_writes_only_control_crl(tmp_pki):
    """吊销控制面证书：进 control CRL，不进 nodes CRL（两个域互不污染）。"""
    cert = _control_cert(tmp_pki)
    serial = int(_serial(cert), 16)
    revoked = pki.revoke_control_cert(tmp_pki, "control-plane", "key_compromise")
    assert [e["serial"] for e in revoked] == [_serial(cert)]

    control = x509.load_pem_x509_crl((tmp_pki / "nginx" / "control.crl").read_bytes())
    nodes = x509.load_pem_x509_crl((tmp_pki / "nginx" / "nodes.crl").read_bytes())
    assert serial in {e.serial_number for e in control}
    assert serial not in {e.serial_number for e in nodes}
    assert pki.list_control_certs(tmp_pki)[0]["reason"] == "key_compromise"


def test_revoke_control_cert_rejects_bad_input(tmp_pki):
    with pytest.raises(ValueError, match="unknown revocation reason"):
        pki.revoke_control_cert(tmp_pki, "control-plane", "nope")
    with pytest.raises(ValueError, match="invalid serial"):
        pki.revoke_control_cert(tmp_pki, "control-plane", serial="zz")


def test_control_cert_endpoints(client, tmp_pki):
    """API：列出与吊销 control 证书；吊销后 CRL 可见。"""
    cert = _control_cert(tmp_pki)
    serial = _serial(cert)
    listed = client.get("/pki/control", headers={"Authorization": "Bearer x"}).json()
    assert [(i["cn"], i["serial"]) for i in listed] == [("control-plane", serial)]

    res = client.delete("/pki/control/control-plane?reason=key_compromise",
                        headers={"Authorization": "Bearer x"})
    assert res.status_code == 200, res.text
    assert res.json()["serials"] == [serial]
    crl = x509.load_pem_x509_crl((tmp_pki / "nginx" / "control.crl").read_bytes())
    assert int(serial, 16) in {e.serial_number for e in crl}

    again = client.delete("/pki/control/control-plane",
                          headers={"Authorization": "Bearer x"})
    assert again.status_code == 404


def test_renew_accepts_unrevoked_serial(client, tmp_pki, monkeypatch):
    """未吊销的序列号走到签发（签发链路 mock，只验证闸门放行）。"""
    certs = _issue(tmp_pki)
    serial = _serial(certs["client.crt"])
    monkeypatch.setattr(pki, "sign_component_certs",
                        lambda *a, **k: {"client.crt": b"NEW", "serving.crt": b"NEW2"})
    monkeypatch.setattr(pki, "ca_cert_pem", lambda d, kind="nodes": b"FAKE-CA")
    from control_plane.app.stores import store as _store

    with _store.locked():
        from control_plane.app.stores import agents
        agents.add("node-a", "", role_disk=True, enabled=True, tags=["auto"])
    res = client.post("/enroll/renew", json={
        "agent_id": "node-a", "component": "agent",
        "csr_client": "FAKE", "csr_serving": "FAKE",
    }, headers={
        "X-Client-Cert-Dn": "/CN=agent-node-a",
        "X-Client-Cert-Serial": serial,
        "X-Client-Class": "component",
    })
    assert res.status_code in (200, 201), res.text
