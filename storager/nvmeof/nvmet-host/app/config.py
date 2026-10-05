"""nvmet-host 组件配置加载：与 storager-agent 同源。

读取同一份 storager/kurrent.yaml（节点级单文件权威配置），本组件消费
spec.controlPlane（enroll 入口）；spec.agent 块由 agent 组件消费，此处以
同构模型校验（不消费但节点级配置整体有效——extra="forbid" 语义两侧一致）。

分层职责：监听地址/盘目录/pki/cp-ca 等部署细节与一次性引导凭据
（bootstrap token 文件）均属部署清单 compose 职责，以模块常量固化。

校验语义与 agent 侧一致：未知字段拒绝（extra="forbid"）、必填缺失报错、默认值注入。
"""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# compose 卷挂载路径（storager/kurrent.yaml → 容器内，只读）
DEFAULT_CONFIG_FILE = "/etc/kurrent/kurrent.yaml"

# ── 部署细节常量（docker-compose.yml 职责，不在 yml 声明）──
DEFAULT_HOST_ADDR = "0.0.0.0"              # 监听地址（host 网络，agent 经 DEFAULT_NVMET_HOST_URL 访问）
DEFAULT_HOST_PORT = 4841
DEFAULT_HOST_DISK_DIR = "/srv/nvmet-disks"   # compose: 宿主盘目录 → 本路径（device_path 重拼前缀）
DEFAULT_PKI_DIR = "/etc/kurrent/pki"         # compose: ${KURRENT_NVMET_PKI_HOST} → /etc/kurrent/pki
DEFAULT_CP_CA = "/etc/kurrent/cp-ca.crt"     # compose: server.crt → /etc/kurrent/cp-ca.crt:ro
DEFAULT_BOOTSTRAP_TOKEN_FILE = "/etc/kurrent/bootstrap/nvmet-host.token"  # nvmet-host 派生引导凭据（agent enroll 按能力
                                                                           # 签发并落盘；compose: ../bootstrap → 本目录:ro）

BackendType = Literal["nvmet", "stgt", "lio"]


class AgentSpec(BaseModel):
    """spec.agent：数据面服务配置（agent 组件消费；本组件同构校验不消费）。

    diskDir 语义 = 宿主存储路径（与 agent 侧一致）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    backend: BackendType = Field("nvmet", alias="backend")
    advertise_url: str = Field("", alias="advertiseUrl")
    disk_dir: str = Field(alias="diskDir")
    nqn_base: str = Field(alias="nqnBase")


class ControlPlaneSpec(BaseModel):
    """spec.controlPlane：控制面入口（enroll/renew 的 nginx TLS 端点）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    url: str = Field(alias="url")


class MetadataSpec(BaseModel):
    """metadata：节点身份（name = agent_id，由 kurrent join 自动生成，与组件证书 CN 一致；
    remark = 备注，人类识别用，随首次 enroll 登记进控制面台账）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(alias="name")
    remark: str = Field("", alias="remark")


class NodeSpec(BaseModel):
    """spec：agent / controlPlane 子块（同构校验；token 在独立凭据文件）。"""
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
