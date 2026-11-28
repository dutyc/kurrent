"""pki 共享常量与低层证书工具。"""

import logging

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID
from pathlib import Path
import datetime as _dt
import os
import re
import threading

log = logging.getLogger("control-plane")

CA_FILENAME = "ca.crt"
CA_KEY_FILENAME = "ca.key"
CA_ROOT_DIRNAME = "ca"
CA_KINDS = ("users", "nodes", "control")
CA_CN_PREFIX = "kurrent-ca-"
TOKENS_FILENAME = "bootstrap-tokens.yml"
_TOKENS_LOCK = threading.RLock()
_TOKEN_TOUCH_INTERVAL_SECONDS = 60
_TOKEN_TOUCH_LOCK = threading.Lock()
_TOKEN_TOUCH: dict[str, float] = {}
_CRL_LOCK = threading.RLock()
CA_DAYS = 3650
CRL_DAYS = 30
CRL_NEXT_UPDATE_DAYS = 7
COMPONENT_CRL_NEXT_UPDATE_DAYS = 30
USERS_REGISTRY_FILENAME = "issued.yml"
USER_CN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
REVOCATION_REASONS = {
    "unspecified": x509.ReasonFlags.unspecified,
    "key_compromise": x509.ReasonFlags.key_compromise,
    "superseded": x509.ReasonFlags.superseded,
    "cessation_of_operation": x509.ReasonFlags.cessation_of_operation,
}
COMPONENT_PREFIX = {"agent": "agent", "nvmet-host": "nvmet", "control-plane": "control-plane"}


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


def _parse_ts(value: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _align_owner(path: Path) -> None:
    """把文件属主对齐所在目录（容器内 root 写入宿主状态根时归操作员）。"""
    _chown_like(path, path.parent)


def _chown_like(path: Path, ref: Path) -> None:
    """把文件属主对齐参考对象（挂载卷目录）；非 root 或属主相同则跳过。"""
    try:
        st = ref.stat()
        if os.stat(path).st_uid == st.st_uid and os.stat(path).st_gid == st.st_gid:
            return
        os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass
