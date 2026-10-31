"""节点声明式配置模型：唯一权威 schema（agent/nvmet-host 同构校验）。

本模块只定义模型与校验，不读取环境；运行态单例在 config.py，
`python -m app.config_tool validate|show` 在组件镜像内复用同一模型。

校验语义：
- 未知字段拒绝（extra="forbid"）、默认值注入、apiVersion/kind 锁定；
- 节点身份（metadata.name）是组件证书 CN 与控制面登记键，字符集/长度在声明期
  约束；diskDir 必须是宿主绝对路径；nqnBase 必须是合法 NQN 命名域；
- 容器内路径/监听/内部通讯地址属 compose 职责，不在声明中（出现即拒绝）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator)

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


def load_config_file(path: str | Path) -> NodeConfiguration:
    """加载并校验指定文件；缺失/语法/校验失败抛 RuntimeError（启动即失败）。"""
    cfg_path = Path(path)
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


def effective_config(cfg: NodeConfiguration) -> dict:
    """生效配置（默认值已注入；camelCase alias），供 config show 展示。"""
    return cfg.model_dump(by_alias=True, mode="json", exclude_none=True)
