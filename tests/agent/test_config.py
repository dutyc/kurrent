"""节点声明式配置（kurrent.yaml）加载与校验测试（pydantic v2）。

覆盖：合法加载、默认值注入、未知字段拒绝（extra="forbid"）、必填缺失报错、
bad backend 拒绝、文件缺失/坏 yaml 阻断启动。conftest 已生成合法 kurrent.yaml
并经 KURRENT_CONFIG_FILE 指向（import app.config 的模块级 CONFIG 即消费该文件）。
"""

import yaml
import pytest

from app.config import load_config


def _dump(data: dict) -> str:
    return yaml.safe_dump(data, allow_unicode=True)


def _valid() -> dict:
    return {
        "apiVersion": "kurrent.io/v1",
        "kind": "NodeConfiguration",
        "metadata": {"name": "test-node-01"},
        "spec": {
            "agent": {"diskDir": "/tmp/disks", "nqnBase": "nqn.2026-07.com.test"},
            "controlPlane": {"url": "https://127.0.0.1"},
        },
    }


def _load(tmp_path, data: dict):
    path = tmp_path / "kurrent.yaml"
    path.write_text(_dump(data), encoding="utf-8")
    return load_config(str(path))


def test_load_valid(tmp_path):
    node = _load(tmp_path, _valid())
    assert node.metadata.name == "test-node-01"
    assert node.spec.agent.disk_dir == "/tmp/disks"
    assert node.spec.agent.nqn_base == "nqn.2026-07.com.test"
    assert node.spec.control_plane.url == "https://127.0.0.1"


def test_defaults_injected(tmp_path):
    """缺省字段注入默认值：backend 默认 nvmet、advertiseUrl 默认空。

    容器内路径/监听/内部通讯/一次性 token（pki/cp-ca/日志/缓存/nvmetHostUrl/nvmetHost/
bootstrap）属部署清单 compose 职责，不在 yml 声明。"""
    spec = _load(tmp_path, _valid()).spec
    assert spec.agent.backend == "nvmet"
    assert spec.agent.advertise_url == ""


def test_metadata_remark_default_empty(tmp_path):
    """remark 可缺省（备注：人类识别用，随首次 enroll 登记进控制面台账）→ 默认空串。"""
    assert _load(tmp_path, _valid()).metadata.remark == ""


def test_metadata_remark_loaded(tmp_path):
    """remark 显式声明原样加载（自由文本，不做格式校验）。"""
    data = _valid()
    data["metadata"]["remark"] = "storage-nvmet-01（生产存储）"
    assert _load(tmp_path, data).metadata.remark == "storage-nvmet-01（生产存储）"


def test_container_path_keys_rejected(tmp_path):
    """容器内路径/监听/内部通讯/一次性 token 键不在 yml 声明（分层职责）：出现即拒绝。"""
    for path in [("spec", "agent", "logFile"),
                 ("spec", "agent", "nvmetCacheFile"),
                 ("spec", "agent", "iscsiContainer"),
                 ("spec", "agent", "nvmetHostUrl"),
                 ("spec", "nvmetHost"),
                 ("spec", "controlPlane", "caFile"),
                 ("spec", "pki"),
                 ("spec", "bootstrap")]:
        data = _valid()
        node = data["spec"]
        for key in path[1:-1]:
            node = node[key]
        node[path[-1]] = "oops"
        with pytest.raises(RuntimeError, match="invalid node configuration"):
            _load(tmp_path, data)


def test_unknown_field_rejected(tmp_path):
    """未知字段拒绝（extra="forbid"）：spec.agent 塞未知键 → 启动即失败。"""
    data = _valid()
    data["spec"]["agent"]["unknownField"] = "oops"
    with pytest.raises(RuntimeError, match="invalid node configuration"):
        _load(tmp_path, data)


def test_unknown_top_level_rejected(tmp_path):
    """spec 顶层未知块同样拒绝。"""
    data = _valid()
    data["spec"]["unexpected"] = 1
    with pytest.raises(RuntimeError, match="invalid node configuration"):
        _load(tmp_path, data)


def test_missing_required_rejected(tmp_path):
    """必填缺失报错：diskDir（宿主存储路径）缺失即失败（默认值不注入）。"""
    data = _valid()
    del data["spec"]["agent"]["diskDir"]
    with pytest.raises(RuntimeError, match="invalid node configuration"):
        _load(tmp_path, data)


