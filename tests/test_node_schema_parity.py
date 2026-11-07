"""节点配置 schema parity：agent 与 nvmet-host 镜像内两份同构模型不得漂移。

两个镜像的构建上下文相互隔离，模型各自维护；本用例按文件路径加载两份 schema，
锁定 JSON Schema 与一组样本的校验结果（成功 / 错误路径）完全一致。
"""

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
AGENT_SCHEMA = ROOT / "storager" / "agent" / "app" / "config_schema.py"
NVMET_CONFIG = ROOT / "storager" / "nvmeof" / "nvmet-host" / "app" / "config.py"


def _load_module(path: Path, package: str):
    """按文件路径加载为 <package>.<stem>；父目录注册为命名空间包以解析模块内相对导入。"""
    if package not in sys.modules:
        pkg = types.ModuleType(package)
        pkg.__path__ = [str(path.parent)]
        sys.modules[package] = pkg
    name = f"{package}.{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _valid_node() -> dict:
    return {
        "apiVersion": "kurrent.io/v1",
        "kind": "NodeConfiguration",
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
    {**_valid_node(), "metadata": {"name": "3f2a9c1d8b7e"}},
    {**_valid_node(), "metadata": {"remark": "生产存储"}},
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
    agent = _load_module(AGENT_SCHEMA, "kurrent_parity_agent")
    state = tmp_path_factory.mktemp("node")
    config_file = state / "kurrent.yaml"
    config_file.write_text(yaml.safe_dump(_valid_node(), allow_unicode=True), encoding="utf-8")
    node_id_file = state / "agent.id"
    node_id_file.write_text("3f2a9c1d8b7e\n", encoding="utf-8")
    old_config = os.environ.get("KURRENT_CONFIG_FILE")
    old_id = os.environ.get("KURRENT_NODE_ID_FILE")
    os.environ["KURRENT_CONFIG_FILE"] = str(config_file)
    os.environ["KURRENT_NODE_ID_FILE"] = str(node_id_file)
    try:
        nvmet = _load_module(NVMET_CONFIG, "kurrent_parity_nvmet")
    finally:
        if old_config is None:
            os.environ.pop("KURRENT_CONFIG_FILE", None)
        else:
            os.environ["KURRENT_CONFIG_FILE"] = old_config
        if old_id is None:
            os.environ.pop("KURRENT_NODE_ID_FILE", None)
        else:
            os.environ["KURRENT_NODE_ID_FILE"] = old_id
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
