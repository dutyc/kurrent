"""控制面→Agent 凭据推送对账测试（B3）：状态回写与投影、失败可见、对账补推（含审计去噪）、
吊销补投、崩溃缝隙覆盖、失效记录剪除与重新登记重推、非 nvmet 目标判 n/a。"""

import base64
import hashlib
import zlib

import yaml

from control_plane.app.agent_client import AgentAPIError
from control_plane.app.config import settings
from control_plane.app.routers.workers import reconcile_credentials_once


def make_secret(key: bytes = b"0123456789abcdef0123456789abcdef") -> str:
    """合法 DHHC-1 密钥（蓝图 2.1 契约：DHHC-1:01:<base64(key+CRC32 小端终值)>）。"""
    return "DHHC-1:01:" + base64.b64encode(key + zlib.crc32(key).to_bytes(4, "little")).decode()


AGENT_BODY = {
    "id": "ag-01", "base_url": "http://ag-01:8000",
    "role": {"disk": True, "cd": False}, "tags": ["storage", "nvmet"],
}


def _setup_agent_and_disk(client, auth_headers, worker_id: str = "worker-01") -> dict:
    res = client.post("/agents", json=AGENT_BODY, headers=auth_headers)
    assert res.status_code == 201, res.text
    client.post("/workers", json={"worker_id": worker_id}, headers=auth_headers)
    res = client.post(f"/workers/{worker_id}/luns/disk", json={
        "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
    }, headers=auth_headers)
    assert res.status_code == 201, res.text
    return res.json()


def _push_count() -> int:
    """凭据推送审计条数（去噪断言用）。"""
    return settings.operations_file.read_text(encoding="utf-8").count('"credential.push"')


def _fail_push(w, s, subs, hosts, tls_psk=None, generation=0):
    raise RuntimeError("agent down")


def _ok_push(w, s, subs, hosts, tls_psk=None):
    expected = len(subs) * len(hosts) if (tls_psk or s is None) else 0
    return {"worker_id": w, "tls_applied": expected if s else 0,
            "tls_expected": expected, "tls_revoked": expected if s is None else 0}


# ============================ 状态回写与投影 ============================


def test_push_state_recorded_and_projected(client, auth_headers, mock_agent_client):
    """推送成功 → 状态台账回写（审计哈希，无明文）且响应/GET 投影 ok。"""
    _setup_agent_and_disk(client, auth_headers)
    secret = make_secret()
    res = client.put("/workers/worker-01/credential", json={"secret": secret}, headers=auth_headers)
    assert res.status_code == 200
    push = res.json()["push"]
    assert push["result"] == "ok" and push["updated_at"]
    agents_view = push["agents"]
    assert agents_view == [{"agent_id": "ag-01", "result": "ok",
                            "updated_at": agents_view[0]["updated_at"]}]

    body = client.get("/workers/worker-01/credential", headers=auth_headers).json()
    assert body["push"] == push
    assert secret not in client.get("/workers/worker-01/credential", headers=auth_headers).text

    raw = settings.credential_push_file.read_text(encoding="utf-8")
    assert "sha256:" in raw and secret not in raw


def test_push_degraded_when_host_tls_disabled(client, auth_headers, mock_agent_client):
    """宿主无 TLS 端口：认证已写但 PSK 未应用 → degraded，不能显示为已送达。"""
    _setup_agent_and_disk(client, auth_headers)

    def no_tls(w, s, subs, hosts, tls_psk=None, generation=0):
        return {"tls_applied": 0, "tls_expected": len(subs) * len(hosts),
                "tls_unavailable": len(subs) * len(hosts),
                "tls_error": "TLS port disabled"}

    mock_agent_client.set_credential = no_tls
    res = client.put("/workers/worker-01/credential",
                     json={"secret": make_secret()}, headers=auth_headers)
    push = res.json()["push"]
    assert push["result"] == "degraded"
    assert push["agents"][0]["result"] == "degraded"
    assert "TLS port disabled" in push["agents"][0]["error"]


def test_push_failed_when_tls_partially_applied(client, auth_headers, mock_agent_client):
    """部分应用（例如子系统已删）→ failed，不能把未应用当成功。"""
    _setup_agent_and_disk(client, auth_headers)

    def partial(w, s, subs, hosts, tls_psk=None, generation=0):
        return {"tls_applied": 0, "tls_expected": 1, "tls_missing": 1,
                "tls_error": "subsystem not found"}

    mock_agent_client.set_credential = partial
    res = client.put("/workers/worker-01/credential",
                     json={"secret": make_secret()}, headers=auth_headers)
    push = res.json()["push"]
    assert push["result"] == "failed"
    assert "not applied" in push["agents"][0]["error"]


