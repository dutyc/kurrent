"""组件 PKI：内部 CA + bootstrap token 引导 + 证书签发。

设计：
- 控制面 = API 服务 + CA：幂等生成内部 CA（state/pki/ca.{key,crt}），
  为每个组件签发 client cert（身份，CN=<component>-<id>）与 serving cert（TLS 服务端）
- bootstrap token = 集群级通用引导凭据（<6位>.<16位>，
  登记只存 sha256 hash）；TTL 内可被多次 enroll 复用（不限制
  使用次数）——agent 通用 token 不绑节点（节点名由节点自决 + enroll 自动登记）；
  nvmet-host 凭据按 agent 能力上报派生（backend=nvmet 时随 enroll 响应下发，绑节点）
- 组件证书 TTL 90 天（CA 10 年）：轮换 = 组件在证书剩余 <20% 时重新提交 CSR
  （/renew，经 nginx mTLS 校验证书身份），控制面自动签发
- 吊销 = 证书到期自然失效 + 组件从注册表移除后拒绝 renew（不引入 CRL，MVP 够用）。
  注意后半个条件**只对非 agent 组件成立**：agent 组件在 /enroll/renew 上走自动登记
  （routers/enroll.py::_issue），移除台账条目不会让它失效，只会在下次轮换时重新登记。
- 管理面凭据同 CA 体系：admin client cert（CN=admin, O=admin）+
  管理凭据文件 admin.conf（kurrent 原生扁平 YAML，CLI 自动发现）；
  管理 token 与 bootstrap token 同台账（usage=admin 区分，认证器链 Bearer 校验）

CSR 校验规则（防冒名）：subject CN 必须与 token 绑定的组件身份一致（nvmet-host
派生 token 按 agent_id 匹配；agent 通用 token 由 CSR 自决 + 自动登记）；
签名有效；公钥 RSA >= 2048。
"""

import datetime as _dt
import hashlib
import hmac
import logging
import os
import secrets
import threading
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from . import config

log = logging.getLogger("control-plane")

CA_FILENAME = "ca.crt"
CA_KEY_FILENAME = "ca.key"
TOKENS_FILENAME = "bootstrap-tokens.yml"
# 台账写锁：同步路由跑线程池，签发与吊销交错会把已吊销条目写回（读路径不加锁，
# os.replace 原子替换，读到旧或新都自洽）
_TOKENS_LOCK = threading.RLock()
CA_DAYS = 3650
CA_CN = "kurrent-ca"
# 组件身份前缀：client cert 的 CN 约定 <prefix>-<id>，enroll/renew 时按组件类型校验
COMPONENT_PREFIX = {"agent": "agent", "nvmet-host": "nvmet", "control-plane": "control-plane"}


# ============================ CA ============================

def _load_key(path: Path):
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def _load_cert(path: Path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def ensure_ca(pki_dir: Path) -> tuple:
    """幂等生成内部 CA。返回 (cert, key)；已存在则加载（轮换 = 删 state/pki/ 重启）。"""
    pki_dir.mkdir(parents=True, exist_ok=True)
    cert_path = pki_dir / CA_FILENAME
    key_path = pki_dir / CA_KEY_FILENAME
    if cert_path.exists() and key_path.exists():
        return _load_cert(cert_path), _load_key(key_path)

    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_CN)])
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
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("pki: generated internal CA (cn=%s, %d days)", CA_CN, CA_DAYS)
    return cert, key


def ca_cert_pem(pki_dir: Path) -> bytes:
    return (pki_dir / CA_FILENAME).read_bytes()


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
            if _load_cert(cert_path).not_valid_after_utc > _dt.datetime.now(_dt.timezone.utc):
                return cert_path, key_path
            log.warning("pki: control-plane client cert expired, rotating")
        except ValueError:
            pass
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr_pem = _build_csr(key, component)
    cert = _sign_csr(ca_cert, ca_key, csr_pem, "clientAuth", [], config.CONFIG.spec.pki.component_cert_days)
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    os.chmod(key_path, 0o600)
    cert_path.write_bytes(cert)
    log.info("pki: control-plane client cert ready: %s", cert_path)
    return cert_path, key_path


# ============================ 管理面凭据（admin.conf）============================
# 管理凭据并入组件 PKI 同一 CA（同 CA 既签组件证书也签 admin.conf）：
# admin client cert（CN=admin/O=admin）→ pki/users/admin/；管理凭据文件 admin.conf
# （kurrent 原生扁平 YAML：server + 三个证书路径键；证书路径为相对 admin.conf 目录的引用，
# 宿主/容器视角一致）。CLI 自动发现该文件走 443 mTLS；轮换 = 证书过期后重启
# （自动重签并刷新 admin.conf，保留管理员手改的 server）；历史 kubeconfig 结构启动时
# 自动迁移为扁平格式（原 server 保留）。

