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
# 控制面从认证密钥派生的 TLS-PSK（64 位十六进制，见 control_plane _derive_tls_psk）
TLS_PSK = "ab" * 32


# ============================ NvmetHostClient（mock UDS 连接） ============================


class _FakeResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self):
        return self._payload


class _FakeConnection:
    """替代 _UnixHTTPSConnection：记录请求，返回预设响应。"""

    last = None
    default_payload = b'{"ok": true}'
    default_status = 200
    request_error: Exception | None = None

    def __init__(self, socket_path, context, timeout):
        self.socket_path = socket_path
        self.context = context
        self.timeout = timeout
        self.request_args = None
        self.response = None
        self.closed = False
        _FakeConnection.last = self

    def request(self, method, path, body=None, headers=None):
        if _FakeConnection.request_error is not None:
            raise _FakeConnection.request_error
        self.request_args = {"method": method, "path": path,
                             "body": body, "headers": headers or {}}

    def getresponse(self):
        return _FakeResponse(_FakeConnection.default_payload, _FakeConnection.default_status)

    def close(self):
        self.closed = True


def test_host_client_request(monkeypatch, pki_dir):
    """请求构造：共享卷 UDS + mTLS 上下文 + JSON body + 路径 URL 编码；响应 JSON 解析。"""
    from app import nvmet

    _FakeConnection.default_payload = b'{"ok": true}'
    _FakeConnection.default_status = 200
    _FakeConnection.request_error = None
    monkeypatch.setattr(nvmet, "_UnixHTTPSConnection", _FakeConnection)
    client = nvmet.NvmetHostClient("/run/kurrent/nvmet/nvmet-host.sock", pki_dir)
    result = client.create_subsystem("iqn.2026-07.com.test:worker-01.ubuntu", "/srv/x.img")
    assert result == {"ok": True}
    conn = _FakeConnection.last
    assert conn.socket_path == "/run/kurrent/nvmet/nvmet-host.sock"
    assert conn.request_args["method"] == "POST"
    assert conn.request_args["path"] == "/subsystems"
    assert conn.request_args["headers"]["Content-Type"] == "application/json"
    assert json.loads(conn.request_args["body"]) == {
        "nqn": "iqn.2026-07.com.test:worker-01.ubuntu", "backing": "/srv/x.img"}
    assert conn.closed


def test_host_client_path_quoting(monkeypatch, pki_dir):
    """NQN 含冒号/点：路径段必须 URL 编码（宿主服务路由才能正确解析）。"""
    from app import nvmet

    _FakeConnection.default_payload = b"{}"
    _FakeConnection.default_status = 200
    _FakeConnection.request_error = None
    monkeypatch.setattr(nvmet, "_UnixHTTPSConnection", _FakeConnection)
    client = nvmet.NvmetHostClient("/run/kurrent/nvmet/nvmet-host.sock", pki_dir)
    client.set_host(IQN_UBUNTU, HOST_NQN, SECRET)
    assert _FakeConnection.last.request_args["path"] == (
        "/subsystems/iqn.2026-07.com.test%3Aworker-01.ubuntu/hosts"
    )


def test_host_client_http_error(monkeypatch, pki_dir):
    """非 2xx → NvmetHostError(status, detail)；detail 取响应 JSON 的 detail 字段。"""
    from app import nvmet

    _FakeConnection.default_payload = b'{"detail":"subsystem exists"}'
    _FakeConnection.default_status = 409
    _FakeConnection.request_error = None
    monkeypatch.setattr(nvmet, "_UnixHTTPSConnection", _FakeConnection)
    with pytest.raises(nvmet.NvmetHostError) as exc_info:
        nvmet.NvmetHostClient("/run/kurrent/nvmet/nvmet-host.sock", pki_dir) \
            .create_subsystem("nqn:x", "/srv/x.img")
    assert exc_info.value.status == 409
    assert "subsystem exists" in exc_info.value.detail


def test_host_client_unreachable(monkeypatch, pki_dir):
    """UDS 连接失败（宿主服务停机）→ NvmetHostError(status=0)。"""
    from app import nvmet

    _FakeConnection.default_payload = b"{}"
    _FakeConnection.default_status = 200
    _FakeConnection.request_error = ConnectionRefusedError("socket missing")
    monkeypatch.setattr(nvmet, "_UnixHTTPSConnection", _FakeConnection)
    with pytest.raises(nvmet.NvmetHostError) as exc_info:
        nvmet.NvmetHostClient("/run/kurrent/nvmet/nvmet-host.sock", pki_dir).healthz()
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


