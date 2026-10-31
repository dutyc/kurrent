"""组件 PKI：三分根 CA + bootstrap token 引导 + 证书签发。

设计：
- 控制面 = API 服务 + CA，按凭据域拆三个独立根（state/pki/ca/<kind>/）：
    users   人类客户端证书（nginx 信任）
    nodes   agent / nvmet-host 证书（nginx renew、控制面访问节点、agent→nvmet-host）
    control 控制面自身 client cert（节点 TLS 层唯一受信客户端）
  nginx 只信任 users + nodes；control 证书只用于 cp→节点方向，结构性切断互换。
- 遗留单 CA（state/pki/ca.crt）在启动时原位提升为 nodes：存量组件证书继续有效，
  管理证书与控制面客户端证书按 issuer 检测后重签。
- 签发 profile 重建 subject：只保留 CN，CSR 自报的 O/OU 一律拒绝（组只来自 users.yml）。
- bootstrap token 必须绑定 component + agent_id；TTL 内可复用（一次性模式见蓝图 P3）。
- 组件证书 TTL 90 天（CA 10 年）：剩余 <20% 时经 /renew（nginx mTLS）重签。
- 管理面凭据：admin client cert 由 users CA 签发（CN=admin，无 O），组由 state/users.yml
  决定；管理 token 与 bootstrap token 同台账（usage=admin 区分）。
- users 域证书登记在 pki/users/issued.yml（按序列号）；吊销 = 标记 + 重签 CRL，
  nginx 以 ssl_crl 校链（clients.crl = users + nodes 两张 CRL 的拼接，nodes 为空表）。
  证书文件保留以便审计与列表，CRL 是唯一的有效性权威。
"""

import datetime as _dt
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
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
CA_ROOT_DIRNAME = "ca"
CA_KINDS = ("users", "nodes", "control")
CA_CN_PREFIX = "kurrent-ca-"
TOKENS_FILENAME = "bootstrap-tokens.yml"
# 台账写锁：同步路由跑线程池，签发与吊销交错会把已吊销条目写回（读路径不加锁，
# os.replace 原子替换，读到旧或新都自洽）
_TOKENS_LOCK = threading.RLock()
# last_used_at 节流：同一 token 每分钟最多回写一次（Bearer 每请求都会校验）
_TOKEN_TOUCH_INTERVAL_SECONDS = 60
_TOKEN_TOUCH_LOCK = threading.Lock()
_TOKEN_TOUCH: dict[str, float] = {}
# 证书登记/CRL 写锁：签发、吊销与列表同步交错时以锁串行，文件原子替换
_CRL_LOCK = threading.RLock()
CA_DAYS = 3650
# CRL：30 天有效期 / 签发时 7 天 next_update；吊销后立即重签并由控制面 reload nginx。
# 组件域（nodes/control）由节点侧定期拉取，next_update 放宽到 30 天：控制面短暂
# 不可达时节点仍可凭旧 CRL 互相校验，超过 30 天则 fail-closed。
CRL_DAYS = 30
CRL_NEXT_UPDATE_DAYS = 7
COMPONENT_CRL_NEXT_UPDATE_DAYS = 30
USERS_REGISTRY_FILENAME = "issued.yml"
# 人类证书 CN 兼作目录名：限制字符集，杜绝路径穿越
USER_CN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
# 允许的吊销原因（RFC 5280 子集）：默认 unspecified；key_compromise 用于私钥泄露
REVOCATION_REASONS = {
    "unspecified": x509.ReasonFlags.unspecified,
    "key_compromise": x509.ReasonFlags.key_compromise,
    "superseded": x509.ReasonFlags.superseded,
    "cessation_of_operation": x509.ReasonFlags.cessation_of_operation,
}
# 组件身份前缀：client cert 的 CN 约定 <prefix>-<id>，enroll/renew 时按组件类型校验
COMPONENT_PREFIX = {"agent": "agent", "nvmet-host": "nvmet", "control-plane": "control-plane"}


# ============================ CA ============================