def ensure_admin_credentials(pki_dir: Path, ca_cert, ca_key, admin_conf_path: Path,
                             cn: str | None = None, days: int | None = None) -> tuple[Path, Path]:
    """幂等签发管理端 client cert（CN=cn, O=admin）并确保管理凭据文件 admin.conf。"""
    cn = cn or config.ADMIN_CN
    days = days or config.ADMIN_CERT_DAYS
    cert_dir = pki_dir / "users" / cn
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "client.crt"
    key_path = cert_dir / "client.key"
    fresh = False
    if cert_path.exists() and key_path.exists():
        try:
            fresh = _load_cert(cert_path).not_valid_after_utc > _dt.datetime.now(_dt.timezone.utc)
        except ValueError:
            fresh = False
    if not fresh:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        csr_pem = _build_csr(key, cn, orgs=[config.ADMIN_GROUP])
        cert = _sign_csr(ca_cert, ca_key, csr_pem, "clientAuth", [], days)
        key_path.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        os.chmod(key_path, 0o600)
        cert_path.write_bytes(cert)
        log.info("pki: admin client cert issued (cn=%s, %d days)", cn, days)
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


def _chown_like(path: Path, ref: Path) -> None:
    """把文件属主对齐参考对象（挂载卷目录）；非 root 或属主相同则跳过。"""
    try:
        st = ref.stat()
        if os.stat(path).st_uid == st.st_uid and os.stat(path).st_gid == st.st_gid:
            return
        os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass


def _write_admin_conf(admin_conf_path: Path, cert_path: Path, key_path: Path,
                      server: str = "") -> None:
    """渲染管理凭据文件 admin.conf（kurrent 原生扁平 YAML，四键；0600）。

    两条信任链独立：客户端身份 = 内部 CA 签发的 admin 证书（nginx 校链）；
    服务端信任锚 = TOFU 自签 server.crt（443 nginx 证书，与引导链同一锚）。
    证书路径为相对本文件所在目录的引用（宿主/容器视角一致）。
    server 缺省 https://127.0.0.1/api/cp；旧格式迁移时传入原文件中的地址以保留手改。
    """
    def rel(p: Path) -> str:
        return os.path.relpath(p, admin_conf_path.parent)

    server_cert = Path(config.settings.cert_dir) / "server.crt"
    doc = {
        # nginx 唯一入口的 API 根（/api/cp 前缀反代控制面）；远程管理员改写为
        # https://<host>/api/cp（或 CLI 用 --server 覆盖）
        "server": server or "https://127.0.0.1/api/cp",
        "certificate-authority": rel(server_cert),
        "client-certificate": rel(cert_path),
        "client-key": rel(key_path),
    }
    admin_conf_path.parent.mkdir(parents=True, exist_ok=True)
    header = "# kurrent 管理凭据（控制面自举生成；server 可改写为对外入口，证书路径相对本文件）\n"
    admin_conf_path.write_text(header + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                               encoding="utf-8")
    os.chmod(admin_conf_path, 0o600)
    log.info("pki: admin.conf written: %s", admin_conf_path)


def _legacy_admin_server(admin_conf_path: Path) -> str | None:
    """检测历史 kubeconfig 结构 admin.conf：是则返回其中的 server（提取失败为空串，
    调用方以默认值兜底）；已是扁平格式（含顶层 server 键）或文件缺失则返回 None。"""
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
    log.info("pki: session signing key generated: %s", path)
    return key