def test_push_without_tls_report_fails_closed(client, auth_headers, mock_agent_client):
    """Agent 不回报 tls_applied（旧版/异常）：failed，不再默认 ok。"""
    _setup_agent_and_disk(client, auth_headers)
    mock_agent_client.set_credential = (
        lambda w, s, subs, hosts, tls_psk=None, generation=0: {"worker_id": w})
    res = client.put("/workers/worker-01/credential",
                     json={"secret": make_secret()}, headers=auth_headers)
    assert res.json()["push"]["result"] == "failed"


def test_reconcile_repushes_when_key_missing(client, auth_headers, mock_agent_client):
    """台账 ok 但宿主 keyring 已空（重启）：对账回查在位状态并重推。"""
    _setup_agent_and_disk(client, auth_headers)
    secret = make_secret()
    res = client.put("/workers/worker-01/credential",
                     json={"secret": secret}, headers=auth_headers)
    assert res.json()["push"]["result"] == "ok"

    mock_agent_client.credential_status = (
        lambda worker_id, sub_nqns, host_nqns: {
            "worker_id": worker_id,
            "keys": [{"sub_nqn": s, "host_nqn": h, "present": False}
                     for s in sub_nqns for h in host_nqns],
            "present_all": False,
        })
    pushes = []
    mock_agent_client.set_credential = (
        lambda w, s, subs, hosts, tls_psk=None, generation=0:
        pushes.append((s, generation)) or _ok_push(w, s, subs, hosts, tls_psk))

    summary = reconcile_credentials_once()
    assert summary == {"targets": 1, "pushed": 1, "skipped": 0, "failed": 0}
    assert pushes and pushes[0][0] == secret
    assert pushes[0][1] > 0


def test_reconcile_key_check_uses_agent_config(client, auth_headers, mock_agent_client,
                                               monkeypatch):
    """keyring 在位回查必须用 AgentConfig 建客户端。

    AgentRegistry.client 接收 AgentConfig 并读其 base_url；传 agent_id 字符串会抛
    AttributeError，被 except 吞掉后一律按“不在位”处理，退化为每分钟重推。
    """
    _setup_agent_and_disk(client, auth_headers)
    res = client.put("/workers/worker-01/credential",
                     json={"secret": make_secret()}, headers=auth_headers)
    assert res.json()["push"]["result"] == "ok"

    from control_plane.app.stores import agents

    seen = []

    def factory(agent):
        seen.append(agent)
        assert getattr(agent, "base_url", ""), "client() must receive AgentConfig"
        return mock_agent_client

    monkeypatch.setattr(agents, "client", factory)
    pushes = []
    mock_agent_client.set_credential = (
        lambda w, s, subs, hosts, tls_psk=None, generation=0:
        pushes.append(1) or _ok_push(w, s, subs, hosts, tls_psk))

    summary = reconcile_credentials_once()
    assert summary == {"targets": 1, "pushed": 0, "skipped": 1, "failed": 0}
    assert not pushes
    assert seen and seen[0].id == "ag-01"


def test_generation_advances_and_stale_result_ignored(client, auth_headers, mock_agent_client):
    """每次推送推进 generation；旧 generation 的结果不能覆盖新台账。"""
    _setup_agent_and_disk(client, auth_headers)
    seen = []
    mock_agent_client.set_credential = (
        lambda w, s, subs, hosts, tls_psk=None, generation=0:
        seen.append(generation) or _ok_push(w, s, subs, hosts, tls_psk))

    client.put("/workers/worker-01/credential",
               json={"secret": make_secret()}, headers=auth_headers)
    client.put("/workers/worker-01/credential",
               json={"secret": make_secret()}, headers=auth_headers)
    data = yaml.safe_load(settings.credentials_file.read_text(encoding="utf-8"))
    assert data["generations"]["worker-01"] >= 2
    assert seen[0] < seen[1]

    from control_plane.app.routers import workers as wmod
    # 迟到的旧结果：不应把台账覆盖回 ok/旧 generation
    with wmod.credential_pushes.locked():
        before = wmod.credential_pushes.load()["pushes"]["worker-01/ag-01"]
    assert wmod._set_push_state("worker-01", "ag-01", "failed", None,
                                before["generation"] - 1, "stale") is False
    with wmod.credential_pushes.locked():
        after = wmod.credential_pushes.load()["pushes"]["worker-01/ag-01"]
    assert after["generation"] == before["generation"] and after["result"] == "ok"


