"""pki 子模块：issuance。"""

import logging

from ..core import config
from ..core.utils import AGENT_ID_RE
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pathlib import Path
import datetime as _dt
import os
import secrets
import yaml
from .common import CA_FILENAME, CA_KEY_FILENAME, COMPONENT_PREFIX, USER_CN_PATTERN
from .common import _align_owner, _chown_like, _issuer_cn, _load_cert, _load_key, _subject_cn, ca_dir
from .registries import register_component_cert, register_control_cert, register_user_cert
from .signing import _build_csr, _csr_cn, _parse_csr, _sign_csr

log = logging.getLogger("control-plane")

def issue_user_cert(pki_dir: Path, cn: str, csr_pem: bytes, days: int | None = None) -> dict:
    """按客户端提供的 CSR 签发人类证书（users CA，clientAuth，subject 仅 CN）。

    私钥由申请者在本地生成、永不上送；这里只签 CSR。CSR 的 CN 必须等于请求路径里的
    CN，且除 CN 外不得携带任何 subject 属性（组只来自 users.yml）。签出的证书文件
    落盘到 pki/users/<cn>/issued/<serial>.crt 供审计与列表。
    """
    cn = cn.strip()
    if not USER_CN_PATTERN.fullmatch(cn):
        raise ValueError("invalid cn: use letters, digits, dot, underscore, dash, @ (max 64)")
    days = int(days if days is not None else config.settings.admin_cert_days)
    if days < 1 or days > config.settings.user_cert_days_max:
        raise ValueError(f"days must be between 1 and {config.settings.user_cert_days_max}")
    try:
        parsed = _parse_csr(csr_pem)
    except ValueError as exc:
        raise ValueError(f"invalid csr: {exc}") from exc
    extra = [attr.oid._name for attr in parsed.subject if attr.oid != NameOID.COMMON_NAME]
    if extra:
        raise ValueError(f"csr subject attribute not allowed: {', '.join(extra)}")
    if _csr_cn(parsed) != cn:
        raise ValueError(f"csr cn mismatch: expect {cn}")
    ca_cert = _load_cert(ca_dir(pki_dir, "users") / CA_FILENAME)
    ca_key = _load_key(ca_dir(pki_dir, "users") / CA_KEY_FILENAME)
    cert_pem = _sign_csr(ca_cert, ca_key, csr_pem, "clientAuth", [], days, subject_cn=cn)
    cert = x509.load_pem_x509_certificate(cert_pem)
    serial = format(cert.serial_number, "x")
    cert_dir = Path(pki_dir) / "users" / cn / "issued"
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / f"{serial}.crt"
    cert_path.write_bytes(cert_pem)
    os.chmod(cert_path, 0o644)
    _align_owner(cert_path)
    register_user_cert(pki_dir, cert_path)
    from ..auth import audit
    audit.alarm("user_cert_issued", detail=f"cn={cn}, serial={serial}, days={days}",
                route="pki", principal=cn)
    return {
        "cn": cn,
        "serial": serial,
        "fingerprint": cert.fingerprint(hashes.SHA256()).hex(),
        "created_at": cert.not_valid_before_utc.isoformat(),
        "expires_at": cert.not_valid_after_utc.isoformat(),
        "certificate": cert_pem.decode(),
    }


