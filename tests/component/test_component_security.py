"""组件入站安全：peer cert 解析、CN/issuer 白名单、CRL 校验与分发落盘。

安全模块在两个镜像里各有一份（agent / nvmet-host），按文件路径加载，避免
`app.security` 包名冲突。
"""

import datetime as dt
import importlib.util
import os
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


agent_security = _load("agent_security", "storager/agent/app/security.py")
nvmet_security = _load("nvmet_security", "storager/nvmeof/nvmet-host/app/security.py")


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _ca(cn: str):
    key = _key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _crl(ca_key, ca_cert, *, revoked=(), next_update_days: int = 30) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    last = now - dt.timedelta(minutes=5 if next_update_days >= 0 else 2 * 24 * 60)
    builder = (x509.CertificateRevocationListBuilder()
               .issuer_name(ca_cert.subject)
               .last_update(last)
               .next_update(now + dt.timedelta(days=next_update_days)))
    for serial in revoked:
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder().serial_number(serial)
            .revocation_date(now).build())
    return builder.sign(ca_key, hashes.SHA256()).public_bytes(
        serialization.Encoding.PEM).decode()


def _scope(*, cn: str, issuer: str, serial: int) -> dict:
    return {"scheme": "https", "extensions": {"tls": {"peercert": {
        "subject": ((("commonName", cn),),),
        "issuer": ((("commonName", issuer),),),
        "serialNumber": format(serial, "X"),
    }}}}


def test_peer_identity_parses_cn_issuer_serial():
    ident = agent_security.peer_identity(_scope(cn="control-plane", issuer="kurrent-ca-control",
                                                serial=0x1A))
    assert (ident.cn, ident.issuer_cn, ident.serial) == ("control-plane", "kurrent-ca-control", 26)
    assert agent_security.peer_identity({}) is None


def test_verify_peer_allows_valid_and_denies(monkeypatch):
    monkeypatch.delenv(agent_security.INSECURE_ENV, raising=False)
    key, ca = _ca("kurrent-ca-control")
    store = agent_security.CrlStore()
    store.update("control", x509.load_pem_x509_crl(
        _crl(key, ca, revoked=[99]).encode()))
    kwargs = dict(allowed_cns={"control-plane"}, issuer_cn="kurrent-ca-control",
                  crl_kind="control", store=store)

    ident = agent_security.verify_peer(
        _scope(cn="control-plane", issuer="kurrent-ca-control", serial=10), **kwargs)
    assert ident.cn == "control-plane"

    with pytest.raises(HTTPException) as exc:
        agent_security.verify_peer(
            _scope(cn="control-plane", issuer="kurrent-ca-users", serial=10), **kwargs)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        agent_security.verify_peer(
            _scope(cn="agent-x", issuer="kurrent-ca-control", serial=10), **kwargs)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        agent_security.verify_peer(
            _scope(cn="control-plane", issuer="kurrent-ca-control", serial=99), **kwargs)
    assert exc.value.status_code == 403
    assert "revoked" in exc.value.detail


def test_verify_peer_fails_closed_without_fresh_crl(monkeypatch):
    monkeypatch.delenv(agent_security.INSECURE_ENV, raising=False)
    scope = _scope(cn="control-plane", issuer="kurrent-ca-control", serial=10)
    kwargs = dict(allowed_cns={"control-plane"}, issuer_cn="kurrent-ca-control",
                  crl_kind="control", store=agent_security.CrlStore())
    with pytest.raises(HTTPException) as exc:
        agent_security.verify_peer(scope, **kwargs)
    assert exc.value.status_code == 503

    key, ca = _ca("kurrent-ca-control")
    expired = agent_security.CrlStore()
    expired.update("control", x509.load_pem_x509_crl(
        _crl(key, ca, next_update_days=-1).encode()))
    kwargs["store"] = expired
    with pytest.raises(HTTPException) as exc:
        agent_security.verify_peer(scope, **kwargs)
    assert exc.value.status_code == 503

    monkeypatch.setenv(agent_security.INSECURE_ENV, "1")
    assert agent_security.verify_peer(scope, **kwargs) is None


def test_write_crls_validates_issuer_and_updates_store(tmp_path):
    nodes_key, nodes_ca = _ca("kurrent-ca-nodes")
    control_key, control_ca = _ca("kurrent-ca-control")
    (tmp_path / "ca-nodes.crt").write_bytes(nodes_ca.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "ca-control.crt").write_bytes(control_ca.public_bytes(serialization.Encoding.PEM))
    store = agent_security.CrlStore()
    agent_security.write_crls(tmp_path, {
        "nodes_crl": _crl(nodes_key, nodes_ca, revoked=[7]),
        "control_crl": _crl(control_key, control_ca, revoked=[8]),
    }, store)

    assert (tmp_path / "nodes.crl").is_file()
    assert (tmp_path / "control.crl").is_file()
    assert store.revoked("nodes", 7) and not store.revoked("nodes", 8)
    assert store.revoked("control", 8) and not store.revoked("control", 7)

    with pytest.raises(ValueError, match="issuer mismatch"):
        agent_security.write_crls(tmp_path, {
            "nodes_crl": _crl(control_key, control_ca),
            "control_crl": _crl(control_key, control_ca),
        }, store)


def test_peer_hook_is_idempotent():
    from uvicorn.protocols.http import h11_impl
    agent_security.install_peer_cert_hook()
    first = h11_impl.RequestResponseCycle.__init__
    agent_security.install_peer_cert_hook()
    assert h11_impl.RequestResponseCycle.__init__ is first


def test_server_context_requires_client_cert(tmp_path):
    key = _key()
    now = dt.datetime.now(dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "serving")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    (tmp_path / "serving.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "serving.key").write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()))
    (tmp_path / "ca-control.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    ctx = agent_security.build_server_context(tmp_path, "ca-control.crt")
    import ssl
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert ctx.maximum_version == ssl.TLSVersion.TLSv1_2


def test_nvmet_host_gate_uses_nodes_issuer(monkeypatch):
    monkeypatch.delenv(nvmet_security.INSECURE_ENV, raising=False)
    key, ca = _ca("kurrent-ca-nodes")
    store = nvmet_security.CrlStore()
    store.update("nodes", x509.load_pem_x509_crl(_crl(key, ca).encode()))
    ident = nvmet_security.verify_peer(
        _scope(cn="agent-node-a", issuer="kurrent-ca-nodes", serial=1),
        allowed_cns={"agent-node-a", "nvmet-node-a"},
        issuer_cn="kurrent-ca-nodes", crl_kind="nodes", store=store)
    assert ident.cn == "agent-node-a"


@pytest.mark.parametrize("mod", [agent_security, nvmet_security])
def test_initial_crl_fetch_retries_transient_failures(mod, monkeypatch):
    """启动期 502 不应变成一次性失败：短重试后成功。"""
    calls = []

    def flaky(client, pki_dir, store):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("crl fetch failed: HTTP 502")

    monkeypatch.setattr(mod, "refresh_once", flaky)
    assert mod._initial_fetch(None, Path("."), mod.CrlStore(), retries=5, delay=0) is True
    assert len(calls) == 3


@pytest.mark.parametrize("mod", [agent_security, nvmet_security])
def test_initial_crl_fetch_gives_up_after_retries(mod, monkeypatch):
    """持续失败时返回 False（入站 fail-closed，周期刷新继续），不抛异常。"""
    calls = []

    def down(client, pki_dir, store):
        calls.append(1)
        raise RuntimeError("crl fetch unreachable")

    monkeypatch.setattr(mod, "refresh_once", down)
    assert mod._initial_fetch(None, Path("."), mod.CrlStore(), retries=3, delay=0) is False
    assert len(calls) == 3
