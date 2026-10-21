"""Agent（nvmet 后端）单测夹具：临时 kurrent.yaml + 直接 import app.main。

storager/agent/app 注入 sys.path（顶层 app 包名与 control_plane.app / nvmet_host_main
均不冲突），节点声明式配置写为临时 kurrent.yaml 并经 KURRENT_CONFIG_FILE 指向
（main.py/config.py 顶层 import 即加载，不再读 KURRENT_* 业务 env）。
PKI 引导跳过：预生成占位 client.crt（长有效期）使 ensure_pki() 走 ready 分支，
单测不连控制面（引导/轮换/证书落盘由部署集成测试覆盖）。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = PROJECT_ROOT / "storager" / "agent"
sys.path.insert(0, str(AGENT_DIR))  # 注入 agent 目录：import app → agent/app 包
sys.path.insert(0, str(PROJECT_ROOT / "tests"))  # pki_testkit 共享工具

from pki_testkit import gen_pki_dir  # noqa: E402

_STATE = Path(tempfile.mkdtemp(prefix="storager-agent-test-"))
_PKI_DIR = gen_pki_dir(_STATE / "pki")
_CONF_FILE = _STATE / "kurrent.yaml"
_CONF_FILE.write_text(
    f"""apiVersion: kurrent.io/v1
kind: NodeConfiguration
metadata:
  name: test-agent-01
spec:
  agent:
    backend: nvmet
    advertiseUrl: "https://127.0.0.1:4840"
    diskDir: {_STATE / "disks"}
    nqnBase: nqn.2026-07.com.test
  controlPlane:
    url: "https://127.0.0.1"
""",
    encoding="utf-8",
)
os.environ["KURRENT_CONFIG_FILE"] = str(_CONF_FILE)

import app.config as _cfg  # noqa: E402
# 容器内路径/凭据常量重定向到测试状态目录：pki/cp-ca/盘目录挂载点/日志/引导凭据属部署清单 compose 职责，
# 测试环境不依赖宿主挂载（ensure_pki 走 ready 分支，不实际使用 cp-ca/token）
_cfg.DEFAULT_PKI_DIR = str(_PKI_DIR)
_cfg.DEFAULT_CP_CA = str(_STATE / "cp-ca.crt")
_cfg.DEFAULT_DISK_DIR = str(_STATE / "disks")
_cfg.DEFAULT_LOG_FILE = str(_STATE / "ops.jsonl")
_cfg.DEFAULT_NVMET_CACHE_FILE = str(_STATE / "nvmet-credentials.json")
_cfg.DEFAULT_BOOTSTRAP_TOKEN_FILE = str(_STATE / "bootstrap-token")
# nvmet-host 派生凭据占位文件：backend=nvmet 且文件缺失时 ensure_pki 会强制 renew
# （真实网络请求）——预建占位使 force_renew 不触发（单测走 ready 分支）
_cfg.DEFAULT_NVMET_BOOTSTRAP_TOKEN_FILE = str(_STATE / "bootstrap-nvmet.token")
Path(_cfg.DEFAULT_NVMET_BOOTSTRAP_TOKEN_FILE).write_text("placeholder.token\n", encoding="utf-8")

import app.main as agent_main  # noqa: E402


@pytest.fixture()
def fake_host():
    """内存 fake nvmet-host 客户端：记录调用序列，可注入 404/不可达。"""
    from app.nvmet import NvmetHostError

    class FakeHost:
        def __init__(self):
            self.calls: list = []
            self.subsystems: dict = {}
            self.unreachable = False
            self.configfs = True
            self.tls_ready = True  # 置 False 模拟宿主服务无 TLS 端口（PUT /tls → 409）

        def _check(self):
            if self.unreachable:
                raise NvmetHostError(0, "nvmet host unreachable: test")

        def healthz(self, timeout=None):
            self._check()
            return {"status": "ok", "configfs": self.configfs}

        def capabilities(self):
            self._check()
            return {"backend": "nvmet", "cd": False}

        def create_subsystem(self, nqn, backing):
            self._check()
            if nqn in self.subsystems:
                raise NvmetHostError(409, f"subsystem exists: {nqn}")
            self.calls.append(("create_subsystem", nqn, backing))
            self.subsystems[nqn] = backing

        def delete_subsystem(self, nqn):
            self._check()
            self.calls.append(("delete_subsystem", nqn))
            if nqn not in self.subsystems:
                raise NvmetHostError(404, f"subsystem not found: {nqn}")
            del self.subsystems[nqn]

        def list_subsystems(self):
            self._check()
            return [{"nqn": n, "namespaces": [{"nsid": 1, "device_path": b}], "hosts": []}
                    for n, b in self.subsystems.items()]

        def set_host(self, nqn, hostnqn, secret):
            self._check()
            if nqn not in self.subsystems:
                raise NvmetHostError(404, f"subsystem not found: {nqn}")
            self.calls.append(("set_host", nqn, hostnqn, secret))

        def delete_host(self, nqn, hostnqn):
            self._check()
            if nqn not in self.subsystems:
                raise NvmetHostError(404, f"subsystem not found: {nqn}")
            self.calls.append(("delete_host", nqn, hostnqn))

        def set_tls_key(self, nqn, hostnqn, psk):
            self._check()
            if nqn not in self.subsystems:
                raise NvmetHostError(404, f"subsystem not found: {nqn}")
            if not self.tls_ready:
                raise NvmetHostError(409, "TLS port disabled (NVMET_TLS_PORT_ID 为空)")
            self.calls.append(("set_tls_key", nqn, hostnqn, psk))

        def delete_tls_key(self, nqn, hostnqn):
            self._check()
            if nqn not in self.subsystems:
                raise NvmetHostError(404, f"subsystem not found: {nqn}")
            self.calls.append(("delete_tls_key", nqn, hostnqn))

    host = FakeHost()
    return host, host.calls


@pytest.fixture()
def backend(fake_host, tmp_path):
    """NvmetBackend + NvmetCredentialCache（fake host + 独立缓存文件）。"""
    from app.nvmet import NvmetBackend, NvmetCredentialCache

    host, calls = fake_host
    disk_dir = tmp_path / "disks"
    disk_dir.mkdir()
    cache = NvmetCredentialCache(host, str(tmp_path / "creds.json"))
    return NvmetBackend(host, cache, str(disk_dir), "nqn.2026-07.com.test"), cache, calls


@pytest.fixture()
def agent_client(monkeypatch, fake_host, tmp_path):
    """Agent FastAPI 端点夹具：monkeypatch 全局 backend 为 nvmet 实例（不触发 lifespan）。"""
    from app.nvmet import NvmetBackend, NvmetCredentialCache
    from fastapi.testclient import TestClient

    host, calls = fake_host
    cache = NvmetCredentialCache(host, str(tmp_path / "agent-creds.json"))
    disk_dir = tmp_path / "disks"
    disk_dir.mkdir()
    fake = NvmetBackend(host, cache, str(disk_dir), "nqn.2026-07.com.test")
    monkeypatch.setattr(agent_main, "backend", fake)
    agent_main._backend_probe_cache.update(ts=0.0, value=None)  # healthz 探针缓存跨用例不残留
    client = TestClient(agent_main.app)
    return client, fake, calls


@pytest.fixture(scope="session")
def pki_dir():
    """测试 PKI 目录（conftest 顶层生成）：NvmetHostClient 构造 SSLContext 用（请求被 mock）。"""
    return _PKI_DIR