def test_push_failure_visible_then_reconcile_recovers(client, auth_headers, mock_agent_client):
    """推送失败可见（failed + error）；对账补推成功；再次对账跳过（不刷屏、无新审计）。"""
    _setup_agent_and_disk(client, auth_headers)
    secret = make_secret()
    mock_agent_client.set_credential = _fail_push
    res = client.put("/workers/worker-01/credential", json={"secret": secret}, headers=auth_headers)
    push = res.json()["push"]
    assert push["result"] == "failed"
    assert push["agents"][0]["result"] == "failed" and "agent down" in push["agents"][0]["error"]
    count_after_failure = _push_count()

    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append(s) or _ok_push(w, s, subs, hosts, tls_psk)
    summary = reconcile_credentials_once()
    assert summary == {"targets": 1, "pushed": 1, "skipped": 0, "failed": 0}
    assert pushes == [secret]
    # 失败→成功是一次状态迁移：记一条审计（去噪 = 周期重放不重复记录）
    assert _push_count() == count_after_failure + 1

    body = client.get("/workers/worker-01/credential", headers=auth_headers).json()
    assert body["push"]["result"] == "ok"

    summary = reconcile_credentials_once()
    assert summary == {"targets": 1, "pushed": 0, "skipped": 1, "failed": 0}
    assert pushes == [secret]  # 已同步：未重推
    assert _push_count() == count_after_failure + 1


def test_reconcile_delivers_revocation_after_outage(client, auth_headers, mock_agent_client):
    """吊销推送失败（Agent 离线）→ 状态 failed；恢复后对账补投 secret=None → ok。"""
    _setup_agent_and_disk(client, auth_headers)
    client.put("/workers/worker-01/credential",
               json={"secret": make_secret()}, headers=auth_headers)
    mock_agent_client.set_credential = _fail_push

    res = client.delete("/workers/worker-01/credential", headers=auth_headers)
    assert res.json()["push"]["result"] == "failed"
    body = client.get("/workers/worker-01/credential", headers=auth_headers).json()
    assert body["exists"] is False and body["push"]["result"] == "failed"

    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append(s) or _ok_push(w, s, subs, hosts, tls_psk)
    summary = reconcile_credentials_once()
    assert pushes == [None] and summary["pushed"] == 1
    body = client.get("/workers/worker-01/credential", headers=auth_headers).json()
    assert body["exists"] is False and body["push"]["result"] == "ok"


def test_reconcile_covers_crash_gap(client, auth_headers, mock_agent_client):
    """崩溃缝隙：台账已写入、推送未发生（无状态记录）→ 投影 pending，对账补推。"""
    _setup_agent_and_disk(client, auth_headers)
    secret = make_secret()
    settings.credentials_file.write_text(yaml.safe_dump({"credentials": {"worker-01": {
        "worker_id": "worker-01",
        "secret": secret,
        "secret_hash": "sha256:" + hashlib.sha256(secret.encode()).hexdigest(),
        "created_at": "t",
        "updated_at": "t",
    }}}, sort_keys=True), encoding="utf-8")

    body = client.get("/workers/worker-01/credential", headers=auth_headers).json()
    assert body["exists"] is True and body["push"]["result"] == "pending"

    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append(s) or _ok_push(w, s, subs, hosts, tls_psk)
    summary = reconcile_credentials_once()
    assert pushes == [secret] and summary["pushed"] == 1
    body = client.get("/workers/worker-01/credential", headers=auth_headers).json()
    assert body["push"]["result"] == "ok"


def test_reconcile_prunes_stale_records_and_repushes_after_reregister(
        client, auth_headers, mock_agent_client):
    """Agent 注销 → 记录剪除；重新登记 → 无记录即重推（节点重置后凭据自动补投）。"""
    _setup_agent_and_disk(client, auth_headers)
    client.put("/workers/worker-01/credential",
               json={"secret": make_secret()}, headers=auth_headers)
    assert "worker-01/ag-01" in settings.credential_push_file.read_text(encoding="utf-8")

    res = client.delete("/agents/ag-01", headers=auth_headers)
    assert res.status_code in (200, 204), res.text
    summary = reconcile_credentials_once()
    assert summary["targets"] == 0
    assert "worker-01/ag-01" not in settings.credential_push_file.read_text(encoding="utf-8")
    # 登记已删 → 无从投递，投影不得谎报「待推送」（对账永不消费该对）
    body = client.get("/workers/worker-01/credential", headers=auth_headers).json()
    assert body["exists"] is True
    assert body["push"] == {"result": "none", "updated_at": None, "agents": []}

    res = client.post("/agents", json=AGENT_BODY, headers=auth_headers)
    assert res.status_code == 201, res.text
    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append(s) or _ok_push(w, s, subs, hosts, tls_psk)
    summary = reconcile_credentials_once()
    assert summary["pushed"] == 1 and len(pushes) == 1


