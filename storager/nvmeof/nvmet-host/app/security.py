"""入站安全：peer cert 注入、CN/issuer 白名单与 CRL 校验。

与 storager-agent 同构（独立镜像，代码各自维护）：Python/OpenSSL 不支持在运行中的
SSLContext 热加载 CRL，所以入站吊销在应用层按序列号校验；出站由调用方每次新建
上下文并在 TLS 层校 CRL。

本服务的调用方是同一宿主上的 Agent（共享卷 UDS）；CN 白名单接受
agent-<id>（正常调用）与 nvmet-<id>（本组件健康检查自检）。
"""

import functools
import inspect
import logging
import os
import ssl
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import NameOID
from fastapi import HTTPException

log = logging.getLogger("nvmet-host.security")

CRL_CA_FILES = {"nodes": "ca-nodes.crt", "control": "ca-control.crt"}
INSECURE_ENV = "KURRENT_COMPONENT_INSECURE"
REFRESH_ENV = "KURRENT_COMPONENT_CRL_REFRESH_SECONDS"
DEFAULT_REFRESH_SECONDS = 60


@dataclass(frozen=True)
class PeerIdentity:
    cn: str
    issuer_cn: str
    serial: int


@dataclass(frozen=True)
class CrlSnapshot:
    revoked: frozenset[int]
    next_update: datetime
    fetched_at: datetime


