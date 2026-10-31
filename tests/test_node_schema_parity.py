"""节点配置 schema parity：agent 与 nvmet-host 镜像内两份同构模型不得漂移。

两个镜像的构建上下文相互隔离，模型各自维护；本用例按文件路径加载两份 schema，
锁定 JSON Schema 与一组样本的校验结果（成功 / 错误路径）完全一致。
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
AGENT_SCHEMA = ROOT / "storager" / "agent" / "app" / "config_schema.py"
NVMET_CONFIG = ROOT / "storager" / "nvmeof" / "nvmet-host" / "app" / "config.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(name, None)


def _valid_node() -> dict:
    return {
        "apiVersion": "kurrent.io/v1",
        "kind": "NodeConfiguration",
        "metadata": {"name": "storage-01", "remark": "生产存储"},
        "spec": {
            "agent": {
                "backend": "nvmet",
                "advertiseUrl": "https://10.0.0.1:4840",
                "diskDir": "/data/storager_img",
                "nqnBase": "nqn.2026-07.com.kurrent",
            },
            "controlPlane": {"url": "https://10.0.0.1"},
        },
    }


CORPUS = [
    _valid_node(),
    {**_valid_node(), "apiVersion": "kurrent.io/v2"},
    {**_valid_node(), "kind": "Other"},
    {**_valid_node(), "metadata": {"name": "Bad Name"}},
    {**_valid_node(), "metadata": {"name": "ok", "remark": "备注"}},
    {**_valid_node(), "spec": {"agent": {"diskDir": "/x", "nqnBase": "nqn.2026-07.com.x"}}},
    {**_valid_node(), "spec": {"agent": {"diskDir": "relative", "nqnBase": "nqn.2026-07.com.x"},
                              "controlPlane": {"url": "https://10.0.0.1"}}},
    {**_valid_node(), "spec": {"agent": {"diskDir": "/x", "nqnBase": "bad"},
                              "controlPlane": {"url": "https://10.0.0.1"}}},
    {**_valid_node(), "spec": {"agent": {"diskDir": "/x", "nqnBase": "nqn.2026-07.com.x",
                                        "unknownField": 1},
                              "controlPlane": {"url": "https://10.0.0.1"}}},
    {**_valid_node(), "spec": {"agent": {"diskDir": "/x", "nqnBase": "nqn.2026-07.com.x"},
                              "controlPlane": {"url": "ftp://10.0.0.1"}}},
]


def _outcome(model, data: dict):
    try:
        model.model_validate(data)
        return ("ok", [])
    except ValidationError as exc:
        return ("invalid", [tuple(err["loc"]) for err in exc.errors()])


@pytest.fixture(scope="module")
def schemas(tmp_path_factory):
    agent = _load_module(AGENT_SCHEMA, "kurrent_agent_node_schema")
    config_file = tmp_path_factory.mktemp("node") / "kurrent.yaml"
    config_file.write_text(yaml.safe_dump(_valid_node(), allow_unicode=True), encoding="utf-8")
    old = os.environ.get("KURRENT_CONFIG_FILE")
    os.environ["KURRENT_CONFIG_FILE"] = str(config_file)
    try:
        nvmet = _load_module(NVMET_CONFIG, "kurrent_nvmet_node_schema")
    finally:
        if old is None:
            os.environ.pop("KURRENT_CONFIG_FILE", None)
        else:
            os.environ["KURRENT_CONFIG_FILE"] = old
    return agent, nvmet


def test_json_schema_identical(schemas):
    agent, nvmet = schemas
    assert agent.NodeConfiguration.model_json_schema() == \
        nvmet.NodeConfiguration.model_json_schema()


def test_validation_outcomes_identical(schemas):
    agent, nvmet = schemas
    for data in CORPUS:
        assert _outcome(agent.NodeConfiguration, data) == \
            _outcome(nvmet.NodeConfiguration, data), data
