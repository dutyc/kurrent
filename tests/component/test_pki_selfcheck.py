"""组件启动吊销自检：公开 nodes.crl → 比对本组件证书序列号 → 重新 enroll。

覆盖两个组件实现（agent / nvmet-host），并区分“可验证命中”与
“拉取失败 / 验签失败 / issuer 不符 / CRL 过期 → 不据此改变启动行为”。
"""

import datetime as dt
import importlib.util
import ssl
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[2]

CLIENT_SERIAL = 0x111
SERVING_SERIAL = 0x222


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AGENT = _load("agent_pki_selfcheck", "storager/agent/app/pki_client.py")
NVMET = _load("nvmet_pki_selfcheck", "storager/nvmeof/nvmet-host/app/pki_client.py")
MODULES = [pytest.param(AGENT, id="agent"), pytest.param(NVMET, id="nvmet-host")]


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(cn: str):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _ca(cn: str = "kurrent-ca-nodes"):
    key = _key()
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                           critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


def _leaf(ca_key, ca_cert, cn: str, serial: int, *, expired: bool = False):
    key = _key()
    now = dt.datetime.now(dt.timezone.utc)
    if expired:
        before, after = now - dt.timedelta(days=30), now - dt.timedelta(days=1)
    else:
        before, after = now - dt.timedelta(days=1), now + dt.timedelta(days=365)
    cert = (x509.CertificateBuilder()
            .subject_name(_name(cn)).issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(serial)
            .not_valid_before(before).not_valid_after(after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def _crl(ca_key, issuer, revoked=(), *, next_update_days: int = 30):
    now = dt.datetime.now(dt.timezone.utc)
    last = now - dt.timedelta(minutes=5)
    next_update = now + dt.timedelta(days=next_update_days)
    if next_update <= last:
        last = now - dt.timedelta(days=10)
    builder = (x509.CertificateRevocationListBuilder()
               .issuer_name(issuer).last_update(last).next_update(next_update))
    for serial in revoked:
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder().serial_number(serial)
            .revocation_date(now).build())
    return builder.sign(ca_key, hashes.SHA256())


def _pem_key(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption())


def _pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _make_pki(tmp_path: Path, *, revoked=(), expired_client=False,
              crl_override=None) -> dict:
    pki = tmp_path / "pki"
    pki.mkdir(parents=True)
    ca_key, ca_cert = _ca()
    client_key, client_cert = _leaf(ca_key, ca_cert, "agent-node-a", CLIENT_SERIAL,
                                    expired=expired_client)
    serving_key, serving_cert = _leaf(ca_key, ca_cert, "agent-node-a", SERVING_SERIAL)
    (pki / "ca-nodes.crt").write_bytes(_pem(ca_cert))
    (pki / "ca-control.crt").write_bytes(_pem(ca_cert))
    (pki / "client.crt").write_bytes(_pem(client_cert))
    (pki / "client.key").write_bytes(_pem_key(client_key))
    (pki / "serving.crt").write_bytes(_pem(serving_cert))
    (pki / "serving.key").write_bytes(_pem_key(serving_key))
    crl = crl_override if crl_override is not None else _crl(ca_key, ca_cert.subject, revoked)
    (pki / "nodes.crl").write_bytes(crl.public_bytes(serialization.Encoding.PEM))
    return {"dir": pki, "ca_key": ca_key, "ca_cert": ca_cert}


class _Resp:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


def _serve_crl(monkeypatch, mod, pem: bytes) -> None:
    def fake_urlopen(req, *, context=None, timeout=None):
        assert req.full_url.endswith("/crl/nodes.crl")
        assert context is not None and timeout == mod.CRL_FETCH_TIMEOUT
        return _Resp(pem)

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)


def _client(mod, pki: Path, *, token=None, cp_base="https://cp", cp_ca=None):
    component = "agent" if mod is AGENT else "nvmet-host"
    return mod.PkiClient("node-a", component, pki, cp_base, cp_ca, token)


@pytest.fixture(autouse=True)
def _enable_self_check(monkeypatch):
    monkeypatch.setenv("KURRENT_PKI_SELF_CHECK", "1")


@pytest.mark.parametrize("mod", MODULES)
def test_self_revoked_client_and_serving_serial(mod, tmp_path, monkeypatch):
    for serial in (CLIENT_SERIAL, SERVING_SERIAL):
        pki = _make_pki(tmp_path / f"revoked-{serial}", revoked=[serial])
        _serve_crl(monkeypatch, mod, (pki["dir"] / "nodes.crl").read_bytes())
        client = _client(mod, pki["dir"])
        assert client._self_revoked() is True


@pytest.mark.parametrize("mod", MODULES)
def test_self_revoked_false_when_not_listed(mod, tmp_path, monkeypatch):
    pki = _make_pki(tmp_path, revoked=[0x999])
    _serve_crl(monkeypatch, mod, (pki["dir"] / "nodes.crl").read_bytes())
    assert _client(mod, pki["dir"])._self_revoked() is False


