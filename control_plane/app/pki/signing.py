"""pki 子模块：signing。"""

import logging

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import datetime as _dt

log = logging.getLogger("control-plane")

def _build_csr(key, cn: str, sans: list[str] | None = None, orgs: list[str] | None = None) -> bytes:
    """构建 CSR（agent 侧同逻辑，控制面仅测试/自签用）。orgs = O 字段（组，管理用户用）。"""
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, cn)]
    for org in orgs or []:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, org))
    name = x509.Name(attrs)
    builder = x509.CertificateSigningRequestBuilder().subject_name(name)
    if sans:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(s) if ":" not in s else _ip_or_dns(s) for s in sans]
            ),
            critical=False,
        )
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)


def _ip_or_dns(value: str):
    import ipaddress
    try:
        return x509.IPAddress(ipaddress.ip_address(value))
    except ValueError:
        return x509.DNSName(value)


def _parse_csr(csr_pem: bytes) -> x509.CertificateSigningRequest:
    csr = x509.load_pem_x509_csr(csr_pem)
    if not csr.is_signature_valid:
        raise ValueError("csr signature invalid")
    pub = csr.public_key()
    if isinstance(pub, rsa.RSAPublicKey) and pub.key_size < 2048:
        raise ValueError(f"csr rsa key too small: {pub.key_size}")
    return csr


def _csr_cn(csr: x509.CertificateSigningRequest) -> str:
    names = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return names[0].value if names else ""


def _sign_csr(ca_cert, ca_key, csr_pem: bytes, usage: str,
              sans: list[str] | None, days: int, *, subject_cn: str) -> bytes:
    """校验 CSR 并按签发 profile 重建 subject（只保留 CN）。

    CSR 的 subject 不参与签发：组件不能通过自报 O/OU 改变身份；组只来自 users.yml。
    usage = clientAuth | serverAuth；SAN 仅 serverAuth 使用。
    """
    if not subject_cn:
        raise ValueError("subject cn required")
    csr = _parse_csr(csr_pem)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)])
    now = _dt.datetime.now(_dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(csr.public_key())
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
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.CLIENT_AUTH if usage == "clientAuth" else ExtendedKeyUsageOID.SERVER_AUTH]
            ),
            critical=False,
        )
    )
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName([_ip_or_dns(s) for s in sans]), critical=False)
    cert = builder.sign(private_key=ca_key, algorithm=hashes.SHA256())
    return cert.public_bytes(serialization.Encoding.PEM)


def client_cert_cn(cert_pem: bytes) -> str:
    """从 PEM 客户端证书提取 CN（控制面校验 mTLS 客户端身份时用）。"""
    cert = x509.load_pem_x509_certificate(cert_pem)
    names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return names[0].value if names else ""


def parse_dn_cn(dn: str) -> str:
    """从 nginx 透传的客户端证书 DN（$ssl_client_s_dn，如 /C=CN/CN=agent-x）提取 CN。"""
    for part in dn.split("/"):
        key, _, value = part.partition("=")
        if key.strip() == "CN":
            return value.strip()
    return ""
