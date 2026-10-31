"""控制面声明式配置加载与运行时设置。

配置来源：control_plane/kurrent.yaml（apiVersion/kind/metadata/spec 单文件权威配置，
从 kurrent.yaml.example 复制后手工编辑）；容器内挂载路径固定 /etc/kurrent/kurrent.yaml
（KURRENT_CONFIG_FILE 可覆盖，测试注入用）。

分层职责：kurrent.yaml 只声明控制面业务策略（PXE 部署网络/组件 PKI 策略/
服务器证书/引导行为/管理面与会话策略/注册窗口/凭据轮换/数据面参数），容器内文件
路径与容器名属部署清单 docker-compose.yml 职责，此处以模块常量固化；运行时状态
（注册窗口/强制设备认证等）在 state/settings.json，不属声明配置。

schema 在 config_schema.py（pydantic v2，单一权威）：未知字段拒绝（extra="forbid"）、
必填缺失报错、默认值注入、apiVersion/kind 锁定、networking 跨字段语义校验——
配置错误在 kurrent init 校验阶段即失败，杜绝 env 自由键值的静默偏差。
"""

import os
from pathlib import Path

from .config_schema import (ControlPlaneConfiguration, config_hash,
                            effective_config, load_config_file)

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
DEFAULT_TFTP_DIR = "/var/tftp"                          # 与 dnsmasq 容器 tftp-root 同一宿主目录（只读）
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
# 注册窗口 TTL 下限（分钟）：窗口只在部署期短开，不可配置为永久
REGISTRATION_WINDOW_TTL_MIN_MINUTES = 1
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
# 自动化 token scope：admin=全部 / readonly=只读 / enroll=签发节点引导 token
TOKEN_SCOPES = ("admin", "readonly", "enroll")


def load_config(path: str | None = None) -> ControlPlaneConfiguration:
    """加载并校验控制面配置；文件缺失/校验失败抛错阻断启动（启动即失败）。"""
    return load_config_file(path or os.environ.get("KURRENT_CONFIG_FILE") or DEFAULT_CONFIG_FILE)


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
    "dnsmasq_conf": "DEFAULT_DNSMASQ_CONF",
    "tftp_dir": "DEFAULT_TFTP_DIR",
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
    "auto_boot_timeout_ms": ("boot", "auto_boot_timeout_ms"),
    "agent_timeout": ("agent_timeout_sec",),
    "cert_san": ("server_cert", "san"),
    "cert_days": ("server_cert", "days"),
    "cert_rotate_on_change": ("server_cert", "rotate_on_change"),
    "session_ttl_hours": ("auth", "session_ttl_hours"),
    "admin_cert_days": ("auth", "admin_cert_days"),
    "user_cert_days_max": ("auth", "user_cert_days_max"),
    "token_ttl_hours_default": ("auth", "token_ttl_hours"),
    "token_ttl_hours_max": ("auth", "token_max_ttl_hours"),
    "registration_window_max_ttl_minutes": ("registration", "window_max_ttl_minutes"),
    "registration_window_max_devices": ("registration", "window_max_devices"),
    "bootstrap_token_ttl_minutes_default": ("pki", "bootstrap_token_ttl_minutes"),
    "bootstrap_token_ttl_minutes_max": ("pki", "bootstrap_token_max_ttl_minutes"),
    "bootstrap_token_max_uses_upper": ("pki", "bootstrap_token_max_uses"),
    "crl_refresh_hours": ("pki", "crl_refresh_hours"),
    "renew_threshold": ("pki", "renew_threshold"),
}


class Settings:
    """控制面运行时设置：字段动态读模块常量 / yml spec / env（属性访问即解析，不缓存）。

    测试重定向 DEFAULT_* 常量或替换 KURRENT_CONFIG_FILE 后无需重建实例即生效。
    两个凭据后台参数保留 env 覆盖（KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL /
    KURRENT_CP_CREDENTIAL_ROTATION_DAYS），供测试与临时运维；声明值是缺省来源。
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
        if name == "session_ttl_seconds":
            return int(CONFIG.spec.auth.session_ttl_hours) * 3600
        if name == "credential_reconcile_interval":
            raw = os.getenv("KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL")
            if raw is None:
                return float(CONFIG.spec.credentials.reconcile_interval_seconds)
            try:
                return float(raw)
            except ValueError:
                return float(CONFIG.spec.credentials.reconcile_interval_seconds)
        if name == "credential_rotation_days":
            raw = os.getenv("KURRENT_CP_CREDENTIAL_ROTATION_DAYS")
            if raw is None:
                return float(CONFIG.spec.credentials.rotation_days)
            try:
                return float(raw)
            except ValueError:
                return float(CONFIG.spec.credentials.rotation_days)
        if name == "insecure":
            # 开发模式开关（唯一放行口；生产严禁）：KURRENT_CP_INSECURE=1 显式开启
            return os.getenv("KURRENT_CP_INSECURE", "") == "1"
        if name == "openapi_enabled":
            # 交互式 API 文档默认关闭（避免未鉴权枚举接口）；开发环境显式开启
            return os.getenv("KURRENT_CP_OPENAPI", "") == "1"
        if name == "config_hash":
            return config_hash(CONFIG)
        raise AttributeError(name)


settings = Settings()
