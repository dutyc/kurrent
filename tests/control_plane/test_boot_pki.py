"""Secure Boot 引导 PKI：独立 CA、叶证书 SAN、幂等、到期续签与 CA 轮换。"""

from pathlib import Path

import datetime as _dt
import hashlib
import ipaddress
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509.oid import NameOID

from control_plane.app import boot_pki


def _load(path: Path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def test_boot_pki_is_idempotent_and_issues_leaf_from_ca(tmp_path):
    pki, certs = tmp_path / "pki", tmp_path / "certs"
    first = boot_pki.ensure_boot_pki(pki, certs, "IP:10.0.0.10,DNS:boot.example")
    assert first["ca_created"] is True
    assert first["cert_created"] is True

    ca = _load(first["ca_path"])
    leaf = _load(first["cert_path"])
    assert leaf.issuer == ca.subject
    assert ca.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == \
        boot_pki.BOOT_CA_CN
    # 叶证书必须真由 boot CA 私钥签发（只比 issuer 名字不够）
    ca.public_key().verify(
        leaf.signature,
        leaf.tbs_certificate_bytes,
        padding.PKCS1v15(),
        leaf.signature_hash_algorithm,
    )
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    names = list(san)
    assert x509.IPAddress(ipaddress.ip_address("10.0.0.10")) in names
    assert x509.DNSName("boot.example") in names
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False

    assert (first["key_path"]).stat().st_mode & 0o777 == 0o600
    assert (boot_pki.boot_ca_dir(pki) / boot_pki.CA_KEY_FILENAME).stat().st_mode & 0o777 == 0o600

    second = boot_pki.ensure_boot_pki(pki, certs, "IP:10.0.0.10,DNS:boot.example")
    assert second["ca_created"] is False
    assert second["cert_created"] is False
    assert second["ca_sha256"] == first["ca_sha256"]
    assert second["cert_sha256"] == first["cert_sha256"]


def _sha256_der(cert) -> str:
    from cryptography.hazmat.primitives import serialization

    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def _write_expired_leaf(pki: Path, certs: Path) -> None:
    """用当前 boot CA 签一张已过期的叶证书落盘，复现「跑了一年之后」的磁盘状态。"""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    ca_dir = boot_pki.boot_ca_dir(pki)
    ca = _load(ca_dir / boot_pki.CA_CERT_FILENAME)
    ca_key = serialization.load_pem_private_key(
        (ca_dir / boot_pki.CA_KEY_FILENAME).read_bytes(), password=None)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = _dt.datetime.now(_dt.timezone.utc)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                                    boot_pki.BOOT_SERVER_CN)]))
        .issuer_name(ca.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=400))
        .not_valid_after(now - _dt.timedelta(days=1))
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )
    certs.mkdir(parents=True, exist_ok=True)
    (certs / boot_pki.SERVER_CERT_FILENAME).write_bytes(
        leaf.public_bytes(serialization.Encoding.PEM))
    # 叶私钥必须是这张证书对应的密钥，否则幂等检查会先因证书/密钥不匹配而重建
    (certs / boot_pki.SERVER_KEY_FILENAME).write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))


def test_boot_pki_reissues_leaf_on_expiry(tmp_path):
    """回归：叶证书 1 年有效，控制面重启必须检查有效期。

    真实故障形态：只比签名归属时，到期后重启仍复用旧证书——4443 提供服务但
    固定 boot CA 的签名固件拒绝握手，引导链断在 TLS，且续签不需要重刷固件。"""
    pki, certs = tmp_path / "pki", tmp_path / "certs"
    first = boot_pki.ensure_boot_pki(pki, certs, "IP:127.0.0.1")
    _write_expired_leaf(pki, certs)
    expired = _load(certs / boot_pki.SERVER_CERT_FILENAME)
    assert expired.not_valid_after_utc <= _dt.datetime.now(_dt.timezone.utc)

    renewed = boot_pki.ensure_boot_pki(pki, certs, "IP:127.0.0.1")
    assert renewed["cert_created"] is True
    assert renewed["cert_sha256"] != _sha256_der(expired)
    assert renewed["cert_sha256"] != first["cert_sha256"]
    assert renewed["ca_sha256"] == first["ca_sha256"]  # boot CA 不动：固件锚点保持有效
    assert _load(renewed["cert_path"]).not_valid_after_utc > _dt.datetime.now(
        _dt.timezone.utc) + _dt.timedelta(days=boot_pki.BOOT_CERT_DAYS - 1)


def test_boot_pki_reissues_leaf_before_renewal_window(tmp_path):
    """剩余有效期低于阈值（30 天）即续签，避免到期当天才发现问题。"""
    pki, certs = tmp_path / "pki", tmp_path / "certs"
    near = boot_pki.ensure_boot_pki(pki, certs, "IP:127.0.0.1",
                                    cert_days=boot_pki.BOOT_RENEW_BEFORE_DAYS - 1)
    again = boot_pki.ensure_boot_pki(pki, certs, "IP:127.0.0.1")
    assert again["cert_created"] is True
    assert again["cert_sha256"] != near["cert_sha256"]


def test_boot_pki_keeps_healthy_leaf(tmp_path):
    pki, certs = tmp_path / "pki", tmp_path / "certs"
    first = boot_pki.ensure_boot_pki(pki, certs, "IP:127.0.0.1")
    second = boot_pki.ensure_boot_pki(pki, certs, "IP:127.0.0.1")
    assert second["cert_created"] is False
    assert second["cert_sha256"] == first["cert_sha256"]


def test_boot_pki_reissues_leaf_when_ca_is_replaced(tmp_path):
    pki, certs = tmp_path / "pki", tmp_path / "certs"
    first = boot_pki.ensure_boot_pki(pki, certs, "IP:127.0.0.1")

    ca_dir = boot_pki.boot_ca_dir(pki)
    (ca_dir / boot_pki.CA_CERT_FILENAME).unlink()
    (ca_dir / boot_pki.CA_KEY_FILENAME).unlink()

    second = boot_pki.ensure_boot_pki(pki, certs, "IP:127.0.0.1")
    assert second["ca_created"] is True
    assert second["cert_created"] is True
    assert second["ca_sha256"] != first["ca_sha256"]
    assert _load(second["cert_path"]).issuer == _load(second["ca_path"]).subject


def test_boot_pki_does_not_touch_component_pki(tmp_path):
    """boot CA 与 users/nodes/control 三分根分离，绝不写进 ca-bundle。"""
    pki, certs = tmp_path / "pki", tmp_path / "certs"
    boot_pki.ensure_boot_pki(pki, certs, "IP:127.0.0.1")
    assert not (pki / "nginx").exists()
    assert not (pki / "ca" / "users").exists()
    assert boot_pki.boot_ca_dir(pki) == pki / "ca" / "boot"