def test_backend_bad_backing_400(backend, monkeypatch):
    """宿主以 400 拒绝 backing（如 512 GPT）时透传 400，不误报为宿主不可用 503。"""
    from fastapi import HTTPException
    from app import nvmet

    b, _, _ = backend

    def reject(nqn, backing):
        raise nvmet.NvmetHostError(400, "backing GPT is 512-byte-sector layout")

    monkeypatch.setattr(b.host, "create_subsystem", reject)
    with pytest.raises(HTTPException) as exc_info:
        b.create_target(IQN_UBUNTU, "/srv/iscsi/worker-01.ubuntu.img", cd=False)
    assert exc_info.value.status_code == 400
    assert "512-byte-sector" in exc_info.value.detail


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
        entry = json.load(f)["worker-01"]
    assert entry["secret"] is None  # 保留 tombstone，周期 reconcile 继续确保 key 已删


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
    assert backend.cache._entries["worker-01"]["secret"] is None


def test_push_credential_rejects_nqn_traversal(agent_client):
    """sub_nqns/host_nqns 会拼进宿主 URL 与 configfs 路径：非 NQN 形态或含 `..` 一律 400。"""
    client, backend, calls = agent_client
    for field in ("sub_nqns", "host_nqns"):
        res = client.post("/credential", json={
            "worker_id": "worker-01", "secret": SECRET,
            "sub_nqns": [NQN_UBUNTU], "host_nqns": [HOST_NQN],
            **{field: ["nqn.2026-07.com.test:../../probe"]},
        })
        assert res.status_code == 400, field
    assert not calls
    assert "worker-01" not in backend.cache._entries


def test_healthz_backend_ready(agent_client):
    """/healthz：nvmet 后端带数据面就绪度（宿主服务可达 + configfs 就绪 → backend_ready=true）。"""
    client, backend, calls = agent_client
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["backend"] == "nvmet"
    assert body["backend_ready"] is True
    assert body["crl"] == {}


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


def test_verify_client_cert_requires_tls():
    """明文（scheme=http）一律 401；TLS 路径在单测放行（CN/CRL 由 component 测试覆盖）。"""
    from fastapi import HTTPException

    import app.main as agent_main

    class _Plain:
        scope = {"scheme": "http"}

    class _TLS:
        scope = {"scheme": "https"}

    with pytest.raises(HTTPException) as exc_info:
        agent_main.verify_client_cert(_Plain())
    assert exc_info.value.status_code == 401
    agent_main.verify_client_cert(_TLS())  # insecure 测试模式放行


def test_push_credential_requires_nvmet_backend(monkeypatch):
    """非 nvmet 后端（如 stgt）拒绝凭据推送。"""
    from fastapi.testclient import TestClient

    from app import main as agent_main

    monkeypatch.setattr(agent_main, "backend", object())
    client = TestClient(agent_main.app, base_url="https://testserver")
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


# ============================ 目标端 TLS-PSK（控制面派生，随认证密钥同步） ============================


def test_cache_apply_pushes_tls_psk(backend, fake_host):
    """apply(..., tls_psk)：认证登记之外同时把 PSK 注入宿主服务（sub × host 全组合）+ 缓存落盘。"""
    b, cache, calls = backend
    b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    cache.apply("worker-01", SECRET, [NQN_UBUNTU], [HOST_NQN], TLS_PSK)
    assert ("set_host", NQN_UBUNTU, HOST_NQN, SECRET) in calls
    assert ("set_tls_key", NQN_UBUNTU, HOST_NQN, TLS_PSK) in calls
    with open(cache.path, encoding="utf-8") as f:
        assert json.load(f)["worker-01"]["tls_psk"] == TLS_PSK


def test_cache_reconcile_replays_tls_psk(backend, fake_host):
    """reconcile 重放含 TLS-PSK 的条目：宿主不可达窗口后认证与 PSK 一并补上。"""
    b, cache, calls = backend
    b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    host, _ = fake_host
    host.unreachable = True
    with pytest.raises(Exception):
        cache.apply("worker-01", SECRET, [NQN_UBUNTU], [HOST_NQN], TLS_PSK)
    host.unreachable = False
    assert cache.reconcile() == {"applied": 1, "failed": 0}
    assert ("set_host", NQN_UBUNTU, HOST_NQN, SECRET) in calls
    assert ("set_tls_key", NQN_UBUNTU, HOST_NQN, TLS_PSK) in calls


