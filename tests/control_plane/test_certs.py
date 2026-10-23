"""users 域证书吊销：登记 / CRL / nginx reload / 会话联动 / 自我保护。"""

import datetime as dt
import json
import re
from types import SimpleNamespace

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import HTTPException

from control_plane.app import maintenance, pki, sessions
from control_plane.app.auth import Identity
from control_plane.app.config import settings
from control_plane.app.routers import certs as certs_mod


@pytest.fixture(autouse=True)
def _stub_reload(monkeypatch):
    """默认不触达 docker；需要失败路径的用例自行覆盖。"""
    monkeypatch.setattr(certs_mod, "reload_nginx", lambda: {"status": "ok"})


def _register_user(cn: str, groups: list[str] | None = None) -> None:
    """把 CN 写进 users.yml（签发前置条件：证书只承载 CN，组来自该文件）。"""
    try:
        doc = yaml.safe_load(settings.users_file.read_text(encoding="utf-8")) or {}
    except OSError:
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    doc.setdefault("users", {})[cn] = {"groups": groups or ["viewer"]}
    settings.users_file.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")


def _csr(cn: str, *, orgs: list[str] | None = None) -> tuple[rsa.RSAPrivateKey, str]:
    """本地生成密钥与 CSR（私钥不上送）：返回 (key, csr PEM)。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = [x509.NameAttribute(NameOID.COMMON_NAME, cn)]
    name += [x509.NameAttribute(NameOID.ORGANIZATION_NAME, org) for org in orgs or []]
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name(name)).sign(
        key, hashes.SHA256())
    return key, csr.public_bytes(serialization.Encoding.PEM).decode()


def _issue_via_api(client, cn: str, *, days: int = 30, orgs: list[str] | None = None):
    _register_user(cn)
    key, csr = _csr(cn, orgs=orgs)
    res = client.post(f"/pki/users/{cn}", json={"csr": csr, "days": days})
    return key, res


def _issue_user(cn: str):
    ca_cert, ca_key = pki.ensure_ca(settings.pki_dir, "users")
    pki.ensure_admin_credentials(
        settings.pki_dir, ca_cert, ca_key,
        settings.admin_conf.parent / f"{cn}.conf", cn=cn, days=1,
    )
    return settings.pki_dir / "users" / cn / "client.crt"


def _crls(path) -> list[x509.CertificateRevocationList]:
    text = path.read_text(encoding="utf-8")
    blocks = re.findall(
        r"-----BEGIN X509 CRL-----.*?-----END X509 CRL-----", text, re.S)
    return [x509.load_pem_x509_crl(block.encode()) for block in blocks]


def _serials(path) -> set[int]:
    return {entry.serial_number for crl in _crls(path) for entry in crl}


def test_startup_writes_crl_bundle():
    """启动即写 CRL（即便无吊销）：users + nodes 两张，nginx ssl_crl 才有文件可读。"""
    path = settings.pki_dir / "nginx" / "clients.crl"
    assert path.exists()
    crls = _crls(path)
    assert len(crls) == 2
    assert {crl.issuer.rfc4514_string() for crl in crls} == {
        "CN=kurrent-ca-users", "CN=kurrent-ca-nodes",
    }


def test_periodic_refresh_rewrites_crl_and_reloads(monkeypatch):
    """长跑边界：CRL nextUpdate 只有 7 天，后台必须定期重签 + reload。"""
    calls = []

    def _reload():
        calls.append("reload")
        return {"status": "ok"}

    monkeypatch.setattr(maintenance, "reload_nginx", _reload)
    out = maintenance.refresh_crl_once()
    assert out["path"].endswith("clients.crl")
    assert calls == ["reload"]
    users_crl = _crls(settings.pki_dir / "nginx" / "users.crl")[0]
    assert users_crl.next_update_utc > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=6)


def test_periodic_refresh_failure_alarms(monkeypatch):
    def _fail():
        raise RuntimeError("nginx unreachable")

    monkeypatch.setattr(maintenance, "reload_nginx", _fail)
    with pytest.raises(RuntimeError):
        maintenance.refresh_crl_once()
    events = [json.loads(line) for line in
              settings.audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(e.get("alarm") == "crl_refresh_failed" for e in events)


def test_admin_cert_is_registered(client):
    res = client.get("/pki/users")
    assert res.status_code == 200
    admin = next(item for item in res.json() if item["cn"] == "admin")
    assert admin["serial"]
    assert len(admin["fingerprint"]) == 64
    assert admin["revoked_at"] == ""
    assert "token_secret_hash" not in admin


def test_revoke_writes_crl_and_lists_revoked(client):
    cert_path = _issue_user("alice-crl")
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())

    res = client.delete("/pki/users/alice-crl")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["serials"] == [format(cert.serial_number, "x")]

    users_crl = _crls(settings.pki_dir / "nginx" / "users.crl")[0]
    entry = next(e for e in users_crl if e.serial_number == cert.serial_number)
    assert entry.extensions.get_extension_for_class(x509.CRLReason).value.reason == x509.ReasonFlags.unspecified

    listed = next(i for i in client.get("/pki/users").json() if i["cn"] == "alice-crl")
    assert listed["revoked_at"]
    assert listed["reason"] == "unspecified"
    # 证书文件保留供审计；CRL 是有效性权威
    assert cert_path.exists()

    assert client.delete("/pki/users/alice-crl").status_code == 404


def test_revoke_reason_key_compromise(client):
    cert_path = _issue_user("bob-crl")
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    res = client.delete("/pki/users/bob-crl", params={"reason": "key_compromise"})
    assert res.status_code == 200, res.text
    for crl in _crls(settings.pki_dir / "nginx" / "clients.crl"):
        for entry in crl:
            if entry.serial_number == cert.serial_number:
                reason = entry.extensions.get_extension_for_class(x509.CRLReason).value
                assert reason.reason == x509.ReasonFlags.key_compromise
                break


def test_revoke_rejects_unknown_reason(client):
    _issue_user("carol-crl")
    res = client.delete("/pki/users/carol-crl", params={"reason": "made-up"})
    assert res.status_code == 400
    assert "revocation reason" in res.json()["detail"]


def test_revoke_kicks_user_sessions(client):
    _issue_user("dave-crl")
    jti, _, _ = sessions.create("dave-crl", ["viewer"], ttl_seconds=600)
    assert any(s["user"] == "dave-crl" for s in sessions.list_sessions())

    res = client.delete("/pki/users/dave-crl")
    assert res.status_code == 200, res.text
    assert res.json()["sessions"] == 1
    assert not any(s["user"] == "dave-crl" for s in sessions.list_sessions())
    assert sessions.resolve(jti) is None


def test_cannot_revoke_own_certificate():
    request = SimpleNamespace(state=SimpleNamespace(
        identity=Identity("alice", ["admin"], "cert")))
    with pytest.raises(HTTPException) as exc:
        certs_mod.revoke_user_cert("alice", request)
    assert exc.value.status_code == 400


def test_reload_failure_reports_500_but_crl_kept(client, monkeypatch):
    cert_path = _issue_user("erin-crl")
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())

    def _fail():
        raise RuntimeError("nginx unreachable")

    monkeypatch.setattr(certs_mod, "reload_nginx", _fail)
    res = client.delete("/pki/users/erin-crl")
    assert res.status_code == 500
    assert "nginx reload failed" in res.json()["detail"]
    assert cert.serial_number in _serials(settings.pki_dir / "nginx" / "clients.crl")


def test_issue_user_cert_signs_csr_and_registers(client):
    """签发：只回传证书（无私钥），CN/issuer 正确，公钥与本地 CSR 一致。"""
    key, res = _issue_via_api(client, "frank-certs")
    assert res.status_code == 201, res.text
    body = res.json()
    assert "private_key" not in body
    cert = x509.load_pem_x509_certificate(body["certificate"].encode())

    assert cert.subject.rfc4514_string() == "CN=frank-certs"
    assert cert.issuer.rfc4514_string() == "CN=kurrent-ca-users"

    def _pub_der(pub):
        return pub.public_bytes(serialization.Encoding.DER,
                                serialization.PublicFormat.SubjectPublicKeyInfo)

    assert _pub_der(cert.public_key()) == _pub_der(key.public_key())
    assert body["serial"] == format(cert.serial_number, "x")
    assert cert.not_valid_after_utc - cert.not_valid_before_utc <= dt.timedelta(days=30, minutes=10)

    listed = next(i for i in client.get("/pki/users").json()
                  if i["serial"] == body["serial"])
    assert listed["cn"] == "frank-certs"
    assert listed["revoked_at"] == ""

    # 控制面只留证书：issued/ 下没有私钥
    issued_dir = settings.pki_dir / "users" / "frank-certs" / "issued"
    assert [p.suffix for p in issued_dir.iterdir()] == [".crt"]


def test_issue_requires_registered_user(client):
    res = client.post("/pki/users/ghost-certs", json={})
    assert res.status_code == 400
    assert "users.yml" in res.json()["detail"]


def test_issue_requires_csr(client):
    _register_user("gina-certs")
    res = client.post("/pki/users/gina-certs", json={"days": 30})
    assert res.status_code == 400
    assert "csr required" in res.json()["detail"]


def test_issue_rejects_csr_cn_mismatch(client):
    _register_user("gina-certs")
    _, csr = _csr("someone-else")
    res = client.post("/pki/users/gina-certs", json={"csr": csr})
    assert res.status_code == 400
    assert "cn mismatch" in res.json()["detail"]


def test_issue_rejects_csr_extra_subject(client):
    _, res = _issue_via_api(client, "gina-certs", orgs=["admin"])
    assert res.status_code == 400
    assert "subject attribute" in res.json()["detail"]


def test_issue_rejects_bad_days(client):
    _, res = _issue_via_api(client, "gina-certs", days=0)
    assert res.status_code == 400
    assert "days" in res.json()["detail"]


def test_issue_rejects_unsafe_cn():
    _, csr = _csr("../escape")
    with pytest.raises(ValueError):
        pki.issue_user_cert(settings.pki_dir, "../escape", csr.encode())


def test_revoke_single_serial_keeps_other_active(client):
    """两张证书同 CN：按序列号吊销只废一张，会话按证书指纹精确踢。"""
    _, first_res = _issue_via_api(client, "henry-certs")
    _, second_res = _issue_via_api(client, "henry-certs")
    first, second = first_res.json(), second_res.json()
    assert first_res.status_code == 201 and second_res.status_code == 201

    jti, _, _ = sessions.create("henry-certs", ["viewer"], ttl_seconds=600,
                                cert_fp=first["fingerprint"])
    other, _, _ = sessions.create("henry-certs", ["viewer"], ttl_seconds=600,
                                  cert_fp=second["fingerprint"])

    res = client.delete("/pki/users/henry-certs", params={"serial": first["serial"]})
    assert res.status_code == 200, res.text
    assert res.json()["serials"] == [first["serial"]]
    assert res.json()["sessions"] == 1
    assert sessions.resolve(jti) is None
    assert sessions.resolve(other) is not None

    listed = {i["serial"]: i for i in client.get("/pki/users").json()
              if i["cn"] == "henry-certs"}
    assert listed[first["serial"]]["revoked_at"]
    assert listed[second["serial"]]["revoked_at"] == ""

    revoked_serials = _serials(settings.pki_dir / "nginx" / "clients.crl")
    assert int(first["serial"], 16) in revoked_serials
    assert int(second["serial"], 16) not in revoked_serials


def test_revoke_unknown_serial_404(client):
    _, res0 = _issue_via_api(client, "iris-certs")
    issued = res0.json()
    res = client.delete("/pki/users/iris-certs", params={"serial": "beef"})
    assert res.status_code == 404
    listed = next(i for i in client.get("/pki/users").json()
                  if i["serial"] == issued["serial"])
    assert listed["revoked_at"] == ""


def test_revoke_rejects_malformed_serial(client):
    _issue_via_api(client, "jack-certs")
    res = client.delete("/pki/users/jack-certs", params={"serial": "not-hex"})
    assert res.status_code == 400
    assert "serial" in res.json()["detail"]
