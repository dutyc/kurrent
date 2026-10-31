"""节点声明式配置加载。

配置来源：storager/kurrent.yaml（apiVersion/kind/metadata/spec 单文件节点权威配置，
kurrent CLI 在 join 时生成与更新，手工业务键编辑此文件）；
容器内挂载路径固定 /etc/kurrent/kurrent.yaml（KURRENT_CONFIG_FILE 可覆盖，测试注入用）。

分层职责：kurrent.yaml 只声明节点级业务配置（身份/加入凭据/数据面参数），
容器内路径（pki/cp-ca/日志/缓存等挂载目标）属于部署清单 docker-compose.yml 职责，
此处以模块常量固化（与 compose 挂载目标一致，单处维护）。

schema 在 config_schema.py（pydantic v2，单一权威）：未知字段拒绝（extra="forbid"）、
必填缺失报错、默认值注入、apiVersion/kind 锁定、身份/路径/NQN 语义校验——
配置错误在 kurrent join 预校验阶段即失败，杜绝 .env 自由键值的静默偏差
（.env 仅保留 docker-compose 插值键）。
"""

import os

from .config_schema import (NodeConfiguration, effective_config,
                            load_config_file)

# compose 卷挂载路径（storager/kurrent.yaml → 容器内，只读）
DEFAULT_CONFIG_FILE = "/etc/kurrent/kurrent.yaml"

# ── 容器内路径/拓扑常量（部署清单 docker-compose.yml 职责，不在 yml 声明）──
DEFAULT_DISK_DIR = "/home/iscsi_img"                         # 容器内盘目录挂载点（compose: ${KURRENT_DISK_DIR} → 本路径）
DEFAULT_LOG_FILE = "/var/log/kurrent/ops.jsonl"               # compose: ../agent/logs → /var/log/kurrent
DEFAULT_NVMET_CACHE_FILE = "/var/log/kurrent/nvmet-credentials.json"
DEFAULT_ISCSI_CONTAINER = "storager-iscsi"                     # compose 服务名（docker.sock 调度，仅 stgt|lio）
DEFAULT_PKI_DIR = "/etc/kurrent/pki"                           # compose: ${KURRENT_AGENT_PKI_HOST} → /etc/kurrent/pki
DEFAULT_CP_CA = "/etc/kurrent/cp-ca.crt"                       # compose: server.crt → /etc/kurrent/cp-ca.crt:ro
DEFAULT_NVMET_HOST_SOCKET = "/run/kurrent/nvmet/nvmet-host.sock"  # 共享卷 UDS（同机固定拓扑，无 TCP 面）
DEFAULT_BOOTSTRAP_TOKEN_FILE = "/etc/kurrent/bootstrap/agent.token"  # 通用引导凭据（TTL 内可复用，
                                                                     # compose: ../bootstrap → /etc/kurrent/bootstrap，join 写入）
DEFAULT_NVMET_BOOTSTRAP_TOKEN_FILE = "/etc/kurrent/bootstrap/nvmet-host.token"  # nvmet-host 派生凭据（agent enroll 按能力
                                                                                 # 下发落盘，供 nvmet-host 容器引导）


def load_config(path: str | None = None) -> NodeConfiguration:
    """加载并校验节点配置；文件缺失/校验失败抛错阻断启动（启动即失败）。"""
    return load_config_file(path or os.environ.get("KURRENT_CONFIG_FILE") or DEFAULT_CONFIG_FILE)


# 模块级单例：import app.config 即加载（main.py / pki_client.py 共同消费）
CONFIG = load_config()