def _load_key(path: Path):
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def _load_cert(path: Path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def _subject_cn(cert) -> str:
    names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return names[0].value if names else ""


def _issuer_cn(cert) -> str:
    names = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    return names[0].value if names else ""


def ca_dir(pki_dir: Path, kind: str) -> Path:
    if kind not in CA_KINDS:
        raise ValueError(f"unknown CA kind: {kind}")
    return Path(pki_dir) / CA_ROOT_DIRNAME / kind


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
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("pki: generated %s CA (cn=%s, %d days)", kind, cn, CA_DAYS)
    from . import audit
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
    os.replace(tmp, target)
    return target


# ============================ users 域证书登记与 CRL ============================
# 吊销 = 登记条目标记 revoked_at + 按 users CA 重签 CRL；证书文件保留（审计/列表），
# nginx 以 CRL 作为有效性权威。clients.crl 拼接 users 与 nodes 两张 CRL：nginx 的
# ssl_crl 对整条客户端链做 CRL 校验，缺 nodes CRL 会让组件证书一起验不过。

def _registry_path(pki_dir: Path) -> Path:
    return Path(pki_dir) / "users" / USERS_REGISTRY_FILENAME


def _parse_ts(value: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _cert_registry_entry(cert) -> dict:
    return {
        "cn": _subject_cn(cert),
        "serial": int(cert.serial_number),
        "fingerprint": cert.fingerprint(hashes.SHA256()).hex(),
        "created_at": cert.not_valid_before_utc.isoformat(),
        "expires_at": cert.not_valid_after_utc.isoformat(),
        "revoked_at": "",
        "reason": "",
    }


def _load_user_registry(pki_dir: Path) -> dict:
    try:
        doc = yaml.safe_load(_registry_path(pki_dir).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _save_user_registry(pki_dir: Path, registry: dict) -> None:
    path = _registry_path(pki_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yml.tmp")
    tmp.write_text(yaml.safe_dump(registry, allow_unicode=True, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _sync_user_registry(pki_dir: Path) -> dict:
    """把 pki/users/*/client.crt 中未登记的证书补进登记表（兼容旧版本签发的证书）。"""
    registry = _load_user_registry(pki_dir)
    changed = False
    for cert_path in sorted((Path(pki_dir) / "users").glob("*/client.crt")):
        try:
            cert = _load_cert(cert_path)
        except (OSError, ValueError):
            continue
        key = format(cert.serial_number, "x")
        if key in registry:
            continue
        registry[key] = _cert_registry_entry(cert)
        changed = True
    if changed:
        _save_user_registry(pki_dir, registry)
    return registry


def register_user_cert(pki_dir: Path, cert_path: Path) -> None:
    """登记由 users CA 签发的证书（按序列号幂等；已吊销条目保持吊销状态）。"""
    cert = _load_cert(Path(cert_path))
    key = format(cert.serial_number, "x")
    with _CRL_LOCK:
        registry = _load_user_registry(pki_dir)
        if key in registry:
            return
        registry[key] = _cert_registry_entry(cert)
        _save_user_registry(pki_dir, registry)


def list_user_certs(pki_dir: Path) -> list[dict]:
    """列出 users 域证书（含已吊销/已过期；serial 为 hex，fingerprint 为 sha256）。"""
    with _CRL_LOCK:
        registry = _sync_user_registry(pki_dir)
        now = _dt.datetime.now(_dt.timezone.utc)
        items = []
        for key, entry in registry.items():
            if not isinstance(entry, dict):
                continue
            expires = _parse_ts(str(entry.get("expires_at") or ""))
            items.append({
                "cn": str(entry.get("cn") or ""),
                "serial": key,
                "fingerprint": str(entry.get("fingerprint") or ""),
                "created_at": str(entry.get("created_at") or ""),
                "expires_at": str(entry.get("expires_at") or ""),
                "revoked_at": str(entry.get("revoked_at") or ""),
                "reason": str(entry.get("reason") or ""),
                "expired": expires is None or expires <= now,
            })
        items.sort(key=lambda e: (e["cn"], e["created_at"]))
        return items


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
    register_user_cert(pki_dir, cert_path)
    from . import audit
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


def revoke_user_cert(pki_dir: Path, cn: str, reason: str = "unspecified",
                     serial: str | None = None) -> list[dict]:
    """吊销某 CN 的证书：登记标记 + 重签 CRL，返回被吊销条目。

    serial 为空 = 吊销该 CN 全部未吊销证书；给出时只吊销该序列号（须属于该 CN）。
    找不到未吊销证书返回空列表（调用方转 404）。reason/serial 非法抛 ValueError。
    """
    if reason not in REVOCATION_REASONS:
        raise ValueError(f"unknown revocation reason: {reason}")
    cn = cn.strip()
    if not cn:
        raise ValueError("cn required")
    target = None
    if serial is not None:
        raw = serial.strip().lower().removeprefix("0x")
        if not raw or not re.fullmatch(r"[0-9a-f]+", raw):
            raise ValueError(f"invalid serial: {serial}")
        target = int(raw, 16)
    now = _dt.datetime.now(_dt.timezone.utc)
    with _CRL_LOCK:
        registry = _sync_user_registry(pki_dir)
        revoked: list[dict] = []
        for entry in registry.values():
            if not isinstance(entry, dict) or entry.get("cn") != cn:
                continue
            if entry.get("revoked_at"):
                continue
            if target is not None and int(entry.get("serial") or 0) != target:
                continue
            entry["revoked_at"] = now.isoformat()
            entry["reason"] = reason
            revoked.append(entry)
        if revoked:
            _save_user_registry(pki_dir, registry)
            write_nginx_crl(pki_dir)
            from . import audit
            audit.alarm("crl_updated",
                        detail=f"cn={cn}, serials={[format(int(e['serial']), 'x') for e in revoked]}",
                        route="pki", principal=cn)
        return [
            {"serial": format(int(e["serial"]), "x"),
             "fingerprint": str(e.get("fingerprint") or ""),
             "reason": reason}
            for e in revoked
        ]


# ── 组件（nodes 域）证书登记与吊销 ──
# 组件证书由 nodes CA 签发（agent / nvmet-host），按序列号登记后即可填 nodes CRL；
# nginx 的 clients.crl 已包含这张表，吊销后下一次握手即失败。控制面不保存组件证书
# 文件：登记只发生在签发（enroll/renew）时。

def _component_registry_path(pki_dir: Path) -> Path:
    return Path(pki_dir) / "components" / USERS_REGISTRY_FILENAME


def _load_component_registry(pki_dir: Path) -> dict:
    try:
        doc = yaml.safe_load(_component_registry_path(pki_dir).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _save_component_registry(pki_dir: Path, registry: dict) -> None:
    path = _component_registry_path(pki_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yml.tmp")
    tmp.write_text(yaml.safe_dump(registry, allow_unicode=True, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _sync_component_registry(pki_dir: Path) -> dict:
    """读组件登记表（控制面不保存组件证书文件，登记只在签发时写入）。

    P4 之前签发的组件证书没有登记条目：下一次 renew 重新签发时入库，此前不可吊销
    （节点侧仍可用 --rotate 换钥，但旧证书会自然过期）。
    """
    return _load_component_registry(pki_dir)


def register_component_cert(pki_dir: Path, cert_pem: bytes, agent_id: str, kind: str) -> None:
    """登记一张 nodes CA 签发的组件证书（按序列号幂等）。"""
    cert = x509.load_pem_x509_certificate(cert_pem)
    key = format(cert.serial_number, "x")
    with _CRL_LOCK:
        registry = _load_component_registry(pki_dir)
        if key in registry:
            return
        entry = _cert_registry_entry(cert)
        entry["agent_id"] = agent_id
        entry["kind"] = kind
        registry[key] = entry
        _save_component_registry(pki_dir, registry)


def list_component_certs(pki_dir: Path) -> list[dict]:
    """列出组件证书（含已吊销/已过期）；serial 为 hex，fingerprint 为 sha256。"""
    with _CRL_LOCK:
        registry = _sync_component_registry(pki_dir)
        now = _dt.datetime.now(_dt.timezone.utc)
        items = []
        for key, entry in registry.items():
            if not isinstance(entry, dict):
                continue
            expires = _parse_ts(str(entry.get("expires_at") or ""))
            items.append({
                "cn": str(entry.get("cn") or ""),
                "agent_id": str(entry.get("agent_id") or ""),
                "kind": str(entry.get("kind") or ""),
                "serial": key,
                "fingerprint": str(entry.get("fingerprint") or ""),
                "created_at": str(entry.get("created_at") or ""),
                "expires_at": str(entry.get("expires_at") or ""),
                "revoked_at": str(entry.get("revoked_at") or ""),
                "reason": str(entry.get("reason") or ""),
                "expired": expires is None or expires <= now,
            })
        items.sort(key=lambda e: (e["agent_id"], e["kind"], e["serial"]))
        return items


def revoke_component_cert(pki_dir: Path, agent_id: str, reason: str = "unspecified",
                          serial: str | None = None) -> list[dict]:
    """吊销某节点的组件证书（默认 client + serving 全部）：登记标记 + 重签 CRL。

    serial 给出时只吊销该序列号。找不到未吊销证书返回空列表。
    """
    if reason not in REVOCATION_REASONS:
        raise ValueError(f"unknown revocation reason: {reason}")
    agent_id = agent_id.strip()
    if not agent_id:
        raise ValueError("agent_id required")
    target = None
    if serial is not None:
        raw = serial.strip().lower().removeprefix("0x")
        if not raw or not re.fullmatch(r"[0-9a-f]+", raw):
            raise ValueError(f"invalid serial: {serial}")
        target = int(raw, 16)
    now = _dt.datetime.now(_dt.timezone.utc)
    with _CRL_LOCK:
        registry = _sync_component_registry(pki_dir)
        revoked: list[dict] = []
        for entry in registry.values():
            if not isinstance(entry, dict) or entry.get("agent_id") != agent_id:
                continue
            if entry.get("revoked_at"):
                continue
            if target is not None and int(entry.get("serial") or 0) != target:
                continue
            entry["revoked_at"] = now.isoformat()
            entry["reason"] = reason
            revoked.append(entry)
        if revoked:
            _save_component_registry(pki_dir, registry)
            write_nginx_crl(pki_dir)
            from . import audit
            audit.alarm("crl_updated",
                        detail=("agent=%s, serials=%s" % (
                            agent_id,
                            [format(int(e["serial"]), "x") for e in revoked])),
                        route="pki", principal=agent_id)
        return [
            {"serial": format(int(e["serial"]), "x"),
             "fingerprint": str(e.get("fingerprint") or ""),
             "kind": str(e.get("kind") or ""),
             "reason": reason}
            for e in revoked
        ]


def component_cert_revoked(pki_dir: Path, serial: str) -> bool:
    """组件证书序列号是否已吊销（renew 的应用侧兜底：CRL 尚未 reload 时也拒绝）。"""
    raw = (serial or "").strip().lower().removeprefix("0x")
    if not raw or not re.fullmatch(r"[0-9a-f]+", raw):
        return False
    with _CRL_LOCK:
        entry = _sync_component_registry(pki_dir).get(format(int(raw, 16), "x"))
    return bool(isinstance(entry, dict) and entry.get("revoked_at"))


# ── 控制面自身证书（control 域）登记与吊销 ──
# control CA 只签控制面自身 client cert（cp→agent 的 mTLS 身份）。组件入站按
# control CRL 判断该证书是否仍有效；吊销后节点在下一个刷新周期内拒绝控制面连接。

def _control_registry_path(pki_dir: Path) -> Path:
    return Path(pki_dir) / "control" / USERS_REGISTRY_FILENAME


def _load_control_registry(pki_dir: Path) -> dict:
    try:
        doc = yaml.safe_load(_control_registry_path(pki_dir).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _save_control_registry(pki_dir: Path, registry: dict) -> None:
    path = _control_registry_path(pki_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yml.tmp")
    tmp.write_text(yaml.safe_dump(registry, allow_unicode=True, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _sync_control_registry(pki_dir: Path) -> dict:
    """把 components/*/client.crt 中由 control CA 签发、尚未登记的证书补进台账。

    兼容 P4 之前生成的控制面证书：控制面不保存组件证书文件，但自身 client cert
    落在 components/<component>/client.crt，可按 issuer 识别。
    """
    registry = _load_control_registry(pki_dir)
    try:
        control_ca = _load_cert(ca_dir(pki_dir, "control") / CA_FILENAME)
        issuer_cn = _subject_cn(control_ca)
    except (OSError, ValueError):
        return registry
    changed = False
    for cert_path in sorted((Path(pki_dir) / "components").glob("*/client.crt")):
        try:
            cert = _load_cert(cert_path)
        except (OSError, ValueError):
            continue
        if _issuer_cn(cert) != issuer_cn:
            continue
        key = format(cert.serial_number, "x")
        if key in registry:
            continue
        registry[key] = _cert_registry_entry(cert)
        changed = True
    if changed:
        _save_control_registry(pki_dir, registry)
    return registry


def register_control_cert(pki_dir: Path, cert_pem: bytes) -> None:
    """登记一张 control CA 签发的控制面证书（按序列号幂等）。"""
    cert = x509.load_pem_x509_certificate(cert_pem)
    key = format(cert.serial_number, "x")
    with _CRL_LOCK:
        registry = _load_control_registry(pki_dir)
        if key in registry:
            return
        registry[key] = _cert_registry_entry(cert)
        _save_control_registry(pki_dir, registry)


def list_control_certs(pki_dir: Path) -> list[dict]:
    """列出 control 域证书（含已吊销/已过期）；serial 为 hex，fingerprint 为 sha256。"""
    with _CRL_LOCK:
        registry = _sync_control_registry(pki_dir)
        now = _dt.datetime.now(_dt.timezone.utc)
        items = []
        for key, entry in registry.items():
            if not isinstance(entry, dict):
                continue
            expires = _parse_ts(str(entry.get("expires_at") or ""))
            items.append({
                "cn": str(entry.get("cn") or ""),
                "serial": key,
                "fingerprint": str(entry.get("fingerprint") or ""),
                "created_at": str(entry.get("created_at") or ""),
                "expires_at": str(entry.get("expires_at") or ""),
                "revoked_at": str(entry.get("revoked_at") or ""),
                "reason": str(entry.get("reason") or ""),
                "expired": expires is None or expires <= now,
            })
        items.sort(key=lambda e: (e["cn"], e["created_at"]))
        return items


def revoke_control_cert(pki_dir: Path, cn: str, reason: str = "unspecified",
                        serial: str | None = None) -> list[dict]:
    """吊销控制面自身证书（control CA）：标记 + 重签 control CRL。

    与组件吊销分开：control 域不进 443 管理面，不触发 nginx reload；节点侧按
    刷新周期拉取 control CRL 后拒绝已吊销的控制面连接。
    """
    if reason not in REVOCATION_REASONS:
        raise ValueError(f"unknown revocation reason: {reason}")
    cn = cn.strip()
    if not cn:
        raise ValueError("cn required")
    target = None
    if serial is not None:
        raw = serial.strip().lower().removeprefix("0x")
        if not raw or not re.fullmatch(r"[0-9a-f]+", raw):
            raise ValueError(f"invalid serial: {serial}")
        target = int(raw, 16)
    now = _dt.datetime.now(_dt.timezone.utc)
    with _CRL_LOCK:
        registry = _sync_control_registry(pki_dir)
        revoked: list[dict] = []
        for entry in registry.values():
            if not isinstance(entry, dict) or entry.get("cn") != cn:
                continue
            if entry.get("revoked_at"):
                continue
            if target is not None and int(entry.get("serial") or 0) != target:
                continue
            entry["revoked_at"] = now.isoformat()
            entry["reason"] = reason
            revoked.append(entry)
        if revoked:
            _save_control_registry(pki_dir, registry)
            write_nginx_crl(pki_dir)
            from . import audit
            audit.alarm("crl_updated",
                        detail=f"control cn={cn}, serials="
                               f"{[format(int(e['serial']), 'x') for e in revoked]}",
                        route="pki", principal=cn)
        return [
            {"serial": format(int(e["serial"]), "x"),
             "fingerprint": str(e.get("fingerprint") or ""),
             "reason": reason}
            for e in revoked
        ]


def _build_crl(pki_dir: Path, kind: str, revoked: list[dict],
               next_update_days: int = CRL_NEXT_UPDATE_DAYS) -> bytes:
    """按指定 CA 签发一张 CRL；revoked 条目为登记表记录（含 serial/revoked_at/reason）。"""
    ca_cert = _load_cert(ca_dir(pki_dir, kind) / CA_FILENAME)
    ca_key = _load_key(ca_dir(pki_dir, kind) / CA_KEY_FILENAME)
    now = _dt.datetime.now(_dt.timezone.utc)
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca_cert.subject)
        .last_update(now - _dt.timedelta(minutes=5))
        .next_update(now + _dt.timedelta(days=next_update_days))
    )
    for entry in revoked:
        revoked_at = _parse_ts(str(entry.get("revoked_at") or "")) or now
        flags = REVOCATION_REASONS.get(str(entry.get("reason") or ""),
                                       x509.ReasonFlags.unspecified)
        revoked_cert = (
            x509.RevokedCertificateBuilder()
            .serial_number(int(entry["serial"]))
            .revocation_date(revoked_at)
            .add_extension(x509.CRLReason(flags), critical=False)
            .build()
        )
        builder = builder.add_revoked_certificate(revoked_cert)
    builder = builder.add_extension(x509.CRLNumber(int(now.timestamp())), critical=False)
    builder = builder.add_extension(
        x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
        critical=False,
    )
    crl = builder.sign(private_key=ca_key, algorithm=hashes.SHA256())
    return crl.public_bytes(serialization.Encoding.PEM)


def write_nginx_crl(pki_dir: Path) -> Path:
    """重签 users/nodes/control CRL 并写入 nginx 卷。

    - clients.crl：users + nodes 拼接，nginx 443 的 ssl_crl 使用；
    - nodes.crl：组件域（agent/nvmet-host）单独使用，出站 TLS 加载；
    - control.crl：控制面自身证书，组件入站校验使用。

    幂等：启动时即使无吊销也写空 CRL（nginx 配置引用 clients.crl，缺失会导致启动失败）。
    组件域 next_update 放宽到 30 天，容忍控制面短暂不可达。
    """
    with _CRL_LOCK:
        registry = _sync_user_registry(pki_dir)
        users_revoked = [
            e for e in registry.values()
            if isinstance(e, dict) and e.get("revoked_at") and e.get("serial") is not None
        ]
        users_pem = _build_crl(pki_dir, "users", users_revoked)
        component_registry = _sync_component_registry(pki_dir)
        nodes_revoked = [
            e for e in component_registry.values()
            if isinstance(e, dict) and e.get("revoked_at") and e.get("serial") is not None
        ]
        nodes_pem = _build_crl(pki_dir, "nodes", nodes_revoked,
                               COMPONENT_CRL_NEXT_UPDATE_DAYS)
        control_registry = _sync_control_registry(pki_dir)
        control_revoked = [
            e for e in control_registry.values()
            if isinstance(e, dict) and e.get("revoked_at") and e.get("serial") is not None
        ]
        control_pem = _build_crl(pki_dir, "control", control_revoked,
                                 COMPONENT_CRL_NEXT_UPDATE_DAYS)
        target_dir = Path(pki_dir) / "nginx"
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, data in (
            ("users.crl", users_pem),
            ("clients.crl", users_pem + nodes_pem),
            ("nodes.crl", nodes_pem),
            ("control.crl", control_pem),
        ):
            path = target_dir / name
            tmp = path.with_suffix(".crl.tmp")
            tmp.write_bytes(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        return target_dir / "clients.crl"


def component_crls(pki_dir: Path) -> dict[str, str]:
    """组件分发用 CRL：返回 nodes/control 的 PEM 文本与 next_update。

    文件缺失时补签一次（升级后首次 enroll 可能早于启动重签）。
    """
    with _CRL_LOCK:
        target = Path(pki_dir) / "nginx"
        nodes_path = target / "nodes.crl"
        control_path = target / "control.crl"
        if not nodes_path.is_file() or not control_path.is_file():
            write_nginx_crl(pki_dir)
        nodes_pem = nodes_path.read_text(encoding="utf-8")
        control_pem = control_path.read_text(encoding="utf-8")
        nodes_crl = x509.load_pem_x509_crl(nodes_pem.encode())
        return {
            "nodes_crl": nodes_pem,
            "control_crl": control_pem,
            "next_update": nodes_crl.next_update_utc.isoformat(),
        }


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
    cert_path.write_bytes(cert)
    register_control_cert(pki_dir, cert)
    log.info("pki: control-plane client cert ready: %s", cert_path)
    return cert_path, key_path


# ============================ 管理面凭据（admin.conf）============================
# 管理凭据由 users CA 签发（与 nodes 域结构隔离）：
# admin client cert（CN=admin，无 O；组由 state/users.yml 决定）→ pki/users/admin/；管理凭据文件 admin.conf
# （kurrent 原生扁平 YAML：server + 三个证书路径键；证书路径为相对 admin.conf 目录的引用，
# 宿主/容器视角一致）。CLI 自动发现该文件走 443 mTLS；轮换 = 证书过期后重启
# （自动重签并刷新 admin.conf，保留管理员手改的 server）；历史 kubeconfig 结构启动时
# 自动迁移为扁平格式（原 server 保留）。

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
        cert_path.write_bytes(cert)
        log.info("pki: admin client cert issued (cn=%s, %d days)", cn, days)
        from . import audit
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

    两条信任链独立：客户端身份 = users CA 签发的 admin 证书（nginx 校链）；
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


def sign_component_certs(ca_cert, ca_key, agent_id: str, component: str,
                         client_csr: bytes, serving_csr: bytes,
                         serving_sans: list[str],
                         pki_dir: Path | None = None) -> dict[str, bytes]:
    """为组件签发 client + serving 两张证书（agent 代 nvmet-host 的场景同入口）。

    CSR 的 CN 必须与登记身份一致（防冒名）：CN = <prefix>-<agent_id>。
    给出 pki_dir 时把两张证书登记进组件登记表（吊销/CRL 的依据）。
    """
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
# 引导凭据必须绑定 component + agent_id（enroll 只接受同一节点身份），TTL 内可被
# 多次 enroll 复用（不一次性消耗；不消耗模式见认证蓝图 P3）。台账 key = token_id
#（每 token 独立，多 token 并存，写入时惰性清理过期条目）：
# - agent token：签发时指定节点身份（kurrent token create --agent-id）
# - nvmet-host 派生 token（绑 agent_id）：agent enroll 上报 backend=nvmet 时控制面
#   自动签发随响应下发（能力上报驱动，签发不预知后端）

# 节点身份字符集：与 CLI normalizeAgentID / 证书 CN 一致（小写字母/数字/./-）
AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


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


def normalize_token_scopes(scopes: list[str] | None) -> list[str]:
    """校验/去重自动化 token scope；空 = ["admin"]（兼容 P2 之前签发的 token）。

    scope 语义：admin=全部；readonly=只读；enroll=仅可签发 bootstrap token。
    权限只升不降：含 admin 时不再需要其余 scope。
    """
    if not scopes:
        return ["admin"]
    out: list[str] = []
    for scope in scopes:
        scope = str(scope).strip().lower()
        if not scope:
            continue
        if scope not in config.TOKEN_SCOPES:
            raise ValueError(f"unknown token scope: {scope}")
        if scope not in out:
            out.append(scope)
    if not out:
        raise ValueError("token scope cannot be empty")
    return out


def groups_for_scopes(scopes: list[str]) -> list[str]:
    """scope → 授权组（admin 覆盖其余）。"""
    if "admin" in scopes:
        return [config.ADMIN_GROUP]
    if "readonly" in scopes:
        return [config.VIEWER_GROUP]
    return ["enroll"]


def issue_bootstrap_token(pki_dir: Path, agent_id: str = "", component: str = "agent",
                          usage: list[str] | None = None,
                          groups: list[str] | None = None, name: str = "",
                          ttl_hours: int | None = None,
                          ttl_minutes: int | None = None,
                          max_uses: int = 1,
                          scopes: list[str] | None = None) -> str:
    """签发 token，返回明文（登记只存 secret 的 sha256；key = token_id）。

    无幂等沿用（每次新签；明文不可恢复）；
    bootstrap token（usage=enroll）必须绑定 agent_id + component，默认 60 分钟 TTL、
    一次性（max_uses=1，旧条目无该字段 = 不限）；管理 token（usage=["admin"] + groups）
    供认证器链 Bearer 与 WebUI 登录使用，不绑组件（component=""），scope 决定授权
    （admin/readonly/enroll），TTL 默认 24h、上限 30 天。
    """
    usage = usage or ["enroll"]
    if "admin" not in usage:
        if not agent_id:
            raise ValueError("bootstrap token must bind agent_id")
        if not AGENT_ID_RE.fullmatch(agent_id):
            raise ValueError("agent_id must match [a-z0-9][a-z0-9.-]*")
        if ttl_hours is not None:
            raise ValueError("ttl_hours only applies to admin tokens")
        if scopes:
            raise ValueError("scopes only apply to admin tokens")
        minutes = (config.settings.bootstrap_token_ttl_minutes_default
                   if ttl_minutes is None else int(ttl_minutes))
        if not 1 <= minutes <= config.settings.bootstrap_token_ttl_minutes_max:
            raise ValueError(
                f"ttl_minutes must be between 1 and {config.settings.bootstrap_token_ttl_minutes_max}")
        if not 1 <= int(max_uses) <= config.settings.bootstrap_token_max_uses_upper:
            raise ValueError(
                f"max_uses must be between 1 and {config.settings.bootstrap_token_max_uses_upper}")
        ttl = _dt.timedelta(minutes=minutes)
        token_scopes: list[str] = []
        token_groups = groups or []
        max_uses = int(max_uses)
    else:
        token_scopes = normalize_token_scopes(scopes)
        hours = config.settings.token_ttl_hours_default if ttl_hours is None else int(ttl_hours)
        if not 1 <= hours <= config.settings.token_ttl_hours_max:
            raise ValueError(
                f"ttl_hours must be between 1 and {config.settings.token_ttl_hours_max}")
        ttl = _dt.timedelta(hours=hours)
        token_groups = groups if groups is not None else groups_for_scopes(token_scopes)
        max_uses = 0  # 管理 token 不按次数消耗

    token_id = secrets.token_hex(3)
    token_secret = secrets.token_hex(8)
    now = _dt.datetime.now(_dt.timezone.utc)
    entry = {
        "component": component,
        "agent_id": agent_id or "",
        # 明文仅在生成时可见：登记只存 secret 部分 hash（token_id 为台账 key，非机密）
        "token_secret_hash": hashlib.sha256(token_secret.encode()).hexdigest(),
        "created_at": now.isoformat(),
        "expires_at": (now + ttl).isoformat(),
        "last_used_at": "",
        "usage": usage,
        "groups": token_groups,
        "scopes": token_scopes,
    }
    if max_uses:
        entry["max_uses"] = max_uses
        entry["uses"] = 0
    if name:
        entry["name"] = name
    with _TOKENS_LOCK:
        tokens = _load_tokens(pki_dir)
        tokens[token_id] = entry
        _save_tokens(pki_dir, tokens)
    log.info("pki: token issued: usage=%s scopes=%s component=%s agent=%s ttl=%s max_uses=%s",
             entry["usage"], entry["scopes"], component or "-", agent_id or "-",
             ttl, max_uses or "-")
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
            "scopes": list(entry.get("scopes") or []),
            "component": str(entry.get("component") or ""),
            "agent_id": str(entry.get("agent_id") or ""),
            "name": str(entry.get("name") or ""),
            "created_at": str(entry.get("created_at") or ""),  # 旧条目无此字段（签发时才注入）
            "expires_at": str(entry.get("expires_at") or ""),
            "last_used_at": str(entry.get("last_used_at") or ""),
            "uses": int(entry.get("uses") or 0),
            "max_uses": int(entry.get("max_uses") or 0),
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


def _split_token(token: str) -> tuple[str, str]:
    token = token.strip()
    token_id, _, secret = token.partition(".")
    if not token_id or not secret:
        raise ValueError("malformed bootstrap token")
    return token_id, secret


def _check_bootstrap_entry(entry: dict | None, token_id: str, secret: str,
                           agent_id: str, component: str) -> dict:
    if not entry:
        raise ValueError(f"no bootstrap token for token id {token_id}")
    if entry.get("component") != component:
        raise ValueError("bootstrap token component mismatch")
    if entry.get("agent_id") != agent_id:
        raise ValueError("bootstrap token agent mismatch")
    if not hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(),
                               entry.get("token_secret_hash", "")):
        raise ValueError("bootstrap token invalid")
    if not _entry_active(entry.get("expires_at"), _dt.datetime.now(_dt.timezone.utc)):
        raise ValueError("bootstrap token expired")
    return entry


def validate_bootstrap_token(pki_dir: Path, token: str, agent_id: str, component: str) -> None:
    """只校验 bootstrap token（不消耗使用额度）：格式、登记、组件/绑定、hash、TTL。

    失败抛 ValueError（调用方转 401）。enroll 实际走 reserve_bootstrap_token，
    先原子占额度、签发失败再释放。
    """
    token_id, secret = _split_token(token)
    _check_bootstrap_entry(_load_tokens(pki_dir).get(token_id), token_id, secret,
                           agent_id, component)
    log.info("pki: bootstrap token validated: component=%s agent=%s", component, agent_id)


def reserve_bootstrap_token(pki_dir: Path, token: str, agent_id: str,
                            component: str) -> str:
    """原子占用一次使用额度（校验与自增在同一把锁内），返回 token_id。

    旧条目无 max_uses/uses 字段 = 不限次数（P2 签发、尚未过期的 token 保持可用）；
    额度耗尽抛 ValueError（调用方转 401）。
    """
    token_id, secret = _split_token(token)
    with _TOKENS_LOCK:
        tokens = _load_tokens(pki_dir)
        entry = _check_bootstrap_entry(tokens.get(token_id), token_id, secret,
                                       agent_id, component)
        max_uses = int(entry.get("max_uses") or 0)
        uses = int(entry.get("uses") or 0)
        if max_uses and uses >= max_uses:
            raise ValueError("bootstrap token already used")
        entry["uses"] = uses + 1
        _save_tokens(pki_dir, tokens)
    log.info("pki: bootstrap token reserved: component=%s agent=%s uses=%d/%s",
             component, agent_id, uses + 1, max_uses or "unlimited")
    return token_id


def release_bootstrap_token(pki_dir: Path, token_id: str) -> None:
    """签发失败时退回已占用的额度（成功签发不调用）。"""
    if not token_id:
        return
    with _TOKENS_LOCK:
        tokens = _load_tokens(pki_dir)
        entry = tokens.get(token_id)
        if not isinstance(entry, dict):
            return
        entry["uses"] = max(0, int(entry.get("uses") or 0) - 1)
        _save_tokens(pki_dir, tokens)


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


def touch_token(pki_dir: Path, token_id: str) -> None:
    """记录 token 最近使用时间（节流：每分钟最多回写一次）。"""
    now_monotonic = time.monotonic()
    with _TOKEN_TOUCH_LOCK:
        if now_monotonic - _TOKEN_TOUCH.get(token_id, 0.0) < _TOKEN_TOUCH_INTERVAL_SECONDS:
            return
        _TOKEN_TOUCH[token_id] = now_monotonic
    now = _dt.datetime.now(_dt.timezone.utc)
    with _TOKENS_LOCK:
        tokens = _load_tokens(pki_dir)
        entry = tokens.get(token_id)
        if not isinstance(entry, dict):
            return
        entry["last_used_at"] = now.isoformat()
        _save_tokens(pki_dir, tokens)
