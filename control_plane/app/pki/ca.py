"""pki 子模块：ca。"""

import logging

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pathlib import Path
import datetime as _dt
import os
from .common import CA_CN_PREFIX, CA_DAYS, CA_FILENAME, CA_KEY_FILENAME, CA_KINDS
from .common import _align_owner, _load_cert, _load_key, ca_dir

log = logging.getLogger("control-plane")

def ensure_ca_set(pki_dir: Path) -> dict[str, tuple]:
    """幂等确保三分根；遗留单 CA 在原位提升为 ca-nodes。

    存量组件证书都由旧 CA 签发：把旧 CA 归入 nodes 域，现有 agent/nvmet
    证书继续有效；users/control 两个域新生成，管理证书与控制面客户端证书
    在启动时按 issuer 检测后重签。
    """
    pki_dir = Path(pki_dir)
    legacy_cert = pki_dir / CA_FILENAME
    legacy_key = pki_dir / CA_KEY_FILENAME
    nodes_dir = ca_dir(pki_dir, "nodes")
    if legacy_cert.exists() and legacy_key.exists() and not nodes_dir.exists():
        nodes_dir.mkdir(parents=True, exist_ok=True)
        os.replace(legacy_cert, nodes_dir / CA_FILENAME)
        os.replace(legacy_key, nodes_dir / CA_KEY_FILENAME)
        log.warning("pki: promoted legacy CA to ca-nodes (existing component certs remain valid)")
    return {kind: ensure_ca(pki_dir, kind) for kind in CA_KINDS}


def ensure_ca(pki_dir: Path, kind: str = "nodes") -> tuple:
    """幂等生成指定域的 CA。返回 (cert, key)；已存在则加载（轮换 = 删该域目录重启）。"""
    directory = ca_dir(pki_dir, kind)
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / CA_FILENAME
    key_path = directory / CA_KEY_FILENAME
    if cert_path.exists() and key_path.exists():
        return _load_cert(cert_path), _load_key(key_path)

    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    cn = f"{CA_CN_PREFIX}{kind}"
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=CA_DAYS))
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
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)
    _align_owner(key_path)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _align_owner(cert_path)
    log.info("pki: generated %s CA (cn=%s, %d days)", kind, cn, CA_DAYS)
    from ..auth import audit
    audit.alarm("ca_generated", detail=f"{kind} CA {cn}", route="pki")
    return cert, key


def ca_cert_pem(pki_dir: Path, kind: str = "nodes") -> bytes:
    return (ca_dir(pki_dir, kind) / CA_FILENAME).read_bytes()


def write_nginx_trust_bundle(pki_dir: Path) -> Path:
    """nginx 客户端证书信任包 = users + nodes（control 域不进 443）。"""
    target = Path(pki_dir) / "nginx" / "ca-bundle.pem"
    target.parent.mkdir(parents=True, exist_ok=True)
    data = b"".join(ca_cert_pem(pki_dir, kind) for kind in ("users", "nodes"))
    tmp = target.with_suffix(".pem.tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o644)
    _align_owner(tmp)
    os.replace(tmp, target)
    return target
