"""nvmet-host 宿主服务单测夹具：临时 kurrent.yaml + importlib 加载。

NVMET_CONFIGFS 重定向到临时目录（mock configfs），PKI 引导跳过（预生成占位 client.crt
使 ensure_pki() 走 ready 分支，单测不连控制面）；
节点声明式配置写为临时 kurrent.yaml 并经 KURRENT_CONFIG_FILE 指向（main.py 顶层
import .config 即加载，不再读 NVMET_HOST_*/KURRENT_* 业务 env）；
模块以独立名字 nvmet_host_main 加载，避免与 Agent 的 app 包名冲突。
main.py 顶层 `from .config import CONFIG` / `from .pki_client import ensure_pki`
相对导入无 parent package：预注册 app 包 + 本目录 config/pki_client——无论 Agent
测试是否先行占用 sys.modules["app"]，此处都覆盖 app.config / app.pki_client 为
本组件实现（同一组件 PKI 客户端，行为一致）。
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "tests"))  # pki_testkit 共享工具

from pki_testkit import gen_pki_dir  # noqa: E402

_SPEC_DIR = PROJECT_ROOT / "storager" / "nvmeof" / "nvmet-host" / "app"

# TestClient 没有 TLS peer cert；单测跳过 CN/CRL 闸门（闸门本身由 test_component_security 覆盖）
os.environ["KURRENT_COMPONENT_INSECURE"] = "1"
# 单测不连控制面：关闭启动吊销自检（自检本身由 tests/component/test_pki_selfcheck 覆盖）
os.environ["KURRENT_PKI_SELF_CHECK"] = "0"

_STATE = Path(tempfile.mkdtemp(prefix="nvmet-host-test-"))
_PKI_DIR = gen_pki_dir(_STATE / "pki")
_CONF_FILE = _STATE / "kurrent.yaml"
_CONF_FILE.write_text(
    f"""apiVersion: kurrent.io/v1
kind: NodeConfiguration
metadata:
  name: test-nvmet-host-01
spec:
  agent:
    backend: nvmet
    diskDir: /tmp/disks
    nqnBase: nqn.2026-07.com.test
  controlPlane:
    url: "https://127.0.0.1"
""",
    encoding="utf-8",
)
os.environ["KURRENT_CONFIG_FILE"] = str(_CONF_FILE)
os.environ["NVMET_CONFIGFS"] = str(_STATE / "configfs-nvmet")

# main.py 顶层相对导入 .config/.pki_client：spec 加载的模块无 parent package，
# 预注册 app 包 + 本目录 config/pki_client（覆盖 Agent 测试先行导入的同名模块）
if "app" not in sys.modules:
    _app_pkg = types.ModuleType("app")
    _app_pkg.__path__ = []
    sys.modules["app"] = _app_pkg
for _name in ("config", "pki_client"):
    _spec = importlib.util.spec_from_file_location(
        f"app.{_name}", _SPEC_DIR / f"{_name}.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"app.{_name}"] = _mod
    _spec.loader.exec_module(_mod)
# 容器内路径/凭据常量重定向到测试状态目录：pki/cp-ca/引导凭据属部署清单 compose 职责，
# 测试环境不依赖宿主挂载（ensure_pki 走 ready 分支，不实际使用 cp-ca/token）
sys.modules["app.config"].DEFAULT_PKI_DIR = str(_PKI_DIR)
sys.modules["app.config"].DEFAULT_CP_CA = str(_STATE / "cp-ca.crt")
sys.modules["app.config"].DEFAULT_BOOTSTRAP_TOKEN_FILE = str(_STATE / "bootstrap-token")

_spec = importlib.util.spec_from_file_location(
    "nvmet_host_main", _SPEC_DIR / "main.py")
main = importlib.util.module_from_spec(_spec)
sys.modules["nvmet_host_main"] = main
main.__package__ = "app"  # 相对导入 .pki_client 的父包上下文
_spec.loader.exec_module(main)


@pytest.fixture()
def nvmet():
    """宿主服务模块（NvmetManager 指向 mock configfs 根）。"""
    return main


@pytest.fixture()
def configfs(nvmet):
    """mock configfs 根目录：每用例重建（清理上一用例残留）。"""
    root = Path(nvmet.CONFIGFS_NVMET)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return root


@pytest.fixture()
def client(nvmet):
    from fastapi.testclient import TestClient
    # 生产形态是 UDS + TLS；TestClient 用 https scheme 代表 TLS 层已生效
    return TestClient(nvmet.app, base_url="https://testserver")


# Windows 无 SeCreateSymbolicLinkPrivilege（WinError 1314）：os.symlink 不可用。
# 兼容层：symlink → 占位文件 + 记录（records[link_path] = target）；islink 按记录判定；
# unlink 时同步清记录。main.py 的 islink 判断 / unlink 清理 / rmtree 均不受影响。
_symlink_records: dict = {}


@pytest.fixture(autouse=True)
def _symlink_compat(monkeypatch):
    real_unlink = os.unlink
    _symlink_records.clear()

    def fake_symlink(src, dst, target_is_directory=False):
        _symlink_records[str(dst)] = src
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8"):
            pass  # 占位文件：保证 unlink/rmtree 语义与真实 symlink 一致

    def fake_islink(path):
        return str(path) in _symlink_records

    def fake_unlink(path, *args, **kwargs):
        _symlink_records.pop(str(path), None)
        return real_unlink(path, *args, **kwargs)

    # configfs group 删除语义：rmdir 目录即连带清理内部属性文件（configfs 属性
    # 不可单独 unlink）。mock 以普通文件模拟属性，需在 rmdir 前清空——与真实内核一致。
    real_rmdir = os.rmdir

    def fake_rmdir(path, *args, **kwargs):
        path = os.fspath(path)
        if os.path.isdir(path):
            for name in os.listdir(path):
                child = os.path.join(path, name)
                if os.path.isdir(child) and not os.path.islink(child):
                    fake_rmdir(child)
                else:
                    os.unlink(child)
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "symlink", fake_symlink)
    monkeypatch.setattr(os.path, "islink", fake_islink)
    monkeypatch.setattr(os, "unlink", fake_unlink)
    monkeypatch.setattr(os, "rmdir", fake_rmdir)


@pytest.fixture()
def symlinks():
    """symlink 兼容层记录：link 绝对路径 → target（测试断言挂载关系）。"""
    return _symlink_records
