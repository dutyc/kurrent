"""TOFU 引导链服务器证书：幂等生成、声明漂移检测与显式轮换。

证书生命周期归控制面（2026-08-22 裁定：自动生成，gen-cert.sh 废止）：
- 首次启动生成 RSA-2048 自签叶证书（CA=False），指纹写入 fingerprint.txt
  （DER SHA-256 hex，与 openssl x509 -outform DER | sha256sum 一致）；
- 证书旁的 metadata.json 记录生成时的 san/days/指纹：再次启动与声明比对，
  SAN 或天数变化即判定漂移；
- 漂移处理：rotateOnChange=false（默认）保留原证书、日志与 /config 状态报告漂移；
  rotateOnChange=true 重签并替换（指纹变化，已通过 TOFU 信任旧指纹的设备需要
  重新信任），调用方负责 reload nginx；
- 证书/私钥损坏或缺失时无条件重建（HTTPS 已不可用，无保留意义）。
"""

import datetime as _dt
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .config_schema import parse_san_entries

log = logging.getLogger("control-plane")

# 与 nginx 443 配置（/etc/nginx/certs/server.crt|key）一致
CERT_FILENAME = "server.crt"
KEY_FILENAME = "server.key"
FINGERPRINT_FILENAME = "fingerprint.txt"
METADATA_FILENAME = "metadata.json"
DEFAULT_CN = "kurrent-controller"


def parse_san(san_spec: str) -> list[x509.GeneralName]:
    """声明 SAN → x509 GeneralName 列表（声明已在 schema 层校验，此处只做转换）。"""
    names: list[x509.GeneralName] = []
    for kind, value in parse_san_entries(san_spec):
        if kind == "IP":
            names.append(x509.IPAddress(ipaddress.ip_address(value)))
        else:
            names.append(x509.DNSName(value))
    return names


def _cert_san_entries(cert: x509.Certificate) -> list[tuple[str, str]]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    entries = [("IP", str(ip)) for ip in san.get_values_for_type(x509.IPAddress)]
    entries += [("DNS", name) for name in san.get_values_for_type(x509.DNSName)]
    return sorted(entries)


def _fingerprint(cert: x509.Certificate) -> str:
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def read_metadata(cert_dir: Path) -> dict:
    try:
        data = json.loads((Path(cert_dir) / METADATA_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_metadata(cert_dir: Path, san_spec: str, days: int,
                    fingerprint: str, issued_at: str) -> None:
    path = Path(cert_dir) / METADATA_FILENAME
    payload = {"san": san_spec, "days": int(days),
               "fingerprint": fingerprint, "issued_at": issued_at}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load_existing(cert_dir: Path) -> tuple[x509.Certificate | None, str]:
    """读取现有证书并校验私钥配对；不可用时返回 (None, 原因)。"""
    cert_path = Path(cert_dir) / CERT_FILENAME
    key_path = Path(cert_dir) / KEY_FILENAME
    if not cert_path.is_file() or not key_path.is_file():
        return None, "missing"
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (ValueError, TypeError):
        return None, "corrupt"
    cert_pub = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    key_pub = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if cert_pub != key_pub:
        return None, "key_mismatch"
    return cert, ""


def inspect_server_cert(cert_dir: Path, san_spec: str, days: int) -> dict:
    """只读检查证书与声明的差异（不生成/不覆盖），供 /config 状态与 ensure 复用。"""
    metadata = read_metadata(cert_dir)
    cert, problem = _load_existing(cert_dir)
    if cert is None:
        return {
            "present": False,
            "problem": problem,
            "fingerprint": None,
            "san": [],
            "days": metadata.get("days"),
            "notAfter": None,
            "drift": ["missing"],
        }
    drift: list[str] = []
    desired = sorted(parse_san_entries(san_spec))
    actual = _cert_san_entries(cert)
    if desired != actual:
        drift.append("san")
    if metadata.get("days") is not None and int(metadata["days"]) != int(days):
        drift.append("days")
    if cert.not_valid_after_utc <= _dt.datetime.now(_dt.timezone.utc):
        drift.append("expired")
    return {
        "present": True,
        "problem": "",
        "fingerprint": _fingerprint(cert),
        "san": [f"{kind}:{value}" for kind, value in actual],
        "days": metadata.get("days"),
        "notAfter": cert.not_valid_after_utc.isoformat(),
        "drift": drift,
    }


def _generate(cert_dir: Path, san_spec: str, days: int) -> str:
    cert_dir = Path(cert_dir)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, DEFAULT_CN)])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=int(days)))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(parse_san(san_spec)), critical=False)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    # 私钥 0600：仅属主可读写（与 openssl genrsa 权限一致）
    key_path = cert_dir / KEY_FILENAME
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)
    cert_path = cert_dir / CERT_FILENAME
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    fingerprint = _fingerprint(cert)
    (cert_dir / FINGERPRINT_FILENAME).write_text(f"{fingerprint}  {CERT_FILENAME}\n")
    _write_metadata(cert_dir, san_spec, days, fingerprint,
                    now.isoformat())
    return fingerprint


def ensure_server_cert(
    cert_dir: Path,
    san_spec: str = "IP:127.0.0.1,DNS:localhost",
    days: int = 3650,
    rotate_on_change: bool = False,
) -> dict:
    """确保证书存在且与声明一致。

    返回 {generated, rotated, fingerprint, drift}：generated = 首次/损坏后生成；
    rotated = 检测到声明漂移且 rotateOnChange=true 后重签；drift 为检测到的漂移项
    （未轮换时非空，供调用方告警与状态上报）。
    """
    cert_dir = Path(cert_dir)
    cert_dir.mkdir(parents=True, exist_ok=True)
    status = inspect_server_cert(cert_dir, san_spec, days)
    if not status["present"]:
        fingerprint = _generate(cert_dir, san_spec, days)
        log.info("cert: generated self-signed server certificate, fingerprint=%s", fingerprint)
        return {"generated": True, "rotated": False, "fingerprint": fingerprint,
                "drift": status["drift"]}

    # 升级前生成的证书没有 metadata：以当前声明补齐基线（SAN 仍按证书实际比对）
    if not read_metadata(cert_dir):
        _write_metadata(cert_dir, san_spec, days, status["fingerprint"],
                        _dt.datetime.now(_dt.timezone.utc).isoformat())
        status["drift"] = [item for item in status["drift"] if item != "days"]

    drift = status["drift"]
    if drift and rotate_on_change:
        fingerprint = _generate(cert_dir, san_spec, days)
        log.warning("cert: rotated server certificate on declaration change (%s), "
                    "fingerprint=%s — devices pinned to the old TOFU fingerprint must re-trust",
                    ", ".join(drift), fingerprint)
        return {"generated": False, "rotated": True, "fingerprint": fingerprint,
                "drift": drift}
    if drift:
        log.warning(
            "cert: server certificate does not match spec.serverCert (%s); kept existing "
            "certificate. Set spec.serverCert.rotateOnChange=true (TOFU fingerprint changes) "
            "or delete %s and restart to rotate.", ", ".join(drift), cert_dir)
    return {"generated": False, "rotated": False,
            "fingerprint": status["fingerprint"], "drift": drift}