def sign_component_certs(ca_cert, ca_key, agent_id: str, component: str,
                         client_csr: bytes, serving_csr: bytes,
                         serving_sans: list[str],
                         pki_dir: Path | None = None) -> dict[str, bytes]:
    """为组件签发 client + serving 两张证书（agent 代 nvmet-host 的场景同入口）。

    CSR 的 CN 必须与登记身份一致（防冒名）：CN = <prefix>-<agent_id>。
    给出 pki_dir 时把两张证书登记进组件登记表（吊销/CRL 的依据）。
    """
    if not AGENT_ID_RE.fullmatch(agent_id):
        raise ValueError("agent_id must be 12 lowercase hex characters")
    prefix = COMPONENT_PREFIX.get(component)
    if not prefix:
        raise ValueError(f"unknown component: {component}")
    expected_cn = f"{prefix}-{agent_id}"
    for csr in (client_csr, serving_csr):
        parsed = _parse_csr(csr)
        extra = [attr.oid._name for attr in parsed.subject if attr.oid != NameOID.COMMON_NAME]
        if extra:
            raise ValueError(f"csr subject attribute not allowed: {', '.join(extra)}")
        if _csr_cn(parsed) != expected_cn:
            raise ValueError(f"csr cn mismatch: expect {expected_cn}")
    client_cert = _sign_csr(ca_cert, ca_key, client_csr, "clientAuth", [],
                            config.CONFIG.spec.pki.component_cert_days, subject_cn=expected_cn)
    serving_cert = _sign_csr(ca_cert, ca_key, serving_csr, "serverAuth", serving_sans,
                             config.CONFIG.spec.pki.component_cert_days, subject_cn=expected_cn)
    certs = {"client.crt": client_cert, "serving.crt": serving_cert}
    if pki_dir is not None:
        for name, pem in certs.items():
            register_component_cert(pki_dir, pem, agent_id, name.removesuffix(".crt"))
    return certs


def ensure_control_plane_client_cert(pki_dir: Path, ca_cert, ca_key,
                                     component: str = "control-plane") -> tuple[Path, Path]:
    """幂等生成控制面自身 client cert（cp→agent 的 mTLS 客户端身份，CN=component）。

    存在且未过期即复用；过期则轮换（证书文件名固定，重启后以文件为准）。
    返回 (cert_path, key_path)。
    """
    cert_dir = pki_dir / "components" / component
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "client.crt"
    key_path = cert_dir / "client.key"
    if cert_path.exists() and key_path.exists():
        try:
            cert = _load_cert(cert_path)
            if (cert.not_valid_after_utc > _dt.datetime.now(_dt.timezone.utc)
                    and _issuer_cn(cert) == _subject_cn(ca_cert)):
                register_control_cert(pki_dir, cert_path.read_bytes())
                return cert_path, key_path
            log.warning("pki: control-plane client cert expired or issued by another CA, rotating")
        except ValueError:
            pass
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr_pem = _build_csr(key, component)
    cert = _sign_csr(ca_cert, ca_key, csr_pem, "clientAuth", [],
                     config.CONFIG.spec.pki.component_cert_days, subject_cn=component)
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    os.chmod(key_path, 0o600)
    _align_owner(key_path)
    cert_path.write_bytes(cert)
    _align_owner(cert_path)
    register_control_cert(pki_dir, cert)
    log.info("pki: control-plane client cert ready: %s", cert_path)
    return cert_path, key_path


def ensure_admin_credentials(pki_dir: Path, ca_cert, ca_key, admin_conf_path: Path,
                             cn: str | None = None, days: int | None = None) -> tuple[Path, Path]:
    """幂等签发管理端 client cert（subject 仅 CN=cn）并确保管理凭据文件 admin.conf。"""
    cn = cn or config.ADMIN_CN
    days = days or config.settings.admin_cert_days
    cert_dir = pki_dir / "users" / cn
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "client.crt"
    key_path = cert_dir / "client.key"
    fresh = False
    if cert_path.exists() and key_path.exists():
        try:
            cert = _load_cert(cert_path)
            fresh = (cert.not_valid_after_utc > _dt.datetime.now(_dt.timezone.utc)
                     and _issuer_cn(cert) == _subject_cn(ca_cert))
        except ValueError:
            fresh = False
    if not fresh:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        csr_pem = _build_csr(key, cn)
        cert = _sign_csr(ca_cert, ca_key, csr_pem, "clientAuth", [], days, subject_cn=cn)
        key_path.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        os.chmod(key_path, 0o600)
        _align_owner(key_path)
        cert_path.write_bytes(cert)
        _align_owner(cert_path)
        log.info("pki: admin client cert issued (cn=%s, %d days)", cn, days)
        from ..auth import audit
        audit.alarm("admin_cert_issued", detail=f"cn={cn}, days={days}", route="pki",
                    principal=cn)
    register_user_cert(pki_dir, cert_path)
    # 渲染时机：首次签发 / 证书轮换 / 文件缺失；历史 kubeconfig 结构检测到即迁移
    # （提取原 server 保留手改后重写为扁平格式）。扁平格式且证书新鲜 → 原样保留。
    legacy_server = _legacy_admin_server(admin_conf_path)
    if not fresh or not admin_conf_path.exists() or legacy_server is not None:
        if legacy_server is not None:
            log.info("pki: admin.conf legacy format detected, migrating (server=%s)",
                     legacy_server or "<default>")
        _write_admin_conf(admin_conf_path, cert_path, key_path, server=legacy_server or "")
    # 属主对齐挂载卷属主（state/ 目录）：容器 root 写卷时凭据归宿主用户可读，
    # CLI 开箱即用（免手工 chown / 拷贝的体验简化）
    for path in (cert_path, key_path, admin_conf_path):
        _chown_like(path, admin_conf_path.parent)
    return cert_path, key_path