def test_non_nvmet_target_marked_na(client, auth_headers, mock_agent_client):
    """非 nvmet 后端 Agent：推送记 n/a（凭据不适用，非故障；审计 skipped），对账视为已同步。"""
    _setup_agent_and_disk(client, auth_headers)

    def not_nvmet(w, s, subs, hosts, tls_psk=None, generation=0):
        raise AgentAPIError("ag-01", 400, "credential push requires backend=nvmet")

    mock_agent_client.set_credential = not_nvmet
    res = client.put("/workers/worker-01/credential",
                     json={"secret": make_secret()}, headers=auth_headers)
    push = res.json()["push"]
    assert push["result"] == "n/a" and push["agents"][0]["result"] == "n/a"

    summary = reconcile_credentials_once()
    assert summary == {"targets": 1, "pushed": 0, "skipped": 1, "failed": 0}
    ops = settings.operations_file.read_text(encoding="utf-8")
    assert '"reason": "backend not nvmet"' in ops


def test_credential_less_worker_not_pushed(client, auth_headers, mock_agent_client):
    """无密钥且无推送历史 → 不推送、不留记录、投影 none（本用例经建盘链路验证）。

    非 nvmet 后端建盘不签发密钥（DHHC-1 不适用），此时推送只会得 n/a；无意义推送会让
    agent 离线时显示「同步失败/待同步」的虚假故障。nvmet 上建盘则自动签发，见
    test_credentials.py「建盘即签发」。"""
    _setup_agent_and_disk(client, auth_headers)
    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append(s) or _ok_push(w, s, subs, hosts, tls_psk)

    assert not settings.credential_push_file.exists()  # 建盘未触发无意义推送
    summary = reconcile_credentials_once()
    assert summary == {"targets": 1, "pushed": 0, "skipped": 1, "failed": 0}
    assert pushes == []

    body = client.get("/workers/worker-01/credential", headers=auth_headers).json()
    assert body["exists"] is False
    assert body["push"] == {"result": "none", "updated_at": None, "agents": []}


def test_revoke_still_retried_after_history_exists(client, auth_headers, mock_agent_client):
    """有推送历史（曾设置过凭据）后吊销：即使未送达也持续对账重放（与 opt-in 跳过不冲突）。"""
    _setup_agent_and_disk(client, auth_headers)
    client.put("/workers/worker-01/credential",
               json={"secret": make_secret()}, headers=auth_headers)
    mock_agent_client.set_credential = _fail_push
    client.delete("/workers/worker-01/credential", headers=auth_headers)

    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append(s) or _ok_push(w, s, subs, hosts, tls_psk)
    assert reconcile_credentials_once()["pushed"] == 1
    assert pushes == [None]


# ============================ 删除 Worker 清凭据 ============================


def _credential_keys() -> list[str]:
    data = yaml.safe_load(settings.credentials_file.read_text(encoding="utf-8")) or {}
    return list((data.get("credentials") or {}).keys())


def _push_keys() -> list[str]:
    data = yaml.safe_load(settings.credential_push_file.read_text(encoding="utf-8")) or {}
    return list((data.get("pushes") or {}).keys())


def test_worker_delete_forgets_credential(client, auth_headers, mock_agent_client):
    """删除 Worker → 凭据条目与推送台账一并清掉（不留孤儿密钥给同 id 重建继承）。"""
    _setup_agent_and_disk(client, auth_headers)
    client.put("/workers/worker-01/credential",
               json={"secret": make_secret()}, headers=auth_headers)
    assert _credential_keys() == ["worker-01"] and _push_keys() == ["worker-01/ag-01"]

    res = client.delete("/workers/worker-01", headers=auth_headers)
    assert res.status_code == 200, res.text

    assert _credential_keys() == [] and _push_keys() == []
    assert '"credential.forget"' in settings.operations_file.read_text(encoding="utf-8")

    # 同 id 重建：不带旧钥（否则会静默继承并推给新机器的盘）
    client.post("/workers", json={"worker_id": "worker-01"}, headers=auth_headers)
    body = client.get("/workers/worker-01/credential", headers=auth_headers).json()
    assert body["exists"] is False
    assert body["push"] == {"result": "none", "updated_at": None, "agents": []}


def test_batch_worker_delete_forgets_credential(client, auth_headers, mock_agent_client):
    """批量删除是独立代码路径，同样清凭据。"""
    _setup_agent_and_disk(client, auth_headers, worker_id="w-01")
    client.post("/workers", json={"worker_id": "w-02"}, headers=auth_headers)
    client.post("/workers/w-02/luns/disk", json={
        "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
    }, headers=auth_headers)
    for worker_id in ("w-01", "w-02"):
        res = client.put(f"/workers/{worker_id}/credential",
                         json={"secret": make_secret()}, headers=auth_headers)
        assert res.status_code == 200, res.text
    assert sorted(_credential_keys()) == ["w-01", "w-02"]

    res = client.post("/workers/delete/batch",
                      json={"worker_ids": ["w-01", "w-02"]}, headers=auth_headers)
    assert res.status_code == 200, res.text
    assert [item["worker_id"] for item in res.json()["succeeded"]] == ["w-01", "w-02"]

    assert _credential_keys() == [] and _push_keys() == []
