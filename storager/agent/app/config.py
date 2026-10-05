"""节点声明式配置加载。

配置来源：storager/kurrent.yaml（apiVersion/kind/metadata/spec 单文件节点权威配置，
kurrent CLI 在 join 时生成与更新，手工业务键编辑此文件）；
容器内挂载路径固定 /etc/kurrent/kurrent.yaml（KURRENT_CONFIG_FILE 可覆盖，测试注入用）。

分层职责：kurrent.yaml 只声明节点级业务配置（身份/加入凭据/数据面参数），
容器内路径（pki/cp-ca/日志/缓存等挂载目标）属于部署清单 docker-compose.yml 职责，
此处以模块常量固化（与 compose 挂载目标一致，单处维护）。

校验语义：未知字段拒绝（extra="forbid"）、必填缺失报错、默认值注入——
配置错误在启动即失败，杜绝 .env 自由键值的静默偏差（ .env 仅保留
docker-compose 插值键，业务配置全部收敛到此文件）。
"""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# compose 卷挂载路径（storager/kurrent.yaml → 容器内，只读）
DEFAULT_CONFIG_FILE = "/etc/kurrent/kurrent.yaml"

# ── 容器内路径/拓扑常量（部署清单 docker-compose.yml 职责，不在 yml 声明）──
DEFAULT_DISK_DIR = "/home/iscsi_img"                         # 容器内盘目录挂载点（compose: ${KURRENT_DISK_DIR} → 本路径）
DEFAULT_LOG_FILE = "/var/log/kurrent/ops.jsonl"               # compose: ../agent/logs → /var/log/kurrent
DEFAULT_NVMET_CACHE_FILE = "/var/log/kurrent/nvmet-credentials.json"
DEFAULT_ISCSI_CONTAINER = "storager-iscsi"                     # compose 服务名（docker.sock 调度，仅 stgt|lio）
DEFAULT_PKI_DIR = "/etc/kurrent/pki"                           # compose: ${KURRENT_AGENT_PKI_HOST} → /etc/kurrent/pki
DEFAULT_CP_CA = "/etc/kurrent/cp-ca.crt"                       # compose: server.crt → /etc/kurrent/cp-ca.crt:ro
DEFAULT_NVMET_HOST_URL = "https://host.docker.internal:4841"    # 内部组件通讯地址（同机固定拓扑，部署事实）
DEFAULT_BOOTSTRAP_TOKEN_FILE = "/etc/kurrent/bootstrap/agent.token"  # 通用引导凭据（TTL 内可复用，
                                                                     # compose: ../bootstrap → /etc/kurrent/bootstrap，join 写入）
DEFAULT_NVMET_BOOTSTRAP_TOKEN_FILE = "/etc/kurrent/bootstrap/nvmet-host.token"  # nvmet-host 派生凭据（agent enroll 按能力
                                                                                 # 下发落盘，供 nvmet-host 容器引导）

BackendType = Literal["nvmet", "stgt", "lio"]


class AgentSpec(BaseModel):
    """spec.agent：数据面服务配置（后端/存储路径/命名空间/上报地址）。

    diskDir 语义 = 宿主存储路径（数据目录声明）；
    容器内挂载点由 compose 决定（DEFAULT_DISK_DIR），两者经 compose 卷挂载关联。
    """
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    backend: BackendType = Field("nvmet", alias="backend")
    advertise_url: str = Field("", alias="advertiseUrl")  # 控制面可达地址（enroll 自动登记写入 agents.yml base_url）
    disk_dir: str = Field(alias="diskDir")  # 宿主存储路径（数据目录，compose 挂载源经 .env 插值同步）
    nqn_base: str = Field(alias="nqnBase")  # 盘标识命名空间（权威：NQN，IQN 由此派生）


class ControlPlaneSpec(BaseModel):
    """spec.controlPlane：控制面入口。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    url: str = Field(alias="url")


class MetadataSpec(BaseModel):
    """metadata：节点身份（name = agent_id，由 kurrent join 自动生成，与组件证书 CN 一致；
    remark = 备注，人类识别用，随首次 enroll 登记进控制面台账）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(alias="name")
    remark: str = Field("", alias="remark")


class NodeSpec(BaseModel):
    """spec：agent / controlPlane 子块（一次性 token 在独立凭据文件，不在 yml）。"""
    model_config = ConfigDict(extra="forbid")

    agent: AgentSpec
    control_plane: ControlPlaneSpec = Field(alias="controlPlane")


class NodeConfiguration(BaseModel):
    """节点级配置根（apiVersion/kind/metadata/spec）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api_version: str = Field("kurrent.io/v1", alias="apiVersion")
    kind: str = "NodeConfiguration"
    metadata: MetadataSpec
    spec: NodeSpec


def load_config(path: str | None = None) -> NodeConfiguration:
    """加载并校验节点配置；文件缺失/校验失败抛错阻断启动（启动即失败）。"""
    cfg_path = Path(path or os.environ.get("KURRENT_CONFIG_FILE") or DEFAULT_CONFIG_FILE)
    if not cfg_path.is_file():
        raise RuntimeError(
            f"missing node configuration: {cfg_path} "
            "(run 'kurrent join' to generate storager/kurrent.yaml)")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        return NodeConfiguration.model_validate(data)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"invalid node configuration {cfg_path}: {exc}") from exc
    except ValidationError as exc:
        raise RuntimeError(f"invalid node configuration {cfg_path}: {exc}") from exc


# 模块级单例：import app.config 即加载（main.py / pki_client.py 共同消费）
CONFIG = load_config()