@pytest.mark.parametrize("mod", MODULES)
def test_self_revoked_ignores_untrusted_or_stale_crl(mod, tmp_path, monkeypatch):
    pki = _make_pki(tmp_path, revoked=[])
    other_key, other_ca = _ca("other-ca")
    cases = {
        "wrong-signature": _crl(other_key, pki["ca_cert"].subject, [CLIENT_SERIAL]),
        "wrong-issuer": _crl(pki["ca_key"], other_ca.subject, [CLIENT_SERIAL]),
        "stale": _crl(pki["ca_key"], pki["ca_cert"].subject, [CLIENT_SERIAL],
                      next_update_days=-1),
    }
    for label, crl in cases.items():
        _serve_crl(monkeypatch, mod, crl.public_bytes(serialization.Encoding.PEM))
        client = _client(mod, pki["dir"])
        assert client._self_revoked() is False, label


@pytest.mark.parametrize("mod", MODULES)
def test_self_revoked_fetch_failure_is_unknown(mod, tmp_path, monkeypatch):
    pki = _make_pki(tmp_path, revoked=[CLIENT_SERIAL])

    def offline(req, *, context=None, timeout=None):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(mod.urllib.request, "urlopen", offline)
    assert _client(mod, pki["dir"])._self_revoked() is False


@pytest.mark.parametrize("mod", MODULES)
def test_ensure_revoked_uses_bootstrap_not_renew(mod, tmp_path, monkeypatch):
    pki = _make_pki(tmp_path, revoked=[CLIENT_SERIAL])
    _serve_crl(monkeypatch, mod, (pki["dir"] / "nodes.crl").read_bytes())
    client = _client(mod, pki["dir"], token="tok.123")
    calls = []
    monkeypatch.setattr(client, "_enroll", lambda renew: calls.append(renew))
    client.ensure()
    assert calls == [False]


def test_ensure_revoked_survives_force_renew(tmp_path, monkeypatch):
    """join 删除派生凭据触发 force_renew：不得绕过吊销自检去走必然失败的 renew。"""
    pki = _make_pki(tmp_path, revoked=[CLIENT_SERIAL])
    _serve_crl(monkeypatch, AGENT, (pki["dir"] / "nodes.crl").read_bytes())
    client = _client(AGENT, pki["dir"], token="tok.123")
    calls = []
    monkeypatch.setattr(client, "_enroll", lambda renew: calls.append(renew))
    client.ensure(force_renew=True)
    assert calls == [False]


@pytest.mark.parametrize("mod", MODULES)
def test_ensure_self_check_runs_when_not_ready(mod, tmp_path, monkeypatch):
    """证书未达 renew 阈值（ready=false）时仍先查吊销：避免用旧证书 renew 被拒。"""
    pki = _make_pki(tmp_path, revoked=[CLIENT_SERIAL])
    (pki["dir"] / "ca-control.crt").unlink()  # ready() 依赖的信任锚缺失
    _serve_crl(monkeypatch, mod, (pki["dir"] / "nodes.crl").read_bytes())
    client = _client(mod, pki["dir"], token="tok.123")
    calls = []
    monkeypatch.setattr(client, "_enroll", lambda renew: calls.append(renew))
    client.ensure()
    assert calls == [False]


@pytest.mark.parametrize("mod", MODULES)
def test_ensure_revoked_without_token_is_actionable(mod, tmp_path, monkeypatch):
    pki = _make_pki(tmp_path, revoked=[CLIENT_SERIAL])
    _serve_crl(monkeypatch, mod, (pki["dir"] / "nodes.crl").read_bytes())
    client = _client(mod, pki["dir"], token=None)

    def no_token(renew):
        raise RuntimeError("no bootstrap token and no client cert: cannot enroll")

    monkeypatch.setattr(client, "_enroll", no_token)
    with pytest.raises(RuntimeError, match="bootstrap token"):
        client.ensure()


@pytest.mark.parametrize("mod", MODULES)
def test_ensure_expired_cert_bootstraps(mod, tmp_path, monkeypatch):
    pki = _make_pki(tmp_path, expired_client=True)
    client = _client(mod, pki["dir"], token="tok.123")
    calls = []
    monkeypatch.setattr(client, "_enroll", lambda renew: calls.append(renew))
    client.ensure()
    assert calls == [False]


def test_self_revoked_over_real_https(tmp_path, monkeypatch):
    """真实 TLS：公开 CRL 以 https 取回，组件验签后判定本机序列号已吊销。"""
    pki = _make_pki(tmp_path, revoked=[CLIENT_SERIAL])
    crl_bytes = (pki["dir"] / "nodes.crl").read_bytes()

    server_key, server_cert = _ca("test-https-ca")
    leaf_key, leaf_cert = _leaf(server_key, server_cert, "localhost", 0x333)
    (tmp_path / "server.crt").write_bytes(_pem(leaf_cert))
    (tmp_path / "server.key").write_bytes(_pem_key(leaf_key))
    (tmp_path / "cp-ca.crt").write_bytes(_pem(server_cert))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/crl/nodes.crl":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(crl_bytes)))
            self.end_headers()
            self.wfile.write(crl_bytes)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(tmp_path / "server.crt"), str(tmp_path / "server.key"))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        client = _client(AGENT, pki["dir"],
                         cp_base=f"https://127.0.0.1:{port}",
                         cp_ca=tmp_path / "cp-ca.crt")
        assert client._self_revoked() is True
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
