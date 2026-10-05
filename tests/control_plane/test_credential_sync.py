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


def _fail_push(w, s, subs, hosts, tls_psk=None):
    raise RuntimeError("agent down")


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
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None: pushes.append(s) or {}
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
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None: pushes.append(s) or {}
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
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None: pushes.append(s) or {}
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
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None: pushes.append(s) or {}
    summary = reconcile_credentials_once()
    assert summary["pushed"] == 1 and len(pushes) == 1


def test_non_nvmet_target_marked_na(client, auth_headers, mock_agent_client):
    """非 nvmet 后端 Agent：推送记 n/a（凭据不适用，非故障；审计 skipped），对账视为已同步。"""
    _setup_agent_and_disk(client, auth_headers)

    def not_nvmet(w, s, subs, hosts, tls_psk=None):
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
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None: pushes.append(s) or {}

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
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None: pushes.append(s) or {}
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
