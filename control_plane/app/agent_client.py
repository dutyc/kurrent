import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
import ssl
from typing import Any

import httpx
from cryptography import x509

from .config import settings


def _check_crl(path: Path) -> None:
    """出站前确认 nodes CRL 存在且未过期：缺失/过期时 fail-closed，不降级。"""
    try:
        crl = x509.load_pem_x509_crl(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise AgentAPIError("-", 503, f"nodes CRL unavailable: {path}") from exc
    if _dt.datetime.now(_dt.timezone.utc) >= crl.next_update_utc:
        raise AgentAPIError("-", 503, f"nodes CRL expired: {path}")


class AgentAPIError(Exception):
    def __init__(self, agent_id: str, status_code: int, detail: str):
        super().__init__(detail)
        self.agent_id = agent_id
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class AgentConfig:
    id: str
    base_url: str
    role_disk: bool
    role_cd: bool
    storager_ip: str | None = None
    enabled: bool = True
    tags: tuple[str, ...] = ()
    remark: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "base_url": self.base_url,
            "storager_ip": self.storager_ip,
            "role": {"disk": self.role_disk, "cd": self.role_cd},
            "enabled": self.enabled,
            "tags": list(self.tags),
            "remark": self.remark,
        }


class AgentClient:
    """控制面 → Agent 的 mTLS 客户端（控制面持自身客户端证书访问 Agent）。

    身份 = 控制面组件证书（CN=control-plane，control CA 签发）；对端 agent /
    nvmet-host 以 uvicorn ssl_cert_reqs=2 + ssl_ca_certs=ca-control.crt 校验，
    control 域之外无任何证书可用。缺证书材料时拒绝连接（不降级明文）。
    """

    def __init__(self, agent: AgentConfig, timeout: float, *,
                 ca_cert: Path | None = None,
                 client_cert: Path | None = None,
                 client_key: Path | None = None):
        self.agent = agent
        self.timeout = timeout
        comp_dir = settings.pki_dir / "components" / settings.control_plane_component
        self.ca_cert = ca_cert or settings.pki_dir / "ca" / "nodes" / "ca.crt"
        self.crl = settings.pki_dir / "nginx" / "nodes.crl"
        self.client_cert = client_cert or comp_dir / "client.crt"
        self.client_key = client_key or comp_dir / "client.key"
        self._tls_context = None

    def _mtls_context(self) -> ssl.SSLContext:
        """mTLS 上下文：固定信任 nodes CA（节点 serving 证书）+ 本组件 client cert。

        强制 TLS1.2：docker-proxy（userland proxy）对 TLS1.3 客户端证书握手有
        兼容问题（经宿主端口映射的连接会 Broken pipe），且项目 TLS ≤ 1.2 约束
        （nginx 443 同，iPXE mbedTLS 封顶）。
        """
        _check_crl(self.crl)
        ctx = ssl.create_default_context(cafile=str(self.ca_cert))
        ctx.load_verify_locations(str(self.crl))
        ctx.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(self.client_cert), str(self.client_key))
        return ctx

    def healthz(self) -> dict[str, Any]:
        return self._request("GET", "/healthz")

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/capabilities")

    def create_disk(self, iqn: str, filename: str, *, master: str | None = None, size: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"iqn": iqn, "filename": filename}
        if master:
            payload["master"] = master
        if size:
            payload["size"] = size
        return self._request("POST", "/lun/disk", json=payload)

    def create_cd(self, iso: str, iqn: str) -> dict[str, Any]:
        return self._request("POST", "/lun/cd", json={"iso": iso, "iqn": iqn})

    def delete_lun(self, iqn: str, delete_file: bool = False) -> dict[str, Any]:
        return self._request("DELETE", "/lun", params={"iqn": iqn, "delete_file": str(delete_file).lower()})

    def scan(self) -> dict[str, Any]:
        """触发 Agent 扫描镜像目录，为缺失文件重建 target。"""
        return self._request("POST", "/lun/scan")

    def list_luns(self) -> list[dict[str, Any]]:
        return self._request("GET", "/lun")

    def list_masters(self) -> dict[str, Any]:
        """列出 Agent 上可用母盘（后台扫描缓存的 *_tpl_* 文件清单）。"""
        return self._request("GET", "/masters")

    def set_credential(self, worker_id: str, secret: str | None, sub_nqns: list[str],
                       host_nqns: list[str], tls_psk: str | None = None,
                       generation: int = 0) -> dict[str, Any]:
        """推送 NVMe-oF 凭据期望状态给 Agent（控制面驱动，Agent 转调宿主服务同步 hosts 矩阵
        + 目标端 TLS-PSK keyring）。secret=None 吊销该 worker；sub_nqns = 盘子系统 NQN，
        host_nqns = 绑定设备派生 NQN；tls_psk = 派生自认证密钥的 TLS-PSK（见 _derive_tls_psk）。"""
        return self._request("POST", "/credential", json={
            "worker_id": worker_id, "secret": secret,
            "sub_nqns": sub_nqns, "host_nqns": host_nqns, "tls_psk": tls_psk,
            "generation": generation,
        })

    def credential_status(self, worker_id: str, sub_nqns: list[str],
                          host_nqns: list[str]) -> dict[str, Any]:
        """查询目标端 PSK 是否在位（重启后 keyring 清空检测）。"""
        return self._request("POST", "/credential/status", json={
            "worker_id": worker_id, "sub_nqns": sub_nqns, "host_nqns": host_nqns,
        })

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not (self.ca_cert.exists() and self.client_cert.exists() and self.client_key.exists()):
            raise AgentAPIError(self.agent.id, 503,
                                f"control plane client cert missing: {self.client_cert.parent}")
        if self._tls_context is None:
            self._tls_context = self._mtls_context()
        headers = kwargs.pop("headers", {})
        url = f"{self.agent.base_url.rstrip('/')}{path}"
        try:
            with httpx.Client(timeout=self.timeout, verify=self._tls_context) as client:
                response = client.request(method, url, headers=headers, **kwargs)
        except httpx.RequestError as exc:
            raise AgentAPIError(self.agent.id, 503, f"agent request failed: {exc}") from exc
        if response.status_code >= 400:
            raise AgentAPIError(self.agent.id, response.status_code, _response_detail(response))
        if not response.content:
            return None
        return response.json()


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])
    return str(payload)
