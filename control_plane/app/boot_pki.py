"""Secure Boot 引导 PKI：独立 boot CA + 4443 引导 HTTPS 叶证书。

与组件三分根（users/nodes/control）完全分离：boot CA 只签引导端点证书，
不进 nginx 客户端信任包，也不参与组件 CRL。443 的 TOFU 证书不受影响——
存量 L1 设备 pin 的是 443 叶证书，换端口与换锚互不干扰。

布局：
  state/pki/ca/boot/ca.crt|ca.key            boot CA（10 年）
  state/certs/boot-server.crt|boot-server.key 引导端点叶证书（1 年）

轮换 = 删对应文件后重启控制面。boot CA 轮换会让既有签名固件拒绝握手，
必须同时重建并重刷固件；叶证书在每次启动时检查，到期前自动续签（只影响服务端，
固件无需变更）。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .cert_bootstrap import parse_san

log = logging.getLogger("control-plane")

BOOT_CA_DIRNAME = "boot"
BOOT_CA_CN = "kurrent-boot-ca"
BOOT_SERVER_CN = "kurrent-boot-server"
BOOT_CA_DAYS = 3650
BOOT_CERT_DAYS = 365
# 剩余有效期低于该阈值即重签：长跑的控制面不会在到期后把 4443 变成固件拒绝的死端口。
BOOT_RENEW_BEFORE_DAYS = 30
CA_CERT_FILENAME = "ca.crt"
CA_KEY_FILENAME = "ca.key"
SERVER_CERT_FILENAME = "boot-server.crt"
SERVER_KEY_FILENAME = "boot-server.key"


def boot_ca_dir(pki_dir: str | Path) -> Path:
    return Path(pki_dir) / "ca" / BOOT_CA_DIRNAME


def _write_key(path: Path, key) -> None:
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(path, 0o600)


def _load_cert(path: Path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def _ensure_ca(pki_dir: Path) -> tuple[x509.Certificate, object, bool]:
    directory = boot_ca_dir(pki_dir)
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / CA_CERT_FILENAME
    key_path = directory / CA_KEY_FILENAME
    if cert_path.exists() and key_path.exists():
        return _load_cert(cert_path), serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        ), False

    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, BOOT_CA_CN)])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=BOOT_CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=False, content_commitment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    _write_key(key_path, key)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.warning("boot-pki: generated boot CA (cn=%s, %d days)", BOOT_CA_CN, BOOT_CA_DAYS)
    return cert, key, True


def _leaf_signed_by(leaf: x509.Certificate, ca_cert: x509.Certificate) -> bool:
    """同名 CA 重建后 issuer 名字不变，必须用签名本身判断叶证书是否仍归当前 CA。"""
    try:
        ca_cert.public_key().verify(
            leaf.signature,
            leaf.tbs_certificate_bytes,
            padding.PKCS1v15(),
            leaf.signature_hash_algorithm,
        )
        return True
    except (InvalidSignature, ValueError):
        return False


def _leaf_usable(leaf: x509.Certificate, ca_cert: x509.Certificate, now) -> bool:
    """已存在叶证书能否继续使用：仍归当前 CA 签发，且剩余有效期高于续签阈值。

    只比签名归属不够——叶证书 1 年有效，控制面重启若不看有效期就永远不会续签，
    到期后固定 CA 的签名固件会直接拒绝握手（引导失败），而续签不需要重刷固件。"""
    if leaf.not_valid_after_utc <= now + _dt.timedelta(days=BOOT_RENEW_BEFORE_DAYS):
        return False
    return _leaf_signed_by(leaf, ca_cert)


def _ensure_server_cert(
    cert_dir: Path,
    ca_cert: x509.Certificate,
    ca_key,
    san_spec: str,
    days: int,
) -> tuple[x509.Certificate, bool]:
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / SERVER_CERT_FILENAME
    key_path = cert_dir / SERVER_KEY_FILENAME
    if cert_path.exists() and key_path.exists():
        try:
            existing = _load_cert(cert_path)
        except (OSError, ValueError):
            existing = None
        if existing is not None and _leaf_usable(existing, ca_cert,
                                                _dt.datetime.now(_dt.timezone.utc)):
            return existing, False

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = _dt.datetime.now(_dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, BOOT_SERVER_CN)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True, content_commitment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
    )
    san = parse_san(san_spec)
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
    cert = builder.sign(private_key=ca_key, algorithm=hashes.SHA256())
    _write_key(key_path, key)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("boot-pki: issued %s (SAN=%s)", SERVER_CERT_FILENAME, san_spec)
    return cert, True


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_boot_pki(
    pki_dir: str | Path,
    cert_dir: str | Path,
    san_spec: str,
    cert_days: int = BOOT_CERT_DAYS,
) -> dict:
    """幂等生成 boot CA 与引导叶证书，返回状态（不改动组件 PKI）。"""
    pki_dir = Path(pki_dir)
    cert_dir = Path(cert_dir)
    ca_cert, ca_key, ca_created = _ensure_ca(pki_dir)
    server_cert, server_created = _ensure_server_cert(
        cert_dir, ca_cert, ca_key, san_spec, cert_days
    )
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    cert_pem = server_cert.public_bytes(serialization.Encoding.PEM)
    return {
        "ca_path": boot_ca_dir(pki_dir) / CA_CERT_FILENAME,
        "cert_path": cert_dir / SERVER_CERT_FILENAME,
        "key_path": cert_dir / SERVER_KEY_FILENAME,
        "ca_sha256": _sha256_hex(
            ca_cert.public_bytes(serialization.Encoding.DER)
        ),
        "cert_sha256": _sha256_hex(
            server_cert.public_bytes(serialization.Encoding.DER)
        ),
        "ca_created": ca_created,
        "cert_created": server_created,
        "ca_pem": ca_pem,
        "cert_pem": cert_pem,
    }
