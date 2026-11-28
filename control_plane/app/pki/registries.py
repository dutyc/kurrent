"""pki 子模块：registries。"""

import logging

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from pathlib import Path
import datetime as _dt
import os
import re
import yaml
from .common import CA_FILENAME, CA_KEY_FILENAME, COMPONENT_CRL_NEXT_UPDATE_DAYS, CRL_NEXT_UPDATE_DAYS, REVOCATION_REASONS, USERS_REGISTRY_FILENAME, _CRL_LOCK
from .common import _align_owner, _issuer_cn, _load_cert, _load_key, _parse_ts, _subject_cn, ca_dir

log = logging.getLogger("control-plane")

def _registry_path(pki_dir: Path) -> Path:
    return Path(pki_dir) / "users" / USERS_REGISTRY_FILENAME


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
    _align_owner(tmp)
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
            from ..auth import audit
            audit.alarm("crl_updated",
                        detail=f"cn={cn}, serials={[format(int(e['serial']), 'x') for e in revoked]}",
                        route="pki", principal=cn)
        return [
            {"serial": format(int(e["serial"]), "x"),
             "fingerprint": str(e.get("fingerprint") or ""),
             "reason": reason}
            for e in revoked
        ]


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
    _align_owner(tmp)
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
            from ..auth import audit
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
    """组件证书序列号是否已吊销（renew 的应用侧校验：CRL 尚未 reload 时也拒绝）。"""
    raw = (serial or "").strip().lower().removeprefix("0x")
    if not raw or not re.fullmatch(r"[0-9a-f]+", raw):
        return False
    with _CRL_LOCK:
        entry = _sync_component_registry(pki_dir).get(format(int(raw, 16), "x"))
    return bool(isinstance(entry, dict) and entry.get("revoked_at"))


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
    _align_owner(tmp)
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
            from ..auth import audit
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
            _align_owner(tmp)
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
