"""证书自动生成（TOFU 引导链）：幂等、指纹格式、SAN 解析、声明漂移与轮换。"""

import hashlib
import ipaddress
import os
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID

from control_plane.app.cert_bootstrap import (DEFAULT_CN, METADATA_FILENAME,
                                              ensure_server_cert,
                                              inspect_server_cert, parse_san)


def _load_cert(cert_dir: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate((cert_dir / "server.crt").read_bytes())


def test_ensure_server_cert_generates_files_and_fingerprint(tmp_path: Path):
    result = ensure_server_cert(tmp_path)
    fp = result["fingerprint"]
    assert result["generated"] is True and result["rotated"] is False
    assert fp and len(fp) == 64
    assert int(fp, 16) >= 0
    cert = _load_cert(tmp_path)
    # 指纹 = DER SHA-256（与 openssl x509 -outform DER | sha256sum 一致）
    assert fp == hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    assert (tmp_path / "fingerprint.txt").read_text().split()[0] == fp
    # 元数据记录声明基线（SAN/days/指纹），供后续启动做漂移判定
    meta = (tmp_path / METADATA_FILENAME).read_text(encoding="utf-8")
    assert '"days": 3650' in meta and fp in meta
    # 私钥 PEM + 0600 权限（POSIX 语义；Windows 上 os.chmod 权限位不生效，跳过）
    key = serialization.load_pem_private_key((tmp_path / "server.key").read_bytes(), password=None)
    assert key.key_size == 2048
    if sys.platform != "win32":
        assert os.stat(tmp_path / "server.key").st_mode & 0o777 == 0o600


def test_ensure_server_cert_idempotent(tmp_path: Path):
    first = ensure_server_cert(tmp_path)
    crt_before = (tmp_path / "server.crt").read_bytes()
    key_before = (tmp_path / "server.key").read_bytes()
    second = ensure_server_cert(tmp_path)
    assert second["generated"] is False and second["rotated"] is False
    assert second["fingerprint"] == first["fingerprint"]
    assert second["drift"] == []
    assert (tmp_path / "server.crt").read_bytes() == crt_before
    assert (tmp_path / "server.key").read_bytes() == key_before


def test_ensure_server_cert_subject_and_san(tmp_path: Path):
    ensure_server_cert(tmp_path, san_spec="IP:192.168.80.3,DNS:localhost")
    cert = _load_cert(tmp_path)
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == DEFAULT_CN
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("192.168.80.3")]
    assert san.get_values_for_type(x509.DNSName) == ["localhost"]


def test_parse_san_normalizes_and_deduplicates():
    names = parse_san("IP:127.0.0.1, DNS:localhost")
    assert [type(n).__name__ for n in names] == ["IPAddress", "DNSName"]
    assert names[0].value == ipaddress.ip_address("127.0.0.1")
    assert names[1].value == "localhost"


@pytest.mark.parametrize("spec", [
    "IP:not-an-ip",
    "bogus",
    "URL:https://example.com",
    "IP:127.0.0.1,IP:127.0.0.1",
    "",
])
def test_parse_san_rejects_invalid_entries(spec: str):
    with pytest.raises(ValueError):
        parse_san(spec)


def test_san_drift_reported_but_kept_by_default(tmp_path: Path):
    first = ensure_server_cert(tmp_path, "IP:127.0.0.1,DNS:localhost", 3650)
    result = ensure_server_cert(tmp_path, "IP:10.0.0.1,DNS:localhost", 3650)
    assert result["rotated"] is False and result["drift"] == ["san"]
    assert result["fingerprint"] == first["fingerprint"]
    assert inspect_server_cert(tmp_path, "IP:10.0.0.1,DNS:localhost", 3650)["drift"] == ["san"]


def test_rotation_on_change_regenerates_and_records_metadata(tmp_path: Path):
    first = ensure_server_cert(tmp_path, "IP:127.0.0.1,DNS:localhost", 3650)
    result = ensure_server_cert(tmp_path, "IP:10.0.0.1,DNS:localhost", 7300,
                                rotate_on_change=True)
    assert result["rotated"] is True and result["drift"] == ["san", "days"]
    assert result["fingerprint"] != first["fingerprint"]
    status = inspect_server_cert(tmp_path, "IP:10.0.0.1,DNS:localhost", 7300)
    assert status["drift"] == [] and status["days"] == 7300


def test_days_drift_reported(tmp_path: Path):
    ensure_server_cert(tmp_path, "IP:127.0.0.1,DNS:localhost", 3650)
    status = inspect_server_cert(tmp_path, "IP:127.0.0.1,DNS:localhost", 100)
    assert status["present"] is True
    assert status["drift"] == ["days"]


def test_legacy_cert_without_metadata_adopts_baseline(tmp_path: Path):
    first = ensure_server_cert(tmp_path, "IP:127.0.0.1,DNS:localhost", 3650)
    (tmp_path / METADATA_FILENAME).unlink()
    result = ensure_server_cert(tmp_path, "IP:127.0.0.1,DNS:localhost", 100)
    assert result["drift"] == []  # 旧证书无 days 基线：以当前声明补齐，不误报
    assert result["fingerprint"] == first["fingerprint"]
    assert inspect_server_cert(tmp_path, "IP:127.0.0.1,DNS:localhost", 100)["drift"] == []


def test_corrupt_key_regenerated(tmp_path: Path):
    ensure_server_cert(tmp_path)
    (tmp_path / "server.key").write_bytes(b"not a key")
    result = ensure_server_cert(tmp_path)
    assert result["generated"] is True
    assert inspect_server_cert(tmp_path, "IP:127.0.0.1,DNS:localhost", 3650)["present"] is True