class CrlStore:
    """两份吊销表的内存快照；刷新线程原子替换，请求路径只读。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: dict[str, CrlSnapshot] = {}

    def update(self, kind: str, crl) -> None:
        snapshot = CrlSnapshot(
            revoked=frozenset(entry.serial_number for entry in crl),
            next_update=crl.next_update_utc,
            fetched_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._snapshots[kind] = snapshot

    def snapshot(self, kind: str) -> CrlSnapshot | None:
        with self._lock:
            return self._snapshots.get(kind)

    def healthy(self, kind: str, now: datetime | None = None) -> bool:
        snapshot = self.snapshot(kind)
        return bool(snapshot and (now or datetime.now(timezone.utc)) < snapshot.next_update)

    def revoked(self, kind: str, serial: int) -> bool:
        snapshot = self.snapshot(kind)
        return bool(snapshot and serial in snapshot.revoked)

    def describe(self) -> dict:
        with self._lock:
            return {
                kind: {
                    "revoked": len(snapshot.revoked),
                    "next_update": snapshot.next_update.isoformat(),
                    "fetched_at": snapshot.fetched_at.isoformat(),
                }
                for kind, snapshot in self._snapshots.items()
            }


CRL_STORE = CrlStore()


def install_peer_cert_hook() -> None:
    """把 TLS peer cert 注入 ASGI scope（幂等）。"""
    from uvicorn.protocols.http import h11_impl

    if getattr(h11_impl.RequestResponseCycle, "_kurrent_peer_cert", False):
        return
    original = h11_impl.RequestResponseCycle.__init__
    if "transport" not in inspect.signature(original).parameters:
        raise RuntimeError("uvicorn RequestResponseCycle signature changed; update peer cert hook")

    def _init(self, scope, conn, transport, *args, **kwargs):
        original(self, scope, conn, transport, *args, **kwargs)
        scope.setdefault("extensions", {})["tls"] = {
            "peercert": transport.get_extra_info("peercert"),
        }

    h11_impl.RequestResponseCycle.__init__ = _init
    h11_impl.RequestResponseCycle._kurrent_peer_cert = True


def _name_cn(name) -> str:
    """兼容两种形态：ssl getpeercert 的 dict 元组与 cryptography 的 x509.Name。"""
    if hasattr(name, "get_attributes_for_oid"):
        attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
        return attrs[0].value if attrs else ""
    for rdn in name or ():
        for key, value in rdn:
            if key in ("commonName", "CN"):
                return str(value)
    return ""


@functools.lru_cache(maxsize=8)
def ca_subject_cn(ca_file: str) -> str:
    """信任锚的主体 CN：兼容遗留单 CA（kurrent-ca）与三分根（kurrent-ca-nodes）。"""
    cert = x509.load_pem_x509_certificate(Path(ca_file).read_bytes())
    return _name_cn(cert.subject)


def peer_identity(scope) -> PeerIdentity | None:
    cert = (scope.get("extensions") or {}).get("tls", {}).get("peercert")
    if not cert:
        return None
    try:
        serial = int(str(cert.get("serialNumber") or "0"), 16)
    except ValueError:
        return None
    return PeerIdentity(
        cn=_name_cn(cert.get("subject")),
        issuer_cn=_name_cn(cert.get("issuer")),
        serial=serial,
    )


def verify_peer(scope, *, allowed_cns: set[str], issuer_cn: str,
                crl_kind: str, store: CrlStore) -> PeerIdentity | None:
    """CN + issuer + CRL 校验；失败抛 HTTPException。"""
    if scope.get("scheme") != "https":
        raise HTTPException(401, "TLS required")
    if os.environ.get(INSECURE_ENV) == "1":
        return None
    ident = peer_identity(scope)
    if ident is None:
        raise HTTPException(401, "client certificate required")
    if ident.issuer_cn != issuer_cn:
        raise HTTPException(403, "client certificate issuer not allowed")
    if ident.cn not in allowed_cns:
        raise HTTPException(403, f"client certificate cn not allowed: {ident.cn!r}")
    if not store.healthy(crl_kind):
        raise HTTPException(503, f"{crl_kind} CRL unavailable or stale")
    if store.revoked(crl_kind, ident.serial):
        raise HTTPException(403, "client certificate revoked")
    return ident


def write_crls(pki_dir: Path, data: dict, store: CrlStore) -> None:
    """校验 issuer 后原子落盘 nodes.crl/control.crl 并更新内存快照。"""
    for kind, field in (("nodes", "nodes_crl"), ("control", "control_crl")):
        pem = str(data.get(field) or "")
        if not pem:
            raise ValueError(f"crl response missing {field}")
        crl = x509.load_pem_x509_crl(pem.encode())
        expected = ca_subject_cn(str(Path(pki_dir) / CRL_CA_FILES[kind]))
        if _name_cn(crl.issuer) != expected:
            raise ValueError(f"{field} issuer mismatch")
        path = Path(pki_dir) / f"{kind}.crl"
        tmp = path.with_suffix(".crl.tmp")
        tmp.write_text(pem, encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        store.update(kind, crl)


def refresh_once(client, pki_dir: Path, store: CrlStore) -> None:
    write_crls(Path(pki_dir), client.fetch_crls(), store)


def _initial_fetch(client, pki_dir: Path, store: CrlStore,
                   retries: int = 5, delay: float = 2.0) -> bool:
    """初始拉取：短重试吸收控制面/nginx 启动期的 502，仍失败则交周期刷新。

    周期刷新开启时由刷新线程调用：服务先起来（入站 fail-closed），重试不占启动路径，
    避免控制面晚就绪把组件启动阻塞成一次性的永久失败。周期刷新被关闭时没有线程可交棒，
    只能在调用方做一次有界重试。"""
    for attempt in range(1, max(1, retries) + 1):
        try:
            refresh_once(client, pki_dir, store)
            log.info("crl: initial fetch ok (%s)", store.describe())
            return True
        except Exception as exc:
            log.warning("crl: initial fetch %d/%d failed: %s", attempt, retries, exc)
            if attempt < retries and delay > 0:
                threading.Event().wait(delay)
    log.warning("crl: initial fetch failed, inbound fails closed until periodic refresh succeeds")
    return False


def start_refresh_thread(client, pki_dir: Path, store: CrlStore) -> threading.Thread | None:
    """后台初始重试 + 周期刷新；初始失败不退出（degraded，入站 fail-closed）。"""
    try:
        interval = int(os.environ.get(REFRESH_ENV, DEFAULT_REFRESH_SECONDS))
    except ValueError:
        interval = DEFAULT_REFRESH_SECONDS
    if interval <= 0:
        log.warning("crl: refresh disabled (%s=%s)", REFRESH_ENV, interval)
        _initial_fetch(client, pki_dir, store)
        return None

    def _loop() -> None:
        _initial_fetch(client, pki_dir, store)
        while True:
            threading.Event().wait(interval)
            try:
                refresh_once(client, pki_dir, store)
            except Exception as exc:
                log.warning("crl: refresh failed: %s", exc)

    thread = threading.Thread(target=_loop, name="crl-refresh", daemon=True)
    thread.start()
    return thread


def build_server_context(pki_dir: Path, ca_file: str) -> ssl.SSLContext:
    """服务端 mTLS 上下文：强制客户端证书链，CRL 由应用层校验（可热更新）。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(Path(pki_dir) / "serving.crt"),
                        str(Path(pki_dir) / "serving.key"))
    ctx.load_verify_locations(str(Path(pki_dir) / ca_file))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx
