"""控制面声明式配置加载。

配置来源：control_plane/kurrent.yaml（apiVersion/kind/metadata/spec 单文件权威配置，
从 kurrent.yaml.example 复制后手工编辑）；容器内挂载路径固定 /etc/kurrent/kurrent.yaml
（KURRENT_CONFIG_FILE 可覆盖，测试注入用）。

分层职责：kurrent.yaml 只声明控制面业务策略（PXE 部署网络/组件 PKI 策略/
服务器证书/引导行为/数据面参数），容器内文件路径与容器名属部署清单 docker-compose.yml
职责，此处以模块常量固化（与 compose 挂载目标一致，单处维护）；运行时状态（注册窗口/
强制设备认证等）在 state/settings.json，不属声明配置。

校验语义：未知字段拒绝（extra="forbid"）、必填缺失报错、默认值注入——
配置错误在启动即失败，杜绝 env 自由键值的静默偏差。
"""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# compose 卷挂载路径（control_plane/kurrent.yaml → 容器内，只读）
DEFAULT_CONFIG_FILE = "/etc/kurrent/kurrent.yaml"

# ── 容器内路径/拓扑常量（部署清单 docker-compose.yml 职责，不在 yml 声明）──
DEFAULT_AGENTS_FILE = "/app/config/agents.yml"
DEFAULT_WORKERS_FILE = "/app/state/workers.yml"
DEFAULT_DEVICES_FILE = "/app/state/devices.yml"
DEFAULT_OPERATIONS_FILE = "/app/state/operations.jsonl"
DEFAULT_SETTINGS_FILE = "/app/state/settings.json"
DEFAULT_DNSMASQ_HOSTS_FILE = "/app/dnsmasq/dhcp-hosts.conf"
DEFAULT_DNSMASQ_CONF = "/app/dnsmasq/dnsmasq.conf"      # 由控制面启动时按 spec.networking 生成（yml 权威）
DEFAULT_CERT_DIR = "/app/state/certs"                   # TOFU 自签服务器证书（nginx 只读挂载）
DEFAULT_CREDENTIALS_FILE = "/app/state/credentials.yml"
DEFAULT_PKI_DIR = "/app/state/pki"                      # 三分根 PKI（nginx 只读挂载 nginx/ 信任包）
DEFAULT_USERS_FILE = "/app/state/users.yml"             # 管理用户目录：CN → 组（证书只证明 CN）
DEFAULT_SESSIONS_FILE = "/app/state/sessions.yml"       # 会话登记：sha256(jti) → 身份/组/到期（删即失效）
DEFAULT_AUDIT_FILE = "/app/state/audit.jsonl"           # 认证/授权审计（与操作审计分离）
DEFAULT_CONTROL_PLANE_COMPONENT = "control-plane"       # 控制面自身 client cert 别名（CN=control-plane）
DEFAULT_MASTERS_FILE = "/app/state/masters.yml"
DEFAULT_CREDENTIAL_PUSH_FILE = "/app/state/credential-push.yml"  # 凭据推送状态台账（对账/投影用）
DEFAULT_ADMIN_CONF = "/app/state/admin.conf"               # 管理凭据文件（kurrent 原生扁平 YAML，CLI 自动发现；证书路径为相对引用）
DEFAULT_SESSION_KEY_FILE = "/app/state/session.key"        # 会话签名密钥（运行时秘密，0600）

# ── 协议契约/安全边界（代码层常量，不收敛 yml）──
# 注册窗口 TTL 硬上限（分钟）：不可配置为永久，注册只在窗口期
REGISTRATION_WINDOW_TTL_MAX_MINUTES = 60
REGISTRATION_WINDOW_TTL_MIN_MINUTES = 1
# 注册窗口放行额度上限（台）：省略 = 不限（仍受 TTL 约束），上限防误填大数等于关掉额度
REGISTRATION_WINDOW_MAX_DEVICES_UPPER = 1000
# 挑战 nonce 短 TTL（秒）：防重放，引导期无可靠时钟不能依赖时间戳
CHALLENGE_NONCE_TTL_SECONDS = 60
# nginx mTLS 校验通过后透传的客户端证书 DN 头（组件 renew + 管理面身份）
CLIENT_CERT_DN_HEADER = "x-client-cert-dn"
# 同理透传的序列号头（renew 拒绝已吊销证书的应用侧兜底，早于 CRL reload 生效）
CLIENT_CERT_SERIAL_HEADER = "x-client-cert-serial"
# issuer → 主体类（仅 mTLS SUCCESS 时由 nginx 注入；见 nginx.conf 的 $valid_client_class）
CLIENT_CLASS_HEADER = "x-client-class"
# ── 管理面身份常量：证书 CN=用户名（组来自 state/users.yml）；admin 为自举身份 ──
ADMIN_CN = "admin"
ADMIN_GROUP = "admin"
VIEWER_GROUP = "viewer"
ADMIN_CERT_DAYS = 365                     # 管理证书 TTL（天）
USER_CERT_DAYS_MAX = 3650                 # 人类证书 TTL 上限（天；即时失效靠 CRL，不靠短 TTL）
SESSION_TTL_HOURS = 12                    # 会话 TTL（小时；剩余不足一半才滑动续期）
SESSION_TTL_SECONDS = SESSION_TTL_HOURS * 3600
# 自动化 token scope：admin=全部 / readonly=只读 / enroll=签发节点引导 token
TOKEN_SCOPES = ("admin", "readonly", "enroll")
TOKEN_TTL_HOURS_DEFAULT = 24
TOKEN_TTL_HOURS_MAX = 720                 # 30 天上限
# CRL 定期重签周期（小时）：必须小于 CRL next_update（7 天），否则长跑后 nginx 会因
# CRL 过期拒绝全部客户端证书。重签后由 nginx reload 生效。
CRL_REFRESH_HOURS = 12
# bootstrap token：短 TTL（分钟）+ 一次性（max_uses 默认 1；旧条目无字段 = 不限）
BOOTSTRAP_TOKEN_TTL_MINUTES_DEFAULT = 60
BOOTSTRAP_TOKEN_TTL_MINUTES_MAX = 1440    # 24 小时上限
BOOTSTRAP_TOKEN_MAX_USES_UPPER = 100
# DHHC-1 定期轮换周期（天）：0 = 关闭。轮换由凭据对账线程执行：先推送到全部目标
# Agent，全部送达才在台账切换；部分失败则回推旧密钥，下个周期重试。
CREDENTIAL_ROTATION_DAYS = 90


class NetworkingSpec(BaseModel):
    """spec.networking：PXE 部署网络声明。

    掩码由 subnet（CIDR）推导，dnsmasq.conf 由控制面启动时按本块生成——yml 是权威。
    """
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    interface: str = Field(alias="interface")          # 绑定网卡（dnsmasq interface= + bind-interfaces）
    subnet: str = Field(alias="subnet")                # 服务网段 CIDR（如 192.168.80.0/24）
    dhcp_range: str = Field(alias="dhcpRange")         # DHCP 池起止（如 192.168.80.50,192.168.80.100）
    gateway: str = Field(alias="gateway")              # dhcp-option=3
    dns: str = Field(alias="dns")                      # dhcp-option=6


class PkiSpec(BaseModel):
    """spec.pki：组件 PKI 策略（内部 CA + bootstrap token 引导 + 证书轮换）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    bootstrap_token_ttl_minutes: int = Field(60, alias="bootstrapTokenTtlMinutes", ge=1, le=1440)
    component_cert_days: int = Field(90, alias="componentCertDays", ge=1)
    renew_threshold: float = Field(0.2, alias="renewThreshold", gt=0, le=1)


class ServerCertSpec(BaseModel):
    """spec.serverCert：TOFU 引导链服务器证书（自签，启动时幂等生成，nginx 只读挂载）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    san: str = Field("IP:127.0.0.1,DNS:localhost", alias="san")
    days: int = Field(3650, alias="days", ge=1)


class BootSpec(BaseModel):
    """spec.boot：iPXE 引导行为。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    default_arch: str = Field("x86_64", alias="defaultArch")
    menu_timeout_ms: int = Field(5000, alias="menuTimeoutMs", ge=0)
    auto_boot_timeout_sec: int = Field(1, alias="autoBootTimeoutSec", ge=0)


class Spec(BaseModel):
    """spec：networking 必填；pki/serverCert/boot/agentTimeoutSec 带默认值。

    NQN 命名域不在本文件声明：盘 NQN 与 Host NQN 的 base 均来自节点侧
    spec.agent.nqnBase（节点权威，enroll 经 capabilities.base_nqn 上报）。
    """
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    networking: NetworkingSpec
    pki: PkiSpec = Field(default_factory=PkiSpec)
    server_cert: ServerCertSpec = Field(default_factory=ServerCertSpec, alias="serverCert")
    boot: BootSpec = Field(default_factory=BootSpec)
    agent_timeout_sec: float = Field(10, alias="agentTimeoutSec", gt=0)


class MetadataSpec(BaseModel):
    """metadata：控制面名称（声明身份）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(alias="name")


class ControlPlaneConfiguration(BaseModel):
    """控制面配置根（apiVersion/kind/metadata/spec）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api_version: str = Field("kurrent.io/v1", alias="apiVersion")
    kind: Literal["ControlPlaneConfiguration"] = "ControlPlaneConfiguration"
    metadata: MetadataSpec
    spec: Spec


def load_config(path: str | None = None) -> ControlPlaneConfiguration:
    """加载并校验控制面配置；文件缺失/校验失败抛错阻断启动（启动即失败）。"""
    cfg_path = Path(path or os.environ.get("KURRENT_CONFIG_FILE") or DEFAULT_CONFIG_FILE)
    if not cfg_path.is_file():
        raise RuntimeError(
            f"missing control plane configuration: {cfg_path} "
            "(copy control_plane/kurrent.yaml.example to control_plane/kurrent.yaml)")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        return ControlPlaneConfiguration.model_validate(data)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"invalid control plane configuration {cfg_path}: {exc}") from exc
    except ValidationError as exc:
        raise RuntimeError(f"invalid control plane configuration {cfg_path}: {exc}") from exc


# 模块级单例：import app.config 即加载（main.py / stores.py / routers 共同消费）
CONFIG = load_config()

# ── Settings 字段 → 取值来源映射（属性访问即解析，不缓存）──
_PATH_FIELDS = {
    "agents_file": "DEFAULT_AGENTS_FILE",
    "workers_file": "DEFAULT_WORKERS_FILE",
    "devices_file": "DEFAULT_DEVICES_FILE",
    "operations_file": "DEFAULT_OPERATIONS_FILE",
    "settings_file": "DEFAULT_SETTINGS_FILE",
    "dnsmasq_hosts_file": "DEFAULT_DNSMASQ_HOSTS_FILE",
    "cert_dir": "DEFAULT_CERT_DIR",
    "credentials_file": "DEFAULT_CREDENTIALS_FILE",
    "credential_push_file": "DEFAULT_CREDENTIAL_PUSH_FILE",
    "pki_dir": "DEFAULT_PKI_DIR",
    "users_file": "DEFAULT_USERS_FILE",
    "sessions_file": "DEFAULT_SESSIONS_FILE",
    "audit_file": "DEFAULT_AUDIT_FILE",
    "masters_file": "DEFAULT_MASTERS_FILE",
    "admin_conf": "DEFAULT_ADMIN_CONF",
    "session_key_file": "DEFAULT_SESSION_KEY_FILE",
}
_STR_FIELDS = {
    "control_plane_component": "DEFAULT_CONTROL_PLANE_COMPONENT",
    "admin_cn": "ADMIN_CN",
}
_SPEC_FIELDS = {
    "default_arch": ("boot", "default_arch"),
    "boot_menu_timeout": ("boot", "menu_timeout_ms"),
    "auto_boot_timeout": ("boot", "auto_boot_timeout_sec"),
    "agent_timeout": ("agent_timeout_sec",),
    "cert_san": ("server_cert", "san"),
    "cert_days": ("server_cert", "days"),
}


class Settings:
    """控制面运行时设置：字段动态读模块常量 / yml spec / env（属性访问即解析，不缓存）。

    测试重定向 DEFAULT_* 常量或替换 KURRENT_CONFIG_FILE 后无需重建实例即生效。
    """

    def __getattr__(self, name: str):
        if name in _PATH_FIELDS:
            return Path(globals()[_PATH_FIELDS[name]])
        if name in _STR_FIELDS:
            return globals()[_STR_FIELDS[name]]
        if name in _SPEC_FIELDS:
            node = CONFIG.spec
            for part in _SPEC_FIELDS[name]:
                node = getattr(node, part)
            return node
        if name == "insecure":
            # 开发模式开关（唯一放行口；生产严禁）：KURRENT_CP_INSECURE=1 显式开启
            return os.getenv("KURRENT_CP_INSECURE", "") == "1"
        if name == "openapi_enabled":
            # 交互式 API 文档默认关闭（避免未鉴权枚举接口）；开发环境显式开启
            return os.getenv("KURRENT_CP_OPENAPI", "") == "1"
        if name == "credential_reconcile_interval":
            # 凭据对账周期（秒）：控制面→Agent 推送的幂等重放间隔；0 = 禁用（测试环境）
            return float(os.getenv("KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL", "60"))
        if name == "credential_rotation_days":
            # DHHC-1 定期轮换周期（天）；<=0 关闭（测试环境）
            return float(os.getenv("KURRENT_CP_CREDENTIAL_ROTATION_DAYS",
                                   str(CREDENTIAL_ROTATION_DAYS)))
        raise AttributeError(name)


settings = Settings()
