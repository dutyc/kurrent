"""组件 PKI 客户端。

职责：首次启动引导（bootstrap token → /enroll 换证书）+ 证书轮换（现有 client cert
mTLS → /renew），并把 CA/证书落盘到 pki_dir。调用时机：uvicorn 起服务前（模块导入
时），证书未就绪则进程退出（无证书不启动）。

证书布局（pki_dir 由容器挂载持久化）：
  ca.crt           内部 CA（校验对端证书链）
  client.crt/key   client cert（本组件身份：向 cp renew、向 nvmet-host 认证）
  serving.crt/key  serving cert（本组件 TLS 服务端）

enroll 凭据 = nvmet-host 派生 bootstrap token（agent enroll 按 backend=nvmet 能力签发
随响应下发并落盘 nvmet-host.token，绑 agent_id 防串用）；
renew 凭据 = 现有 client cert（mTLS，nginx 校验后透传 DN 给控制面）。
"""

import datetime as _dt
import ipaddress
import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

log = logging.getLogger("agent")

RENEW_THRESHOLD = 0.2  # 剩余生命周期低于该比例触发轮换（与控制面 config 一致）


def _san(value: str):
    try:
        return x509.IPAddress(ipaddress.ip_address(value))
    except ValueError:
        return x509.DNSName(value)


def _build_csr(key, cn: str, sans: list[str]) -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    builder = x509.CertificateSigningRequestBuilder().subject_name(name)
    if sans:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([_san(s) for s in sans]), critical=False)
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)


def _gen_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write_pem(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    if path.name.endswith(".key"):
        os.chmod(path, 0o600)


def _cert_remaining_ratio(cert_path: Path) -> float | None:
    """证书剩余生命周期比例；文件缺失/损坏返回 None。"""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return None
    now = _dt.datetime.now(_dt.timezone.utc)
    total = (cert.not_valid_after_utc - cert.not_valid_before_utc).total_seconds()
    left = (cert.not_valid_after_utc - now).total_seconds()
    if total <= 0:
        return None
    return left / total


class PkiClient:
    """组件证书引导/轮换客户端。cp_base 为控制面 nginx 入口（https://host.docker.internal）。"""

    def __init__(self, agent_id: str, component: str, pki_dir: Path,
                 cp_base: str, cp_ca: Path | None, bootstrap_token: str | None):
        self.component = component          # agent | nvmet-host（与控制面 COMPONENT_PREFIX 键一致）
        self.agent_id = agent_id
        # CN 前缀：nvmet-host → nvmet（与控制面 sign_component_certs 的规则一致）
        self.prefix = "nvmet" if component == "nvmet-host" else component
        self.cn = f"{self.prefix}-{agent_id}"
        self.pki_dir = Path(pki_dir)
        self.cp_base = cp_base.rstrip("/")
        self.cp_ca = cp_ca          # nginx 服务器证书（自签，作为信任根）
        self.bootstrap_token = bootstrap_token

    def ready(self) -> bool:
        """证书就绪且剩余生命周期高于阈值。"""
        ratio = _cert_remaining_ratio(self.pki_dir / "client.crt")
        return ratio is not None and ratio > RENEW_THRESHOLD

    def ensure(self) -> None:
        """引导或轮换证书（进程启动时调用）。已就绪则跳过。"""
        if self.ready():
            log.info("pki: client cert ok (cn=%s)", self.cn)
            return
        if (self.pki_dir / "client.crt").exists():
            log.warning("pki: client cert expired or near expiry, renewing (cn=%s)", self.cn)
            self._enroll(renew=True)
        else:
            log.info("pki: no client cert, enrolling (cn=%s)", self.cn)
            self._enroll(renew=False)

    # ---- 内部 ----

    def _enroll(self, renew: bool) -> None:
        key = _gen_key()
        serving_key = _gen_key()
        payload = {
            "agent_id": self.agent_id,
            "component": self.component,
            "csr_client": _build_csr(key, self.cn, []).decode(),
            "csr_serving": _build_csr(serving_key, self.cn, ["host.docker.internal", "127.0.0.1"]).decode(),
            "serving_sans": ["host.docker.internal", "127.0.0.1"],
        }
        headers = {"Content-Type": "application/json"}
        context = self._cp_context(renew)
        if not renew:
            if not self.bootstrap_token:
                raise RuntimeError("no bootstrap token and no client cert: cannot enroll")
            headers["Authorization"] = f"Bearer {self.bootstrap_token}"
        req = urllib.request.Request(
            f"{self.cp_base}/api/cp/enroll{'/renew' if renew else ''}",
            data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=context, timeout=15) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"pki enroll failed: HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"pki enroll unreachable: {exc}") from None

        certs = data.get("certificates", {})
        if "client.crt" not in certs or "serving.crt" not in certs:
            raise RuntimeError(f"pki enroll: unexpected response: {sorted(data.keys())}")
        _write_pem(self.pki_dir / "client.key", key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()))
        _write_pem(self.pki_dir / "serving.key", serving_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()))
        _write_pem(self.pki_dir / "client.crt", certs["client.crt"].encode())
        _write_pem(self.pki_dir / "serving.crt", certs["serving.crt"].encode())
        _write_pem(self.pki_dir / "ca.crt", data["ca_crt"].encode())
        log.info("pki: %s done (cn=%s)", "renew" if renew else "enroll", self.cn)

    def _cp_context(self, with_client_cert: bool) -> ssl.SSLContext:
        """访问控制面的 TLS 上下文：信任 nginx 自签服务器证书（TOFU 模式）。

        挂载 server.crt 即固定信任该证书本体（自签证书同时充当自己的信任根），
        hostname 校验已无意义（证书未含 host.docker.internal SAN），故关闭；
        renew 时带本组件 client cert（nginx 该路径强制 mTLS 校验）。
        """
        context = ssl.create_default_context(cafile=str(self.cp_ca)) if self.cp_ca \
            else ssl.create_default_context()
        context.check_hostname = False
        if with_client_cert:
            context.load_cert_chain(
                str(self.pki_dir / "client.crt"), str(self.pki_dir / "client.key"))
        return context


def _read_bootstrap_token(path: Path) -> str | None:
    """读取引导凭据文件（缺失/空返回 None）。"""
    try:
        value = path.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def ensure_pki() -> None:
    """模块级入口：从节点配置（kurrent.yaml）+ 引导凭据文件装配并执行引导/轮换。

    nvmet-host 派生 token 在独立凭据文件（agent enroll 按能力签发并落盘，compose 只读
    挂载），不在 yml 声明。"""
    from .config import CONFIG, DEFAULT_BOOTSTRAP_TOKEN_FILE, DEFAULT_CP_CA, DEFAULT_PKI_DIR
    spec = CONFIG.spec
    PkiClient(CONFIG.metadata.name, "nvmet-host", Path(DEFAULT_PKI_DIR),
              spec.control_plane.url,
              Path(DEFAULT_CP_CA),
              _read_bootstrap_token(Path(DEFAULT_BOOTSTRAP_TOKEN_FILE))).ensure()
