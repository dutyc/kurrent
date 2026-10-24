"""真实 TLS 集成：peer cert hook + 应用层 CN/CRL 热更新。

验证三件在单测里看不到的事：
1. uvicorn 的 peer cert 能进入 ASGI scope（hook 有效）；
2. CN/issuer 不匹配被 403；
3. CRL 更新后无需重启，下一次请求即拒绝已吊销证书。
"""

import datetime as dt
import http.client
import importlib.util
import ipaddress
import os
import socket
import ssl
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import FastAPI, Request

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


security = _load("peer_gate_security", "storager/agent/app/security.py")


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _ca(cn: str):
    key, now = _key(), dt.datetime.now(dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                           critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


def _leaf(ca_key, ca_cert, cn: str, usage, *, sans=(), ips=(), serial=None):
    key, now = _key(), dt.datetime.now(dt.timezone.utc)
    builder = (x509.CertificateBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
               .issuer_name(ca_cert.subject)
               .public_key(key.public_key())
               .serial_number(serial or x509.random_serial_number())
               .not_valid_before(now - dt.timedelta(days=1))
               .not_valid_after(now + dt.timedelta(days=7))
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                              critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                   ca_key.public_key()), critical=False)
               .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
               .add_extension(x509.KeyUsage(True, usage == ExtendedKeyUsageOID.SERVER_AUTH,
                                            usage == ExtendedKeyUsageOID.SERVER_AUTH,
                                            False, False, False, False, False, False),
                              critical=True))
    names = ([x509.DNSName(s) for s in sans]
             + [x509.IPAddress(ipaddress.ip_address(s)) for s in ips])
    if names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(names), critical=False)
    return key, builder.sign(ca_key, hashes.SHA256())


def _crl(ca_key, ca_cert, revoked=()):
    now = dt.datetime.now(dt.timezone.utc)
    builder = (x509.CertificateRevocationListBuilder()
               .issuer_name(ca_cert.subject)
               .last_update(now - dt.timedelta(minutes=5))
               .next_update(now + dt.timedelta(days=30))
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                   ca_key.public_key()), critical=False))
    for serial in revoked:
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder().serial_number(serial)
            .revocation_date(now).build())
    return builder.sign(ca_key, hashes.SHA256())


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _pem_key(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption())


def _cert_pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _client_context(ca_pem: Path, cert=None, key=None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(ca_pem))
    if cert and key:
        ctx.load_cert_chain(str(cert), str(key))
    return ctx


def _get(port: int, ctx: ssl.SSLContext) -> int:
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=5)
    try:
        conn.request("GET", "/probe")
        return conn.getresponse().status
    finally:
        conn.close()


@pytest.fixture()
def gate_server(tmp_path, monkeypatch):
    monkeypatch.delenv(security.INSECURE_ENV, raising=False)
    ca_key, ca_cert = _ca("kurrent-ca-control")
    _write(tmp_path / "ca-control.crt", _cert_pem(ca_cert))
    server_key, server_cert = _leaf(ca_key, ca_cert, "localhost",
                                    ExtendedKeyUsageOID.SERVER_AUTH,
                                    sans=["localhost"], ips=["127.0.0.1"])
    _write(tmp_path / "serving.crt", _cert_pem(server_cert))
    _write(tmp_path / "serving.key", _pem_key(server_key))

    good_key, good_cert = _leaf(ca_key, ca_cert, "control-plane",
                                ExtendedKeyUsageOID.CLIENT_AUTH)
    wrong_key, wrong_cert = _leaf(ca_key, ca_cert, "someone-else",
                                  ExtendedKeyUsageOID.CLIENT_AUTH)
    revoked_key, revoked_cert = _leaf(ca_key, ca_cert, "control-plane",
                                      ExtendedKeyUsageOID.CLIENT_AUTH, serial=4242)
    for name, key, cert in (("good", good_key, good_cert),
                            ("wrong", wrong_key, wrong_cert),
                            ("revoked", revoked_key, revoked_cert)):
        _write(tmp_path / f"{name}.crt", _cert_pem(cert))
        _write(tmp_path / f"{name}.key", _pem_key(key))

    store = security.CrlStore()
    store.update("control", _crl(ca_key, ca_cert))

    app = FastAPI()

    @app.get("/probe")
    def probe(request: Request):
        ident = security.verify_peer(
            request.scope, allowed_cns={"control-plane"},
            issuer_cn="kurrent-ca-control", crl_kind="control", store=store)
        return {"cn": ident.cn}

    security.install_peer_cert_hook()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    context = security.build_server_context(tmp_path, "ca-control.crt")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, http="h11",
                            log_level="error", access_log=False,
                            ssl_context_factory=lambda _cfg, _default: context)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    try:
        yield {"port": port, "tmp": tmp_path, "store": store,
               "ca_key": ca_key, "ca_cert": ca_cert}
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_valid_cert_passes_and_scope_has_peer_cert(gate_server):
    tmp = gate_server["tmp"]
    ctx = _client_context(tmp / "ca-control.crt", tmp / "good.crt", tmp / "good.key")
    assert _get(gate_server["port"], ctx) == 200


def test_wrong_cn_rejected(gate_server):
    tmp = gate_server["tmp"]
    ctx = _client_context(tmp / "ca-control.crt", tmp / "wrong.crt", tmp / "wrong.key")
    assert _get(gate_server["port"], ctx) == 403


def test_revocation_applies_without_restart(gate_server):
    tmp = gate_server["tmp"]
    ctx = _client_context(tmp / "ca-control.crt",
                          tmp / "revoked.crt", tmp / "revoked.key")
    assert _get(gate_server["port"], ctx) == 200
    gate_server["store"].update("control", _crl(gate_server["ca_key"],
                                                gate_server["ca_cert"], revoked=[4242]))
    assert _get(gate_server["port"], ctx) == 403


def test_cert_from_other_ca_rejected_at_handshake(gate_server):
    tmp = gate_server["tmp"]
    other_key, other_ca = _ca("kurrent-ca-users")
    key, cert = _leaf(other_key, other_ca, "control-plane",
                      ExtendedKeyUsageOID.CLIENT_AUTH)
    _write(tmp / "other.crt", _cert_pem(cert))
    _write(tmp / "other.key", _pem_key(key))
    ctx = _client_context(tmp / "ca-control.crt", tmp / "other.crt", tmp / "other.key")
    with pytest.raises((ssl.SSLError, ConnectionError)):
        _get(gate_server["port"], ctx)
