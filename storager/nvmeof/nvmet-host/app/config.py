"""nvmet-host 组件配置加载：与 storager-agent 同源。

读取同一份 storager/kurrent.yaml（节点级单文件权威配置），本组件消费
spec.controlPlane（enroll 入口）；spec.agent 块由 agent 组件消费，此处以
同构模型校验（不消费但节点级配置整体有效——extra="forbid" 语义两侧一致）。

镜像构建上下文相互隔离（nvmet-host 镜像无法 COPY agent 代码），模型在此镜像内
保持同构；tests 中的 schema parity 用例锁定两侧 JSON Schema 与校验结果一致。

分层职责：监听地址/盘目录/pki/cp-ca 等部署细节与一次性引导凭据
（bootstrap token 文件）均属部署清单 compose 职责，以模块常量固化。

校验语义与 agent 侧一致：未知字段拒绝（extra="forbid"）、必填缺失报错、默认值注入、
apiVersion/kind 锁定、身份/路径/NQN 语义校验。
"""

import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator)

# compose 卷挂载路径（storager/kurrent.yaml → 容器内，只读）
DEFAULT_CONFIG_FILE = "/etc/kurrent/kurrent.yaml"

# ── 部署细节常量（docker-compose.yml 职责，不在 yml 声明）──
# 管理面只经共享卷 UDS 暴露（与 agent 同机固定拓扑）：无 TCP 监听，局域网不可达。
DEFAULT_HOST_SOCKET = "/run/kurrent/nvmet/nvmet-host.sock"
DEFAULT_HOST_DISK_DIR = "/srv/nvmet-disks"   # compose: 宿主盘目录 → 本路径（device_path 重拼前缀）
DEFAULT_PKI_DIR = "/etc/kurrent/pki"         # compose: ${KURRENT_NVMET_PKI_HOST} → /etc/kurrent/pki
DEFAULT_CP_CA = "/etc/kurrent/cp-ca.crt"     # compose: server.crt → /etc/kurrent/cp-ca.crt:ro
DEFAULT_BOOTSTRAP_TOKEN_FILE = "/etc/kurrent/bootstrap/nvmet-host.token"  # nvmet-host 派生引导凭据（agent enroll 按能力
                                                                           # 签发并落盘；compose: ../bootstrap → 本目录:ro）

CONFIG_API_VERSION = "kurrent.io/v1"
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")
_NQN_BASE_RE = re.compile(r"^nqn\.[0-9]{4}-[0-9]{2}\.[A-Za-z0-9][A-Za-z0-9.-]{0,222}$")

BackendType = Literal["nvmet", "stgt", "lio"]


def _check_url(value: str, field: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{field} must be an http(s) URL with a host, e.g. https://192.168.1.10")
    if any(ch.isspace() for ch in value):
        raise ValueError(f"{field} must not contain whitespace")
    return value


class AgentSpec(BaseModel):
    """spec.agent：数据面服务配置（后端/存储路径/命名空间/上报地址）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    backend: BackendType = Field("nvmet", alias="backend")
    advertise_url: str = Field("", alias="advertiseUrl", max_length=2048)
    disk_dir: str = Field(alias="diskDir", min_length=1, max_length=4096)
    nqn_base: str = Field(alias="nqnBase", min_length=1, max_length=256)

    @field_validator("advertise_url")
    @classmethod
    def _check_advertise_url(cls, value: str) -> str:
        value = value.strip()
        return value if not value else _check_url(value, "advertiseUrl")

    @field_validator("disk_dir")
    @classmethod
    def _check_disk_dir(cls, value: str) -> str:
        value = value.strip()
        if not Path(value).is_absolute():
            raise ValueError("diskDir must be an absolute host path, e.g. /data/storager_img")
        if "\x00" in value or "\n" in value:
            raise ValueError("diskDir must not contain control characters")
        return value

    @field_validator("nqn_base")
    @classmethod
    def _check_nqn_base(cls, value: str) -> str:
        value = value.strip()
        if not _NQN_BASE_RE.fullmatch(value):
            raise ValueError(
                "nqnBase must look like nqn.<yyyy-mm>.<reverse-domain>, e.g. nqn.2026-07.com.kurrent")
        return value


class ControlPlaneSpec(BaseModel):
    """spec.controlPlane：控制面入口（enroll/renew 的 mTLS 连接目标）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    url: str = Field(alias="url", min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return _check_url(value.strip(), "url")


class MetadataSpec(BaseModel):
    """metadata：节点身份（name = agent_id，机器身份；remark = 人类备注）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(alias="name", min_length=1, max_length=63)
    remark: str = Field("", alias="remark")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _AGENT_NAME_RE.fullmatch(value):
            raise ValueError(
                "name is the node identity (agent_id): lowercase letters, digits, dot or dash")
        return value


class NodeSpec(BaseModel):
    """spec：agent / controlPlane 子块（一次性 token 在独立凭据文件）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    agent: AgentSpec
    control_plane: ControlPlaneSpec = Field(alias="controlPlane")


class NodeConfiguration(BaseModel):
    """节点级配置根（apiVersion/kind/metadata/spec）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    api_version: Literal["kurrent.io/v1"] = Field(CONFIG_API_VERSION, alias="apiVersion")
    kind: Literal["NodeConfiguration"] = "NodeConfiguration"
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
    except yaml.YAMLError as exc:
        raise RuntimeError(f"invalid node configuration {cfg_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"invalid node configuration {cfg_path}: root must be a YAML mapping")
    try:
        return NodeConfiguration.model_validate(data)
    except ValidationError as exc:
        raise RuntimeError(f"invalid node configuration {cfg_path}: {exc}") from exc


# 模块级单例：import app.config 即加载（main.py / pki_client.py 共同消费）
CONFIG = load_config()