# ============================ CSR 校验 + 签发 ============================

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
              sans: list[str] | None, days: int) -> bytes:
    """校验 CSR 并签发。usage = clientAuth | serverAuth。SAN 仅 serverAuth 使用。"""
    csr = _parse_csr(csr_pem)
    now = _dt.datetime.now(_dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
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


def sign_component_certs(ca_cert, ca_key, agent_id: str, component: str,
                         client_csr: bytes, serving_csr: bytes,
                         serving_sans: list[str]) -> dict[str, bytes]:
    """为组件签发 client + serving 两张证书（agent 代 nvmet-host 的场景同入口）。

    CSR 的 CN 必须与登记身份一致（防冒名）：CN = <prefix>-<agent_id>。
    """
    prefix = COMPONENT_PREFIX.get(component)
    if not prefix:
        raise ValueError(f"unknown component: {component}")
    expected_cn = f"{prefix}-{agent_id}"
    for csr in (client_csr, serving_csr):
        if _csr_cn(_parse_csr(csr)) != expected_cn:
            raise ValueError(f"csr cn mismatch: expect {expected_cn}")
    client_cert = _sign_csr(ca_cert, ca_key, client_csr, "clientAuth", [], config.CONFIG.spec.pki.component_cert_days)
    serving_cert = _sign_csr(ca_cert, ca_key, serving_csr, "serverAuth", serving_sans,
                             config.CONFIG.spec.pki.component_cert_days)
    return {"client.crt": client_cert, "serving.crt": serving_cert}


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


# ============================ bootstrap token ============================
# 集群级通用引导凭据，TTL 内可被多次 enroll 复用
# （不一次性消耗）。台账 key = token_id（每 token 独立，多 token 并存，写入时
# 惰性清理过期条目）：
# - agent 通用 token（不绑节点）：任何存储节点可用它
#   引导（enroll 自动登记，节点名由节点自决）
# - nvmet-host 派生 token（绑 agent_id）：agent enroll 上报 backend=nvmet 时控制面
#   自动签发随响应下发（能力上报驱动，签发不预知后端）

def _token_file(pki_dir: Path) -> Path:
    return pki_dir / TOKENS_FILENAME


def _load_tokens(pki_dir: Path) -> dict:
    try:
        data = yaml.safe_load(_token_file(pki_dir).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_tokens(pki_dir: Path, tokens: dict) -> None:
    """落盘 token 台账（写入前惰性清理过期条目）。"""
    now = _dt.datetime.now(_dt.timezone.utc)
    tokens = {
        tid: entry for tid, entry in tokens.items()
        if _entry_active(entry.get("expires_at"), now)
    }
    pki_dir.mkdir(parents=True, exist_ok=True)
    tmp = _token_file(pki_dir).with_suffix(".yml.tmp")
    tmp.write_text(yaml.safe_dump(tokens, allow_unicode=True, sort_keys=True), encoding="utf-8")
    os.replace(tmp, _token_file(pki_dir))
    os.chmod(_token_file(pki_dir), 0o600)


def _entry_active(expires_at: str, now: _dt.datetime) -> bool:
    try:
        return _dt.datetime.fromisoformat(expires_at) > now
    except (KeyError, TypeError, ValueError):
        return False


def issue_bootstrap_token(pki_dir: Path, agent_id: str = "", component: str = "agent",
                          days: int | None = None, usage: list[str] | None = None,
                          groups: list[str] | None = None, name: str = "") -> str:
    """签发 token，返回明文（登记只存 secret 的 sha256；key = token_id）。

    无幂等沿用（每次新签；明文不可恢复，旧 token TTL 内仍有效）；
    agent 通用 token 不绑节点（agent_id=""），nvmet-host 派生 token 绑 agent_id；
    管理 token（usage=["admin"] + groups）供认证器链 Bearer 与 WebUI 登录使用，
    不绑组件（component=""）。
    """
    days = days or config.CONFIG.spec.pki.bootstrap_token_ttl_days
    token_id = secrets.token_hex(3)
    token_secret = secrets.token_hex(8)
    now = _dt.datetime.now(_dt.timezone.utc)
    entry = {
        "component": component,
        "agent_id": agent_id or "",
        # 明文仅在生成时可见：登记只存 secret 部分 hash（token_id 为台账 key，非机密）
        "token_secret_hash": hashlib.sha256(token_secret.encode()).hexdigest(),
        "created_at": now.isoformat(),
        "expires_at": (now + _dt.timedelta(days=days)).isoformat(),
        "usage": usage or ["enroll"],
        "groups": groups or [],
    }
    if name:
        entry["name"] = name
    with _TOKENS_LOCK:
        tokens = _load_tokens(pki_dir)
        tokens[token_id] = entry
        _save_tokens(pki_dir, tokens)
    log.info("pki: token issued: usage=%s component=%s agent=%s", entry["usage"], component or "-", agent_id or "-")
    return f"{token_id}.{token_secret}"


def revoke_agent_tokens(pki_dir: Path, agent_id: str, component: str = "nvmet-host") -> None:
    """删除某 agent 组件的全部登记条目（派生 token 重建前调用；agent 通用 token 不动）。"""
    with _TOKENS_LOCK:
        tokens = _load_tokens(pki_dir)
        drop = [tid for tid, e in tokens.items()
                if e.get("component") == component and e.get("agent_id") == agent_id]
        for tid in drop:
            tokens.pop(tid)
        if drop:
            _save_tokens(pki_dir, tokens)
    if drop:
        log.info("pki: revoked %d %s token(s) for agent %s", len(drop), component, agent_id)


def get_bootstrap_token(pki_dir: Path, token_id: str) -> dict | None:
    """只读返回 token 登记条目（剔除 secret hash）。"""
    entry = _load_tokens(pki_dir).get(token_id)
    if not entry:
        return None
    return {k: v for k, v in entry.items() if k != "token_secret_hash"}


def list_bootstrap_tokens(pki_dir: Path) -> list[dict]:
    """列出未过期 token 条目（台账可视化管理用；不返回 secret hash——明文本就不可恢复）。

    只读不落盘（不做过期清理，清理发生在下次保存）；按 expires_at 升序（先到期的靠前）。
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    items: list[dict] = []
    for token_id, entry in _load_tokens(pki_dir).items():
        if not isinstance(entry, dict) or not _entry_active(entry.get("expires_at"), now):
            continue
        items.append({
            "token_id": token_id,
            "usage": list(entry.get("usage") or []),
            "groups": list(entry.get("groups") or []),
            "component": str(entry.get("component") or ""),
            "agent_id": str(entry.get("agent_id") or ""),
            "name": str(entry.get("name") or ""),
            "created_at": str(entry.get("created_at") or ""),  # 旧条目无此字段（签发时才注入）
            "expires_at": str(entry.get("expires_at") or ""),
        })
    items.sort(key=lambda e: e["expires_at"])
    return items


def revoke_bootstrap_token(pki_dir: Path, token_id: str) -> bool:
    """吊销（删除）一个 token 条目（无 tombstone），返回是否存在。

    删除即失效：「谁在何时吊销了哪个 token」由操作审计留痕；token 不存在返回 False。
    """
    with _TOKENS_LOCK:
        tokens = _load_tokens(pki_dir)
        if token_id not in tokens:
            return False
        tokens.pop(token_id)
        _save_tokens(pki_dir, tokens)
    log.info("pki: token revoked: %s", token_id)
    return True


def validate_bootstrap_token(pki_dir: Path, token: str, agent_id: str, component: str) -> None:
    """校验 bootstrap token（enroll 用）：格式、登记存在、组件匹配、绑定匹配、未过期。

    TTL 内可复用——不消耗（不限制使用次数）。
    agent 通用 token（无绑定）任何节点可用；nvmet-host 派生 token 须 agent_id 匹配
    （防串用）。失败抛 ValueError（调用方转 401）。
    """
    token = token.strip()
    token_id, _, secret = token.partition(".")
    if not token_id or not secret:
        raise ValueError("malformed bootstrap token")
    entry = _load_tokens(pki_dir).get(token_id)
    if not entry:
        raise ValueError(f"no bootstrap token for token id {token_id}")
    if entry.get("component") != component:
        raise ValueError("bootstrap token component mismatch")
    if entry.get("agent_id") and entry.get("agent_id") != agent_id:
        raise ValueError("bootstrap token agent mismatch")
    if not hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(),
                               entry.get("token_secret_hash", "")):
        raise ValueError("bootstrap token invalid")
    if not _entry_active(entry.get("expires_at"), _dt.datetime.now(_dt.timezone.utc)):
        raise ValueError("bootstrap token expired")
    log.info("pki: bootstrap token validated: component=%s agent=%s", component, agent_id)


def validate_admin_token(pki_dir: Path, token: str) -> dict:
    """校验管理 token（认证器链 Bearer 用）：格式、登记、usage 含 admin、hash、TTL。

    TTL 内可复用（不消耗）；失败抛 ValueError（调用方转 401）。返回台账条目（含 groups）。
    """
    token = token.strip()
    token_id, _, secret = token.partition(".")
    if not token_id or not secret:
        raise ValueError("malformed token")
    entry = _load_tokens(pki_dir).get(token_id)
    if not entry:
        raise ValueError(f"no token for token id {token_id}")
    if "admin" not in (entry.get("usage") or []):
        raise ValueError("token is not an admin token")
    if not hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(),
                               entry.get("token_secret_hash", "")):
        raise ValueError("token invalid")
    if not _entry_active(entry.get("expires_at"), _dt.datetime.now(_dt.timezone.utc)):
        raise ValueError("token expired")
    return entry
