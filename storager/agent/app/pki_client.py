"""组件 PKI 客户端。

职责：首次启动引导（bootstrap token → /enroll 换证书）+ 证书轮换（现有 client cert
mTLS → /renew），并把 CA/证书落盘到 pki_dir。调用时机：uvicorn 起服务前（模块导入
时），证书未就绪则进程退出（无证书不启动）。

证书布局（pki_dir 由容器挂载持久化）：
  ca-control.crt  控制面客户端证书的信任锚（uvicorn --ssl-ca-certs）
  ca-nodes.crt    同伴组件证书的信任锚（agent → nvmet-host）
  client.crt/key   client cert（本组件身份：向 cp renew、向 nvmet-host 认证）
  serving.crt/key  serving cert（本组件 TLS 服务端）

enroll 凭据 = 绑定本 agent_id 的 bootstrap token（独立凭据文件 agent.token）；
nvmet-host 派生凭据随本组件 enroll/renew 响应下发并落盘（nvmet-host.token）；
renew 凭据 = 现有 client cert（mTLS，nginx 按 issuer 判定 class=component）。
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

# 后端 ISO 光驱能力（与各 backend capabilities() 的 cd 字段一致）：stgt 支持，lio/nvmet 不支持。
# agent 引导时随 enroll 上报，控制面自动登记据此推导 role.cd 与标签。
BACKEND_CD_CAPABILITY = {"stgt": True, "lio": False, "nvmet": False}


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
                 cp_base: str, cp_ca: Path | None, bootstrap_token: str | None,
                 advertise_url: str | None = None, capabilities: dict | None = None,
                 remark: str = "", derived_token_file: Path | None = None):
        self.component = component          # agent | nvmet-host（与控制面 COMPONENT_PREFIX 键一致）
        self.agent_id = agent_id
        # CN 前缀：nvmet-host → nvmet（与控制面 sign_component_certs 的规则一致）
        self.prefix = "nvmet" if component == "nvmet-host" else component
        self.cn = f"{self.prefix}-{agent_id}"
        self.pki_dir = Path(pki_dir)
        self.cp_base = cp_base.rstrip("/")
        self.cp_ca = cp_ca          # nginx 服务器证书（自签，作为信任根）
        self.bootstrap_token = bootstrap_token
        self.advertise_url = (advertise_url or "").strip()  # 自动登记时写入 agents.yml
        self.capabilities = capabilities or {}  # 后端能力摘要（backend/cd），自动登记时推导角色/标签
        self.remark = (remark or "").strip()   # 备注（人类识别用），自动登记时写入 agents.yml
        # nvmet-host 派生凭据落盘路径（仅 agent 组件）：控制面按 backend=nvmet 能力签发
        # 随 enroll/renew 响应下发，本组件写入供 nvmet-host 容器引导（compose 共享目录）
        self.derived_token_file = Path(derived_token_file) if derived_token_file else None

    def ready(self) -> bool:
        """证书就绪、剩余生命周期高于阈值，且两个信任锚都已落盘。"""
        ratio = _cert_remaining_ratio(self.pki_dir / "client.crt")
        return (ratio is not None and ratio > RENEW_THRESHOLD
                and (self.pki_dir / "ca-control.crt").is_file()
                and (self.pki_dir / "ca-nodes.crt").is_file())

    def ensure(self, force_renew: bool = False) -> None:
        """引导或轮换证书（进程启动时调用）。已就绪则跳过；force_renew 时证书在也重签
        （agent 用于 nvmet-host 派生凭据缺失时主动刷新，renew 响应带新派生 token）。"""
        if self.ready() and not force_renew:
            log.info("pki: client cert ok (cn=%s)", self.cn)
            return
        if (self.pki_dir / "client.crt").exists():
            log.warning("pki: renewing client cert (cn=%s, force=%s)", self.cn, force_renew)
            try:
                self._enroll(renew=True)
            except RuntimeError as exc:
                # 旧信任根/证书被拒时回退到 bootstrap enroll：CA 收紧后节点不能
                # 再用旧证书换新证书，但绑定 token 仍允许重新加入。
                if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
                    log.warning("pki: renew rejected (%s); falling back to bootstrap enroll", exc)
                    self._enroll(renew=False)
                else:
                    raise
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
            # 控制面可达地址（声明 spec.agent.advertiseUrl）：控制面自动登记时写入 agents.yml
            "base_url": self.advertise_url,
            # 自身能力摘要（声明 spec.agent.backend → cd 能力）：控制面自动登记推导 role.cd 与标签
            "capabilities": self.capabilities,
            # 备注（metadata.remark，人类识别用）：控制面自动登记时写入 agents.yml
            "remark": self.remark,
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
        for field in ("ca_nodes_crt", "ca_control_crt"):
            if not data.get(field):
                raise RuntimeError(f"pki enroll: missing trust anchor {field}")
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
        _write_pem(self.pki_dir / "ca-nodes.crt", data["ca_nodes_crt"].encode())
        _write_pem(self.pki_dir / "ca-control.crt", data["ca_control_crt"].encode())
        # nvmet-host 派生凭据落盘（agent 组件）：控制面按 backend=nvmet 能力签发随响应
        # 下发；写入共享凭据目录供 nvmet-host 容器引导（缺失响应 = 非 nvmet 后端/旧控制面）
        if self.derived_token_file and data.get("nvmet_token"):
            _write_token(self.derived_token_file, data["nvmet_token"])
            log.info("pki: derived nvmet-host bootstrap token written (%s)", self.derived_token_file)
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


def _write_token(path: Path, token: str) -> None:
    """原子写引导凭据文件（0600，与 join 写入权限一致）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(token.strip() + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def ensure_pki() -> None:
    """模块级入口：从节点配置（kurrent.yaml）+ 引导凭据文件装配并执行引导/轮换。

    绑定 agent_id 的 bootstrap token 在独立凭据文件（compose 挂载，join 写入）；nvmet-host 派生
    凭据随 enroll/renew 响应下发落盘（backend=nvmet 时）；派生凭据缺失（历史部署迁移
    清除等）→ 证书在也强制 renew 一次以重新派生。"""
    from .config import (CONFIG, DEFAULT_BOOTSTRAP_TOKEN_FILE, DEFAULT_CP_CA,
                         DEFAULT_NVMET_BOOTSTRAP_TOKEN_FILE, DEFAULT_PKI_DIR)
    spec = CONFIG.spec
    backend = spec.agent.backend
    capabilities = {"backend": backend, "cd": BACKEND_CD_CAPABILITY.get(backend, False)}
    derived = Path(DEFAULT_NVMET_BOOTSTRAP_TOKEN_FILE)
    force_renew = backend == "nvmet" and not derived.is_file()
    PkiClient(CONFIG.metadata.name, "agent", Path(DEFAULT_PKI_DIR), spec.control_plane.url,
              Path(DEFAULT_CP_CA),
              _read_bootstrap_token(Path(DEFAULT_BOOTSTRAP_TOKEN_FILE)),
              spec.agent.advertise_url or None, capabilities,
              remark=CONFIG.metadata.remark,
              derived_token_file=derived).ensure(force_renew=force_renew)