def test_cache_revoke_deletes_tls_psk(backend, fake_host):
    """apply(None)：认证吊销的同时吊销目标端 PSK（解密资格与认证资格同生命周期）。"""
    b, cache, calls = backend
    b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    cache.apply("worker-01", SECRET, [NQN_UBUNTU], [HOST_NQN], TLS_PSK)
    cache.apply("worker-01", None, [NQN_UBUNTU], [HOST_NQN])
    assert ("delete_host", NQN_UBUNTU, HOST_NQN) in calls
    assert ("delete_tls_key", NQN_UBUNTU, HOST_NQN) in calls


def test_cache_secret_without_psk_keeps_existing(backend, fake_host):
    """密钥在但未带 PSK（旧控制面）：不动已有 PSK，避免误清（删除须由吊销显式触发）。"""
    b, cache, calls = backend
    b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    cache.apply("worker-01", SECRET, [NQN_UBUNTU], [HOST_NQN], TLS_PSK)
    before = len(calls)
    cache.apply("worker-01", SECRET, [NQN_UBUNTU], [HOST_NQN])  # tls_psk 缺省 = None
    assert [c for c in calls[before:] if c[0] in ("set_tls_key", "delete_tls_key")] == []


def test_cache_tls_409_is_non_fatal(backend, fake_host):
    """宿主无 TLS 端口（PUT /tls → 409）：认证照常同步、PSK 记警告跳过（降级而非故障）。"""
    b, cache, calls = backend
    b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    host, _ = fake_host
    host.tls_ready = False
    result = cache.apply("worker-01", SECRET, [NQN_UBUNTU], [HOST_NQN], TLS_PSK)
    assert ("set_host", NQN_UBUNTU, HOST_NQN, SECRET) in calls
    assert [c for c in calls if c[0] == "set_tls_key"] == []
    assert result["tls_applied"] == 0 and result["tls_expected"] == 1
    assert result["tls_unavailable"] == 1


def test_sync_reports_tls_applied_counts(backend):
    """成功推送回报 expected/applied，控制面据此判定 ok。"""
    b, cache, _ = backend
    b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    result = cache.apply("worker-01", SECRET, [NQN_UBUNTU], [HOST_NQN], TLS_PSK)
    assert result["tls_applied"] == 1 and result["tls_expected"] == 1
    assert result["tls_unavailable"] == result["tls_missing"] == 0


def test_credential_status_endpoint(agent_client):
    """POST /credential/status：不依赖缓存，直接查目标端 key 是否在位。"""
    client, backend, _ = agent_client
    backend.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    client.post("/credential", json={
        "worker_id": "worker-01", "secret": SECRET,
        "sub_nqns": [NQN_UBUNTU], "host_nqns": [HOST_NQN],
        "tls_psk": TLS_PSK, "generation": 1,
    })
    res = client.post("/credential/status", json={
        "worker_id": "worker-01", "sub_nqns": [NQN_UBUNTU], "host_nqns": [HOST_NQN],
    })
    assert res.status_code == 200
    assert res.json()["present_all"] is True
    backend.host.delete_tls_key(NQN_UBUNTU, HOST_NQN)
    res = client.post("/credential/status", json={
        "worker_id": "worker-01", "sub_nqns": [NQN_UBUNTU], "host_nqns": [HOST_NQN],
    })
    assert res.json()["present_all"] is False


def test_generation_rejects_stale_push(backend, fake_host):
    """旧 generation 的迟到 push 不得覆盖新值；缓存与宿主都不再改变。"""
    b, cache, calls = backend
    b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    cache.apply("worker-01", SECRET, [NQN_UBUNTU], [HOST_NQN], TLS_PSK, generation=2)
    before = len(calls)
    result = cache.apply("worker-01", SECRET, [NQN_UBUNTU], [HOST_NQN],
                         TLS_PSK, generation=1)
    assert result["stale"] is True and result["tls_applied"] == 0
    assert len(calls) == before
    with open(cache.path, encoding="utf-8") as f:
        assert json.load(f)["worker-01"]["generation"] == 2


def test_revoke_tombstone_is_replayed(backend, fake_host):
    """吊销保留 tombstone：周期 reconcile 继续确保目标端 key 已删除。"""
    b, cache, calls = backend
    b.create_target(IQN_UBUNTU, "/srv/x.img", cd=False)
    cache.apply("worker-01", SECRET, [NQN_UBUNTU], [HOST_NQN], TLS_PSK, generation=1)
    cache.apply("worker-01", None, [NQN_UBUNTU], [HOST_NQN], generation=2)
    before = len(calls)
    assert cache.reconcile() == {"applied": 1, "failed": 0}
    assert ("delete_tls_key", NQN_UBUNTU, HOST_NQN) in calls[before:]
