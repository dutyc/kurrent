"""测试 PKI 工具箱：生成自签 CA + client 证书（单测用占位证书 / SSLContext 构造）。

用途：
1. conftest 预生成 client.crt（长有效期）→ ensure_pki() 走 ready 分支跳过引导
   （单测不连控制面，引导/轮换/证书落盘由部署集成测试覆盖）；
2. NvmetHostClient 等 mTLS 客户端的 SSLContext 构造需要合法 PEM 文件。
"""

import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def gen_pki_dir(target: Path) -> Path:
    """在 target 下生成 ca.crt / ca-control.crt / ca-nodes.crt + client.crt/key。"""
    target.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kurrent-test-ca")])
    now = datetime.datetime.now(datetime.timezone.utc)
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
          .public_key(key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=365))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .sign(key, hashes.SHA256()))
    cli = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
           .public_key(key.public_key()).serial_number(x509.random_serial_number())
           .not_valid_before(now - datetime.timedelta(days=1))
           .not_valid_after(now + datetime.timedelta(days=365))
           .sign(key, hashes.SHA256()))
    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    (target / "ca.crt").write_bytes(ca_pem)
    # 三分根后节点侧的两个信任锚（单测占位同一张 CA，只要求文件存在且可建 SSLContext）
    (target / "ca-control.crt").write_bytes(ca_pem)
    (target / "ca-nodes.crt").write_bytes(ca_pem)
    (target / "client.crt").write_bytes(cli.public_bytes(serialization.Encoding.PEM))
    (target / "client.key").write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()))
    return target