def _write_admin_conf(admin_conf_path: Path, cert_path: Path, key_path: Path,
                      server: str = "") -> None:
    """渲染管理凭据文件 admin.conf（kurrent 原生扁平 YAML，四键；0600）。

    两条信任链独立：客户端身份 = users CA 签发的 admin 证书（nginx 校链）；
    服务端信任锚 = TOFU 自签 server.crt（443 nginx 证书，与引导链同一锚）。
    证书路径为相对本文件所在目录的引用（宿主/容器视角一致）。
    server 缺省 https://127.0.0.1/api/cp（宿主 HTTPS 端口非 443 时带上端口）；
    旧格式迁移时传入原文件中的地址以保留手改。
    """
    def rel(p: Path) -> str:
        return os.path.relpath(p, admin_conf_path.parent)

    def default_server() -> str:
        port = os.environ.get("KURRENT_HTTPS_PORT", "").strip()
        if port and port.isdigit() and port != "443":
            return f"https://127.0.0.1:{port}/api/cp"
        return "https://127.0.0.1/api/cp"

    server_cert = Path(config.settings.cert_dir) / "server.crt"
    doc = {
        # nginx 唯一入口的 API 根（/api/cp 前缀反代控制面）；远程管理员改写为
        # https://<host>/api/cp（或 CLI 用 --server 覆盖）
        "server": server or default_server(),
        "certificate-authority": rel(server_cert),
        "client-certificate": rel(cert_path),
        "client-key": rel(key_path),
    }
    admin_conf_path.parent.mkdir(parents=True, exist_ok=True)
    header = "# kurrent management credentials (generated by control-plane bootstrap; server may be rewritten to the external entry; certificate paths are relative to this file)\n"
    admin_conf_path.write_text(header + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                               encoding="utf-8")
    os.chmod(admin_conf_path, 0o600)
    log.info("pki: admin.conf written: %s", admin_conf_path)


def _legacy_admin_server(admin_conf_path: Path) -> str | None:
    """检测历史 kubeconfig 结构 admin.conf：是则返回其中的 server（提取失败为空串，
    调用方以默认值回退）；已是扁平格式（含顶层 server 键）或文件缺失则返回 None。"""
    try:
        doc = yaml.safe_load(admin_conf_path.read_text(encoding="utf-8")) or {}
    except OSError:
        return None
    except yaml.YAMLError:
        return ""  # 内容损坏：按旧格式处理（以默认 server 重写修复）
    if not isinstance(doc, dict):
        return ""
    if "server" in doc:
        return None
    try:
        return str(doc["clusters"][0]["cluster"]["server"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def ensure_session_key(path: Path) -> bytes:
    """幂等生成会话签名密钥（32B，0600；轮换 = 删文件重启）。"""
    if path.exists():
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    os.chmod(path, 0o600)
    _align_owner(path)
    log.info("pki: session signing key generated: %s", path)
    return key
