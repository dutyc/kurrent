"""控制面声明式配置模型：唯一权威 schema（运行时加载与 CLI 校验共用）。

本模块只定义模型与校验，不读取环境、不落盘；运行时单例在 config.py，
`python -m app.config_tool validate|show` 在控制面镜像内复用同一模型——
CLI 不再维护第二套字段白名单。

校验语义：
- 未知字段拒绝（extra="forbid"）、默认值注入、apiVersion/kind 锁定；
- networking 是部署事实（必填）且做网段/池/网关的跨字段校验，错误在
  kurrent init 之前暴露，而不是等控制面容器崩溃；
- 协议契约与安全边界（nonce TTL、超时等）留在代码，不进入声明。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator, model_validator)

CONFIG_API_VERSION = "kurrent.io/v1"

# DNS SAN：允许主机名、localhost 与通配符（nginx 服务器名场景）
_DNS_RE = re.compile(
    r"^(?:\*\.)?(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_METADATA_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")


def parse_san_entries(spec: str) -> list[tuple[str, str]]:
    """解析 spec.serverCert.san（逗号分隔的 IP:/DNS: 条目）。

    非法条目直接拒绝（声明期失败优于启动时静默忽略）；返回去重后的
    (kind, value) 列表，供证书生成与漂移比较共用。
    """
    entries: list[tuple[str, str]] = []
    for raw in (part.strip() for part in spec.split(",")):
        if not raw:
            continue
        kind, sep, value = raw.partition(":")
        kind, value = kind.strip().upper(), value.strip()
        if not sep or not value:
            raise ValueError(f"invalid san entry {raw!r}: expected IP:<addr> or DNS:<name>")
        if kind == "IP":
            try:
                value = str(ipaddress.ip_address(value))
            except ValueError as exc:
                raise ValueError(f"invalid san IP {raw!r}") from exc
        elif kind == "DNS":
            if not _DNS_RE.fullmatch(value):
                raise ValueError(f"invalid san DNS name {raw!r}")
        else:
            raise ValueError(f"invalid san entry {raw!r}: kind must be IP or DNS")
        if (kind, value) in entries:
            raise ValueError(f"duplicate san entry {raw!r}")
        entries.append((kind, value))
    if not entries:
        raise ValueError("san must contain at least one IP: or DNS: entry")
    return entries


class NetworkingSpec(BaseModel):
    """spec.networking：PXE 部署网络声明（dnsmasq.conf 的唯一权威来源）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    interface: str = Field(alias="interface", min_length=1, max_length=15)
    subnet: str = Field(alias="subnet")
    dhcp_range: str = Field(alias="dhcpRange")
    gateway: str = Field(alias="gateway")
    dns: str = Field(alias="dns")

    @field_validator("interface")
    @classmethod
    def _check_interface(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ch.isspace() for ch in value):
            raise ValueError("interface must be a non-empty name without whitespace")
        return value

    @field_validator("subnet")
    @classmethod
    def _check_subnet(cls, value: str) -> str:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ValueError("subnet must be a CIDR such as 192.168.80.0/24") from exc
        if network.version != 4:
            raise ValueError("subnet must be IPv4 (dnsmasq netmask is IPv4)")
        return value

    @field_validator("gateway", "dns")
    @classmethod
    def _check_ip(cls, value: str) -> str:
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError(f"not an IP address: {value}") from exc
        return value

    @model_validator(mode="after")
    def _check_network_consistency(self) -> "NetworkingSpec":
        network = ipaddress.ip_network(self.subnet, strict=False)
        gateway = ipaddress.ip_address(self.gateway)
        if gateway not in network:
            raise ValueError(f"gateway {self.gateway} is outside subnet {self.subnet}")
        parts = [part.strip() for part in self.dhcp_range.split(",")]
        if len(parts) != 2:
            raise ValueError("dhcpRange must be a start,end IP pair")
        try:
            start, end = (ipaddress.ip_address(part) for part in parts)
        except ValueError as exc:
            raise ValueError("dhcpRange must be a start,end IP pair") from exc
        if start.version != 4 or end.version != 4:
            raise ValueError("dhcpRange must be IPv4")
        if start > end:
            raise ValueError("dhcpRange start must not exceed end")
        if start not in network or end not in network:
            raise ValueError(f"dhcpRange {self.dhcp_range} is outside subnet {self.subnet}")
        reserved = {network.network_address, network.broadcast_address}
        if start in reserved or end in reserved:
            raise ValueError("dhcpRange must not include the subnet network or broadcast address")
        return self


class PkiSpec(BaseModel):
    """spec.pki：组件 PKI 与 bootstrap token 策略。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    bootstrap_token_ttl_minutes: int = Field(60, alias="bootstrapTokenTtlMinutes", ge=1, le=1440)
    bootstrap_token_max_ttl_minutes: int = Field(1440, alias="bootstrapTokenMaxTtlMinutes", ge=1, le=1440)
    bootstrap_token_max_uses: int = Field(100, alias="bootstrapTokenMaxUses", ge=1, le=1000)
    component_cert_days: int = Field(90, alias="componentCertDays", ge=1, le=3650)
    renew_threshold: float = Field(0.2, alias="renewThreshold", gt=0, le=1)
    # 必须小于 clients.crl 的 nextUpdate（7 天）；重签后 nginx reload 生效
    crl_refresh_hours: int = Field(12, alias="crlRefreshHours", ge=1, le=167)

    @model_validator(mode="after")
    def _check_token_ttl(self) -> "PkiSpec":
        if self.bootstrap_token_ttl_minutes > self.bootstrap_token_max_ttl_minutes:
            raise ValueError("bootstrapTokenTtlMinutes must not exceed bootstrapTokenMaxTtlMinutes")
        return self


class ServerCertSpec(BaseModel):
    """spec.serverCert：TOFU 引导链自签服务器证书（nginx 443 使用）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    san: str = Field("IP:127.0.0.1,DNS:localhost", alias="san")
    days: int = Field(3650, alias="days", ge=1, le=36500)
    # 声明变化时是否自动重签：默认关闭——轮换会改变 TOFU 指纹，须显式开启
    rotate_on_change: bool = Field(False, alias="rotateOnChange")

    @field_validator("san")
    @classmethod
    def _check_san(cls, value: str) -> str:
        parse_san_entries(value)
        return value


class BootSpec(BaseModel):
    """spec.boot：iPXE 引导行为。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    default_arch: str = Field("x86_64", alias="defaultArch", min_length=1, max_length=32)
    menu_timeout_ms: int = Field(5000, alias="menuTimeoutMs", ge=0)
    auto_boot_timeout_ms: int = Field(1, alias="autoBootTimeoutMs", ge=0)

    @field_validator("default_arch")
    @classmethod
    def _check_arch(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]+", value):
            raise ValueError("defaultArch must use letters, digits or underscore")
        return value


class AuthSpec(BaseModel):
    """spec.auth：管理面证书与会话/自动化 token 策略。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    session_ttl_hours: int = Field(12, alias="sessionTtlHours", ge=1, le=168)
    admin_cert_days: int = Field(365, alias="adminCertDays", ge=1, le=3650)
    user_cert_days_max: int = Field(3650, alias="userCertDaysMax", ge=1, le=36500)
    token_ttl_hours: int = Field(24, alias="tokenTtlHours", ge=1, le=8760)
    token_max_ttl_hours: int = Field(720, alias="tokenMaxTtlHours", ge=1, le=8760)

    @model_validator(mode="after")
    def _check_cert_and_token_bounds(self) -> "AuthSpec":
        if self.admin_cert_days > self.user_cert_days_max:
            raise ValueError("adminCertDays must not exceed userCertDaysMax")
        if self.token_ttl_hours > self.token_max_ttl_hours:
            raise ValueError("tokenTtlHours must not exceed tokenMaxTtlHours")
        return self


class RegistrationSpec(BaseModel):
    """spec.registration：设备注册窗口上限（TTL 下限 1 分钟是协议语义，不入声明）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    window_max_ttl_minutes: int = Field(60, alias="windowMaxTtlMinutes", ge=1, le=1440)
    window_max_devices: int = Field(1000, alias="windowMaxDevices", ge=1, le=100000)


class CredentialsSpec(BaseModel):
    """spec.credentials：DHHC-1 轮换与推送对账（0 = 关闭对应后台任务）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    rotation_days: float = Field(90, alias="rotationDays", ge=0, le=3650)
    reconcile_interval_seconds: float = Field(
        60, alias="reconcileIntervalSeconds", ge=0, le=86400)


class Spec(BaseModel):
    """spec：networking 必填，其余块带默认值；未知字段拒绝。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    networking: NetworkingSpec
    pki: PkiSpec = Field(default_factory=PkiSpec)
    server_cert: ServerCertSpec = Field(default_factory=ServerCertSpec, alias="serverCert")
    boot: BootSpec = Field(default_factory=BootSpec)
    auth: AuthSpec = Field(default_factory=AuthSpec)
    registration: RegistrationSpec = Field(default_factory=RegistrationSpec)
    credentials: CredentialsSpec = Field(default_factory=CredentialsSpec)
    agent_timeout_sec: float = Field(10, alias="agentTimeoutSec", gt=0, le=600)


class MetadataSpec(BaseModel):
    """metadata：控制面名称（声明身份）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(alias="name", min_length=1, max_length=63)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _METADATA_NAME_RE.fullmatch(value):
            raise ValueError("name must use lowercase letters, digits, dot or dash")
        return value


class ControlPlaneConfiguration(BaseModel):
    """控制面配置根（apiVersion/kind/metadata/spec）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api_version: Literal["kurrent.io/v1"] = Field(CONFIG_API_VERSION, alias="apiVersion")
    kind: Literal["ControlPlaneConfiguration"] = "ControlPlaneConfiguration"
    metadata: MetadataSpec
    spec: Spec


def load_config_file(path: str | Path) -> ControlPlaneConfiguration:
    """加载并校验指定文件；缺失/语法/校验失败抛 RuntimeError（启动即失败）。"""
    cfg_path = Path(path)
    if not cfg_path.is_file():
        raise RuntimeError(
            f"missing control plane configuration: {cfg_path} "
            "(copy control_plane/kurrent.yaml.example to control_plane/kurrent.yaml)")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RuntimeError(f"invalid control plane configuration {cfg_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"invalid control plane configuration {cfg_path}: root must be a YAML mapping")
    try:
        return ControlPlaneConfiguration.model_validate(data)
    except ValidationError as exc:
        raise RuntimeError(f"invalid control plane configuration {cfg_path}: {exc}") from exc


def effective_config(cfg: ControlPlaneConfiguration) -> dict:
    """生效配置（默认值已注入；camelCase alias），供 config show/状态端点展示。"""
    return cfg.model_dump(by_alias=True, mode="json", exclude_none=True)


def config_hash(cfg: ControlPlaneConfiguration) -> str:
    """生效配置的规范化 SHA-256（排序键、紧凑 JSON；用于确认运行态加载了哪份声明）。"""
    payload = json.dumps(effective_config(cfg), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