def test_missing_control_plane_rejected(tmp_path):
    """controlPlane 块缺失即失败（url 必填）。"""
    data = _valid()
    del data["spec"]["controlPlane"]
    with pytest.raises(RuntimeError, match="invalid node configuration"):
        _load(tmp_path, data)


def test_bad_backend_rejected(tmp_path):
    """backend 非 Literal["nvmet","stgt","lio"] 拒绝。"""
    data = _valid()
    data["spec"]["agent"]["backend"] = "hacker"
    with pytest.raises(RuntimeError, match="invalid node configuration"):
        _load(tmp_path, data)


def test_missing_file_rejected(tmp_path):
    """文件缺失 → RuntimeError 阻断启动（提示先跑 kurrent join）。"""
    with pytest.raises(RuntimeError, match="missing node configuration"):
        load_config(str(tmp_path / "nope.yaml"))


def test_bad_yaml_rejected(tmp_path):
    """坏 yaml（语法错误）同样阻断启动。"""
    path = tmp_path / "kurrent.yaml"
    path.write_text("spec: [unclosed\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid node configuration"):
        load_config(str(path))


def test_join_generated_shape(tmp_path):
    """kurrent join 渲染的完整配置形态可加载（仅节点级业务键；token 在独立凭据文件）。"""
    data = {
        "apiVersion": "kurrent.io/v1",
        "kind": "NodeConfiguration",
        "metadata": {"name": "storage-nvmet-01"},
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
    node = _load(tmp_path, data)
    assert node.spec.agent.disk_dir == "/data/storager_img"
    assert node.spec.control_plane.url == "https://10.0.0.1"


# ── 声明期语义校验（join/config validate 在写盘前暴露）──

@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.update(apiVersion="kurrent.io/v2"), "apiVersion"),
    (lambda d: d.update(kind="OtherConfiguration"), "kind"),
    (lambda d: d["metadata"].update(name=""), "name"),
    (lambda d: d["metadata"].update(name="Storage-01"), "name"),
    (lambda d: d["spec"]["agent"].update(diskDir="relative/path"), "absolute"),
    (lambda d: d["spec"]["agent"].update(nqnBase="nqn.not-a-date"), "nqnBase"),
    (lambda d: d["spec"]["agent"].update(advertiseUrl="ftp://host:4840"), "http(s)"),
    (lambda d: d["spec"]["controlPlane"].update(url="not-a-url"), "http(s)"),
])
def test_semantic_validation_rejected(tmp_path, mutate, needle):
    data = _valid()
    mutate(data)
    with pytest.raises(RuntimeError, match="invalid node configuration") as exc:
        _load(tmp_path, data)
    assert needle in str(exc.value)


def test_config_tool_validate_and_show(tmp_path, capsys):
    from app import config_tool

    raw = _dump(_valid())
    assert config_tool.run("validate", raw) == 0
    assert "backend=nvmet" in capsys.readouterr().out
    assert config_tool.run("show", raw) == 0
    shown = yaml.safe_load(capsys.readouterr().out)
    assert shown["spec"]["agent"]["backend"] == "nvmet"
    assert shown["spec"]["controlPlane"]["url"] == "https://127.0.0.1"


def test_config_tool_reports_unknown_field(capsys):
    from app import config_tool

    data = _valid()
    data["spec"]["agent"]["unknownField"] = 1
    assert config_tool.run("validate", _dump(data)) == 2
    assert "unknownField" in capsys.readouterr().err


def test_renew_threshold_policy_file(tmp_path):
    """控制面下发的轮换阈值落盘后参与 ready() 判定（缺失回落 0.2）。"""
    from app import pki_client

    assert pki_client._renew_threshold(tmp_path) == pki_client.RENEW_THRESHOLD
    (tmp_path / pki_client.POLICY_FILENAME).write_text('{"renew_threshold": 0.5}\n')
    assert pki_client._renew_threshold(tmp_path) == 0.5
    (tmp_path / pki_client.POLICY_FILENAME).write_text('{"renew_threshold": 7}\n')
    assert pki_client._renew_threshold(tmp_path) == pki_client.RENEW_THRESHOLD
