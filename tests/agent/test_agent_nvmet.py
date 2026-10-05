"""Agent nvmet 后端单测：NvmetHostClient（mock HTTP）、NvmetBackend（fake host）、
NvmetCredentialCache（缓存落盘 + hosts 矩阵同步 + 幂等重放）、POST /credential 端点。

覆盖链路（控制面推送驱动，2026-08-22 裁定）：
控制面 → Agent /credential → 缓存更新（0600 落盘）→ 宿主服务 set_host/delete_host。
"""

import json

import pytest

# 盘标识权威 = NQN（NvmetBackend 内部经 to_nqn 还原写 configfs，IQN 不可作 NQN）；IQN 由 NQN 派生
NQN_UBUNTU = "nqn.2026-07.com.test:worker-01.ubuntu"
NQN_WINDOWS = "nqn.2026-07.com.test:worker-01.windows"
IQN_UBUNTU = "iqn." + NQN_UBUNTU[4:]
IQN_WINDOWS = "iqn." + NQN_WINDOWS[4:]
HOST_NQN = "nqn.2026-07.com.kurrent:host.worker-01"
SECRET = "DHHC-1:01:YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo="


# ============================ NvmetHostClient（mock urllib） ============================


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def close(self):
        pass  # Python 3.14 HTTPError 以 tempfile 包装 fp，__del__ 时会调用 close

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_host_client_request(monkeypatch, pki_dir):
    """请求构造：mTLS 上下文（pki_dir 证书）+ JSON body + 路径 URL 编码；响应 JSON 解析。"""
    from app.nvmet import NvmetHostClient

    captured = {}

    def fake_urlopen(req, *, context=None, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["header"] = req.get_header("Authorization")
        captured["body"] = req.data
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = NvmetHostClient("https://127.0.0.1:4841", pki_dir)
    result = client.create_subsystem("iqn.2026-07.com.test:worker-01.ubuntu", "/srv/x.img")
    assert result == {"ok": True}
    assert captured["url"] == "https://127.0.0.1:4841/subsystems"
    assert captured["method"] == "POST"
    assert captured["header"] is None  # mTLS：客户端证书由 TLS 层承载，无 Authorization 头
    assert json.loads(captured["body"]) == {"nqn": "iqn.2026-07.com.test:worker-01.ubuntu",
                                            "backing": "/srv/x.img"}


def test_host_client_path_quoting(monkeypatch, pki_dir):
    """NQN 含冒号/点：路径段必须 URL 编码（宿主服务路由才能正确解析）。"""
    from app.nvmet import NvmetHostClient

    captured = {}

    def fake_urlopen(req, *, context=None, timeout=None):
        captured["url"] = req.full_url
        return _FakeResponse(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = NvmetHostClient("https://127.0.0.1:4841", pki_dir)
    client.set_host(IQN_UBUNTU, HOST_NQN, SECRET)
    assert captured["url"] == (
        "https://127.0.0.1:4841/subsystems/"
        "iqn.2026-07.com.test%3Aworker-01.ubuntu/hosts"
    )


def test_host_client_http_error(monkeypatch, pki_dir):
    """HTTPError → NvmetHostError(status, detail)；detail 取响应 JSON 的 detail 字段。"""
    import urllib.error

    from app.nvmet import NvmetHostClient, NvmetHostError

    def fake_urlopen(req, *, context=None, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 409, "conflict", {}, _FakeResponse(b'{"detail":"subsystem exists"}'))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(NvmetHostError) as exc_info:
        NvmetHostClient("https://127.0.0.1:4841", pki_dir).create_subsystem("nqn:x", "/srv/x.img")
    assert exc_info.value.status == 409
    assert "subsystem exists" in exc_info.value.detail


def test_host_client_unreachable(monkeypatch, pki_dir):
    """URLError（宿主服务停机）→ NvmetHostError(status=0)。"""
    import urllib.error

    from app.nvmet import NvmetHostClient, NvmetHostError

    def fake_urlopen(req, *, context=None, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(NvmetHostError) as exc_info:
        NvmetHostClient("https://127.0.0.1:4841", pki_dir).healthz()
    assert exc_info.value.status == 0
    assert "unreachable" in exc_info.value.detail


# ============================ NvmetBackend（fake host） ============================


def test_backend_create_target(backend):
    b, cache, calls = backend
    b.create_target(IQN_UBUNTU, "/srv/iscsi/worker-01.ubuntu.img", cd=False)
    assert ("create_subsystem", NQN_UBUNTU, "/srv/iscsi/worker-01.ubuntu.img") in calls


def test_backend_cd_rejected(backend):
    b, _, _ = backend
    with pytest.raises(Exception) as exc_info:
        b.create_target(IQN_UBUNTU, "/srv/x.iso", cd=True)
    assert exc_info.value.status_code == 400  # HTTPException


def test_backend_duplicate_409(backend):
    from fastapi import HTTPException

    b, _, _ = backend
    b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    with pytest.raises(HTTPException) as exc_info:
        b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    assert exc_info.value.status_code == 409


def test_backend_delete_404(backend):
    from fastapi import HTTPException

    b, _, _ = backend
    with pytest.raises(HTTPException) as exc_info:
        b.delete_target(IQN_UBUNTU)
    assert exc_info.value.status_code == 404


def test_backend_list_targets(backend):
    b, _, _ = backend
    b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    targets = b.list_targets()
    assert targets == [{"iqn": NQN_UBUNTU, "nqn": NQN_UBUNTU,
                        "luns": [{"lun": 1, "backing": "/srv/x.img"}]}]


def test_backend_unreachable_503(backend):
    from fastapi import HTTPException

    b, _, _ = backend
    b.host.unreachable = True
    with pytest.raises(HTTPException) as exc_info:
        b.list_targets()
    assert exc_info.value.status_code == 503
    with pytest.raises(HTTPException) as exc_info:
        b.capabilities()
    assert exc_info.value.status_code == 503


def test_backend_wait_ready(backend):
    b, _, _ = backend
    b.wait_ready(retries=1, interval=0)  # healthz configfs=true → 立即返回


def test_backend_scan(backend):
    """scan：.img 建子系统（iqn 去扩展名），.iso 跳过（nvmet 无 cd），已存在跳过。"""
    b, _, calls = backend
    import os

    disk_dir = b.disk_dir
    for name in ("worker-01.ubuntu.img", "worker-02.windows.img", "win-install.iso"):
        with open(os.path.join(disk_dir, name), "wb") as f:
            f.write(b"x" * 1024)
    result = b.scan()
    assert {c["iqn"] for c in result["created"]} == {
        "iqn.2026-07.com.test:worker-01.ubuntu",
        "iqn.2026-07.com.test:worker-02.windows",
    }
    assert result["skipped"] == ["iqn.2026-07.com.test:win-install.iso"]
    # 再次 scan：全部跳过
    result2 = b.scan()
    assert result2["created"] == []
    assert len(result2["skipped"]) == 3


def test_backend_startup_reconciles(backend, fake_host):
    """startup = scan + 凭据缓存重放（Agent 启动即对齐 hosts 矩阵）。"""
    b, cache, calls = backend
    host, _ = fake_host
    b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    cache.apply("worker-01", SECRET, [NQN_UBUNTU], [HOST_NQN])
    assert calls.count(("set_host", NQN_UBUNTU, HOST_NQN, SECRET)) == 1
    result = b.startup()
    assert result["created"] == []  # 空盘目录无可建盘
    assert calls.count(("set_host", NQN_UBUNTU, HOST_NQN, SECRET)) == 2  # 启动重放


# ============================ NvmetCredentialCache ============================


def test_cache_apply_set(backend, fake_host):
    """apply(secret)：缓存落盘（0600）+ 宿主服务 set_host（sub × host 全组合）。"""
    b, cache, calls = backend
    b.create_target(IQN_UBUNTU, "/srv/iscsi/worker-01.ubuntu.img", cd=False)
    b.create_target(IQN_WINDOWS, "/srv/iscsi/worker-01.windows.img", cd=False)
    cache.apply("worker-01", SECRET, [NQN_UBUNTU, NQN_WINDOWS], [HOST_NQN])
    assert ("set_host", NQN_UBUNTU, HOST_NQN, SECRET) in calls
    assert ("set_host", NQN_WINDOWS, HOST_NQN, SECRET) in calls
    with open(cache.path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["worker-01"]["secret"] == SECRET
    assert data["worker-01"]["sub_nqns"] == [NQN_UBUNTU, NQN_WINDOWS]


def test_cache_apply_revoke(backend, fake_host):
    """apply(None)：宿主服务 delete_host + 缓存条目移除。"""
    b, cache, calls = backend
    b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    cache.apply("worker-01", SECRET, [NQN_UBUNTU], [HOST_NQN])
    cache.apply("worker-01", None, [NQN_UBUNTU], [HOST_NQN])
    assert ("delete_host", NQN_UBUNTU, HOST_NQN) in calls
    with open(cache.path, encoding="utf-8") as f:
        assert "worker-01" not in json.load(f)


def test_cache_reconcile_retries(backend, fake_host):
    """reconcile：幂等重放缓存条目（覆盖宿主服务不可达窗口）。"""
    b, cache, calls = backend
    b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    cache.apply("worker-01", SECRET, [NQN_UBUNTU], [HOST_NQN])
    host, _ = fake_host
    host.unreachable = True
    with pytest.raises(Exception):
        cache.apply("worker-02", SECRET, [NQN_UBUNTU], [HOST_NQN])  # 宿主停机：同步失败但缓存已更新
    host.unreachable = False
    assert "worker-02" in cache._entries  # 缓存先行
    result = cache.reconcile()
    assert result == {"applied": 2, "failed": 0}
    assert ("set_host", NQN_UBUNTU, HOST_NQN, SECRET) in calls  # worker-02 已补上


def test_cache_reconcile_drops_stale(backend, fake_host):
    """reconcile 遇子系统 404（盘已删）：移除过期条目，避免死重试。"""
    b, cache, _ = backend
    b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    cache.apply("worker-01", SECRET, [NQN_UBUNTU], [HOST_NQN])
    b.delete_target(IQN_UBUNTU)  # 盘已删：子系统消失，缓存条目过期
    result = cache.reconcile()
    assert result == {"applied": 1, "failed": 0}
    assert "worker-01" not in cache._entries


# ============================ POST /credential 端点 ============================


def test_push_credential_endpoint(agent_client):
    """控制面推送 → 200：缓存更新 + 宿主服务 set_host；审计不记密钥本体。"""
    client, backend, calls = agent_client
    backend.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    res = client.post("/credential", json={
        "worker_id": "worker-01", "secret": SECRET,
        "sub_nqns": [NQN_UBUNTU], "host_nqns": [HOST_NQN],
    })
    assert res.status_code == 200
    assert res.json()["secret"] is True
    assert ("set_host", NQN_UBUNTU, HOST_NQN, SECRET) in calls
    assert "worker-01" in backend.cache._entries
    # 审计：op=credential，req 只含布尔标记不含明文
    import app.main as agent_main
    entries = agent_main.oplog.read()["entries"]
    cred = [e for e in entries if e["op"] == "credential"]
    assert cred and cred[-1]["result"] == "ok"
    assert SECRET not in json.dumps(cred, ensure_ascii=False)


def test_push_credential_revoke(agent_client):
    """secret=null：delete_host + 缓存条目移除。"""
    client, backend, calls = agent_client
    backend.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    client.post("/credential", json={
        "worker_id": "worker-01", "secret": SECRET,
        "sub_nqns": [NQN_UBUNTU], "host_nqns": [HOST_NQN],
    })
    res = client.post("/credential", json={
        "worker_id": "worker-01", "secret": None,
        "sub_nqns": [NQN_UBUNTU], "host_nqns": [HOST_NQN],
    })
    assert res.status_code == 200
    assert res.json()["secret"] is False
    assert ("delete_host", NQN_UBUNTU, HOST_NQN) in calls
    assert "worker-01" not in backend.cache._entries


def test_healthz_backend_ready(agent_client):
    """/healthz：nvmet 后端带数据面就绪度（宿主服务可达 + configfs 就绪 → backend_ready=true）。"""
    client, backend, calls = agent_client
    body = client.get("/healthz").json()
    assert body == {"status": "ok", "backend": "nvmet", "backend_ready": True}


def test_healthz_backend_unreachable_degraded(agent_client):
    """宿主服务不可达：进程活着（status=ok）但 backend_ready=false + detail——控制面据此标 degraded。"""
    client, backend, calls = agent_client
    backend.host.unreachable = True
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["backend"] == "nvmet"
    assert body["backend_ready"] is False
    assert "unreachable" in body["backend_detail"]


def test_healthz_configfs_not_ready_degraded(agent_client):
    """宿主服务可达但 configfs 未就绪（宿主前置缺失）→ 同样 degraded：
    这是 /capabilities 探测不到的洞（服务活着，能力接口照常 200）。"""
    client, backend, calls = agent_client
    backend.host.configfs = False
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["backend_ready"] is False
    assert "configfs" in body["backend_detail"]


def test_verify_client_cert():
    """mTLS 鉴权：TLS 层由 uvicorn CERT_REQUIRED + 内部 CA 强制，
    应用层 verify_client_cert 仅确认连接信息存在——无连接信息（request.client=None）→ 401。"""
    from fastapi import HTTPException

    import app.main as agent_main

    class _NoPeer:
        client = None

    with pytest.raises(HTTPException) as exc_info:
        agent_main.verify_client_cert(_NoPeer())
    assert exc_info.value.status_code == 401


def test_push_credential_requires_nvmet_backend(monkeypatch):
    """非 nvmet 后端（如 stgt）拒绝凭据推送。"""
    from fastapi.testclient import TestClient

    from app import main as agent_main

    monkeypatch.setattr(agent_main, "backend", object())
    client = TestClient(agent_main.app)
    res = client.post("/credential", json={"worker_id": "w"})
    assert res.status_code == 400
    assert "nvmet" in res.json()["detail"]


def test_push_credential_host_unreachable(agent_client, fake_host):
    """宿主服务不可达：端点 503（同步失败但缓存已更新，等待周期 reconcile 重放）。"""
    client, backend, _ = agent_client
    host, _ = fake_host
    host.unreachable = True
    res = client.post("/credential", json={
        "worker_id": "worker-01", "secret": SECRET,
        "sub_nqns": [NQN_UBUNTU], "host_nqns": [HOST_NQN],
    })
    assert res.status_code == 503
    assert "worker-01" in backend.cache._entries  # 缓存先行
    host.unreachable = False
    assert backend.cache.reconcile()["applied"] == 1


