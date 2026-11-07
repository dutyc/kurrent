"""一次性管理凭据交换（bootstrap_exchange）。

交换包是 admin.conf 的副本：只读源目录内的普通文件、包内相对路径、ready 清单
最后写入并含逐文件 SHA-256；request 只在 ready 成功后删除。
"""

import hashlib
import json
import os

import pytest
import yaml

from control_plane.app import bootstrap_exchange, pki


def _write_source(tmp_path):
    """构造真实布局的 admin.conf + 证书文件（内容不参与校验，交换只复制字节）。"""
    state = tmp_path / "state"
    (state / "certs").mkdir(parents=True)
    (state / "pki" / "users" / "admin").mkdir(parents=True)
    (state / "certs" / "server.crt").write_text("server-ca", encoding="utf-8")
    (state / "pki" / "users" / "admin" / "client.crt").write_text("client-cert", encoding="utf-8")
    (state / "pki" / "users" / "admin" / "client.key").write_text("client-key", encoding="utf-8")
    admin_conf = state / "admin.conf"
    admin_conf.write_text(
        "server: https://127.0.0.1/api/cp\n"
        "certificate-authority: certs/server.crt\n"
        "client-certificate: pki/users/admin/client.crt\n"
        "client-key: pki/users/admin/client.key\n",
        encoding="utf-8",
    )
    return state, admin_conf


def _request(exchange):
    exchange.mkdir(mode=0o700, exist_ok=True)
    (exchange / bootstrap_exchange.REQUEST).write_text("requested\n", encoding="utf-8")


def test_no_request_is_a_noop(tmp_path):
    exchange = tmp_path / "exchange"
    exchange.mkdir()
    assert bootstrap_exchange.publish_requested_bundle(exchange, tmp_path / "missing.conf") is False
    assert list(exchange.iterdir()) == []


def test_publishes_bundle_and_removes_request(tmp_path):
    state, admin_conf = _write_source(tmp_path)
    exchange = tmp_path / "exchange"
    _request(exchange)

    assert bootstrap_exchange.publish_requested_bundle(exchange, admin_conf) is True

    assert not (exchange / bootstrap_exchange.REQUEST).exists()
    ready = json.loads((exchange / bootstrap_exchange.READY).read_text(encoding="utf-8"))
    assert ready["kind"] == "BootstrapBundle"
    assert set(ready["files"]) == {
        "admin.conf", "pki/server.crt", "pki/client.crt", "pki/client.key",
    }
    for name, digest in ready["sha256"].items():
        assert hashlib.sha256((exchange / name).read_bytes()).hexdigest() == digest
        assert (exchange / name).stat().st_mode & 0o777 == 0o600
    assert (exchange / "pki").stat().st_mode & 0o777 == 0o700

    copied = yaml.safe_load((exchange / "admin.conf").read_text(encoding="utf-8"))
    assert copied["server"] == "https://127.0.0.1/api/cp"
    for key in ("certificate-authority", "client-certificate", "client-key"):
        assert copied[key].startswith("pki/") and ".." not in copied[key]
    # 服务端权威文件保持不变
    assert admin_conf.read_text(encoding="utf-8").startswith("server:")


def test_aligns_owner_of_exchange_pki_directory(tmp_path, monkeypatch):
    _, admin_conf = _write_source(tmp_path)
    exchange = tmp_path / "exchange"
    _request(exchange)
    aligned = []
    original = bootstrap_exchange._chown_like

    def record(path, ref):
        aligned.append(path)
        original(path, ref)

    monkeypatch.setattr(bootstrap_exchange, "_chown_like", record)

    assert bootstrap_exchange.publish_requested_bundle(exchange, admin_conf) is True
    # 控制面以 root 运行时 pki/ 不降属主，部署用户无法进入目录读取 ready 列出的证书
    assert exchange / bootstrap_exchange.PKI_DIR in aligned


def test_second_publish_is_idempotent(tmp_path):
    _, admin_conf = _write_source(tmp_path)
    exchange = tmp_path / "exchange"
    _request(exchange)
    assert bootstrap_exchange.publish_requested_bundle(exchange, admin_conf) is True
    first = (exchange / bootstrap_exchange.READY).read_text(encoding="utf-8")
    assert bootstrap_exchange.publish_requested_bundle(exchange, admin_conf) is False
    assert (exchange / bootstrap_exchange.READY).read_text(encoding="utf-8") == first


def test_rejects_path_escape(tmp_path):
    state, admin_conf = _write_source(tmp_path)
    (tmp_path / "outside.crt").write_text("outside", encoding="utf-8")
    admin_conf.write_text(
        "server: https://127.0.0.1/api/cp\n"
        "certificate-authority: ../outside.crt\n"
        "client-certificate: pki/users/admin/client.crt\n"
        "client-key: pki/users/admin/client.key\n",
        encoding="utf-8",
    )
    exchange = tmp_path / "exchange"
    _request(exchange)
    with pytest.raises(bootstrap_exchange.BootstrapExchangeError, match="escapes"):
        bootstrap_exchange.publish_requested_bundle(exchange, admin_conf)
    assert not (exchange / bootstrap_exchange.READY).exists()
    assert (exchange / bootstrap_exchange.REQUEST).exists()


def test_rejects_symlinked_certificate(tmp_path):
    state, admin_conf = _write_source(tmp_path)
    target = state / "certs" / "server.crt"
    link = state / "certs" / "link.crt"
    link.symlink_to(target)
    admin_conf.write_text(
        "server: https://127.0.0.1/api/cp\n"
        "certificate-authority: certs/link.crt\n"
        "client-certificate: pki/users/admin/client.crt\n"
        "client-key: pki/users/admin/client.key\n",
        encoding="utf-8",
    )
    exchange = tmp_path / "exchange"
    _request(exchange)
    with pytest.raises(bootstrap_exchange.BootstrapExchangeError, match="symlink"):
        bootstrap_exchange.publish_requested_bundle(exchange, admin_conf)


def test_incomplete_admin_conf_fails_without_ready(tmp_path):
    _, admin_conf = _write_source(tmp_path)
    admin_conf.write_text("server: https://127.0.0.1/api/cp\n", encoding="utf-8")
    exchange = tmp_path / "exchange"
    _request(exchange)
    with pytest.raises(bootstrap_exchange.BootstrapExchangeError, match="incomplete"):
        bootstrap_exchange.publish_requested_bundle(exchange, admin_conf)
    assert not (exchange / bootstrap_exchange.READY).exists()


def test_admin_conf_server_uses_https_port(tmp_path, monkeypatch):
    pki_dir = tmp_path / "pki"
    ca_cert, ca_key = pki.ensure_ca(pki_dir, "users")
    monkeypatch.setenv("KURRENT_HTTPS_PORT", "8443")
    conf = tmp_path / "admin.conf"
    pki.ensure_admin_credentials(pki_dir, ca_cert, ca_key, conf)
    assert "server: https://127.0.0.1:8443/api/cp" in conf.read_text(encoding="utf-8")

    monkeypatch.setenv("KURRENT_HTTPS_PORT", "443")
    conf2 = tmp_path / "admin-443.conf"
    pki.ensure_admin_credentials(pki_dir, ca_cert, ca_key, conf2)
    assert "server: https://127.0.0.1/api/cp" in conf2.read_text(encoding="utf-8")
