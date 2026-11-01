"""DHHC-1 定期轮换：到期判定、先推送后切台账、失败回滚与关闭开关。"""

import base64
import datetime as dt
import hashlib
import zlib

import yaml

from control_plane.app.config import settings
from control_plane.app.routers.workers import reconcile_credentials_once, rotate_due_credentials

AGENT_BODY = {
    "id": "a1b2c3d4e5f6", "base_url": "http://a1b2c3d4e5f6:8000",
    "role": {"disk": True, "cd": False}, "tags": ["storage", "nvmet"],
}


def make_secret(key: bytes = b"0123456789abcdef0123456789abcdef") -> str:
    return "DHHC-1:01:" + base64.b64encode(key + zlib.crc32(key).to_bytes(4, "little")).decode()


def _ok_push(w, s, subs, hosts, tls_psk=None):
    expected = len(subs) * len(hosts) if (tls_psk or s is None) else 0
    return {"worker_id": w, "tls_applied": expected if s else 0,
            "tls_expected": expected, "tls_revoked": expected if s is None else 0}


def _setup(client, auth_headers, worker_id: str = "worker-01", secret: str | None = None,
           age_days: float = 100) -> str:
    """注册 agent + worker + 一块盘 + 凭据；把密钥时间戳回拨 age_days 天。"""
    assert client.post("/agents", json=AGENT_BODY, headers=auth_headers).status_code == 201
    client.post("/workers", json={"worker_id": worker_id}, headers=auth_headers)
    assert client.post(f"/workers/{worker_id}/luns/disk", json={
        "type": "master", "os": "ubuntu", "name": "ubuntu-24.04-master",
    }, headers=auth_headers).status_code == 201
    secret = secret or make_secret()
    res = client.put(f"/workers/{worker_id}/credential", json={"secret": secret},
                     headers=auth_headers)
    assert res.status_code == 200, res.text
    _backdate(worker_id, age_days)
    return secret


def _backdate(worker_id: str, age_days: float) -> None:
    data = yaml.safe_load(settings.credentials_file.read_text(encoding="utf-8"))
    stamp = (dt.datetime.now().astimezone() - dt.timedelta(days=age_days)).isoformat()
    entry = data["credentials"][worker_id]
    entry["created_at"] = stamp
    entry["updated_at"] = stamp
    entry["rotated_at"] = stamp
    settings.credentials_file.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")


def _entry(worker_id: str = "worker-01") -> dict:
    data = yaml.safe_load(settings.credentials_file.read_text(encoding="utf-8"))
    return data["credentials"][worker_id]


def test_rotation_replaces_due_secret(client, auth_headers, mock_agent_client):
    """到期轮换：推送新密钥 → 台账切换；随后的对账不再重推。"""
    old = _setup(client, auth_headers)
    pushes = []
    mock_agent_client.set_credential = (
        lambda w, s, subs, hosts, tls_psk=None, generation=0:
        pushes.append((s, tls_psk)) or _ok_push(w, s, subs, hosts, tls_psk))

    summary = rotate_due_credentials()
    assert summary == {"rotated": 1, "failed": 0, "skipped": 0}
    assert len(pushes) == 1 and pushes[0][0] != old and pushes[0][1]
    entry = _entry()
    assert entry["secret"] == pushes[0][0] and entry["secret"] != old
    assert entry["secret_hash"] == "sha256:" + hashlib.sha256(entry["secret"].encode()).hexdigest()
    assert entry["rotated_at"] > entry["created_at"]
    assert reconcile_credentials_once()["pushed"] == 0


def test_rotation_skips_fresh_secret(client, auth_headers, mock_agent_client):
    _setup(client, auth_headers, age_days=1)
    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append(s) or _ok_push(w, s, subs, hosts, tls_psk)
    assert rotate_due_credentials() == {"rotated": 0, "failed": 0, "skipped": 0}
    assert pushes == []


def test_rotation_push_failure_keeps_old_secret(client, auth_headers, mock_agent_client):
    """推送失败：台账不切换（引导投影仍是宿主当前持有的密钥），记为失败。"""
    old = _setup(client, auth_headers)
    attempted = []

    def fail(w, s, subs, hosts, tls_psk=None, generation=0):
        attempted.append(s)
        raise RuntimeError("agent down")

    mock_agent_client.set_credential = fail
    summary = rotate_due_credentials()
    assert summary == {"rotated": 0, "failed": 1, "skipped": 0}
    assert attempted and attempted[0] != old
    assert _entry()["secret"] == old
    assert '"credential.rotate"' in settings.operations_file.read_text(encoding="utf-8")


def test_rotation_partial_failure_rolls_back_delivered(client, auth_headers, mock_agent_client):
    """部分送达：已收到新密钥的目标回推旧密钥；台账保持不变。"""
    old = _setup(client, auth_headers)
    agent_b = dict(AGENT_BODY, id="b2c3d4e5f601")
    assert client.post("/agents", json=agent_b, headers=auth_headers).status_code == 201
    targets = {
        "a1b2c3d4e5f6": ["nqn.2026-07.io.kurrent:w1"],
        "b2c3d4e5f601": ["nqn.2026-07.io.kurrent:w1-b"],
    }
    calls = []

    def set_credential(w, s, subs, hosts, tls_psk=None, generation=0):
        calls.append((subs, s))
        if any("w1-b" in nqn for nqn in subs):
            raise RuntimeError("agent down")
        return _ok_push(w, s, subs, hosts, tls_psk)

    mock_agent_client.set_credential = set_credential
    import control_plane.app.routers.workers as mod
    orig = mod._sync_targets
    mod._sync_targets = lambda record_, registered=None: targets
    try:
        summary = rotate_due_credentials()
    finally:
        mod._sync_targets = orig
    assert summary == {"rotated": 0, "failed": 1, "skipped": 0}
    # 顺序：新密钥 → a1b2c3d4e5f6 成功、b2c3d4e5f601 失败；随后 a1b2c3d4e5f6 回推旧密钥
    assert len(calls) == 3
    assert calls[0][1] != old and calls[1][1] == calls[0][1]
    assert calls[2][1] == old
    assert _entry()["secret"] == old


def test_rotation_skips_without_nvmet_targets(client, auth_headers, mock_agent_client):
    """没有可推送目标（无盘）→ 跳过，不生成新密钥。"""
    assert client.post("/agents", json=AGENT_BODY, headers=auth_headers).status_code == 201
    client.post("/workers", json={"worker_id": "worker-x"}, headers=auth_headers)
    client.put("/workers/worker-x/credential", json={"secret": make_secret()},
               headers=auth_headers)
    _backdate("worker-x", 200)
    pushes = []
    mock_agent_client.set_credential = lambda w, s, subs, hosts, tls_psk=None, generation=0: pushes.append(s) or _ok_push(w, s, subs, hosts, tls_psk)
    assert rotate_due_credentials() == {"rotated": 0, "failed": 0, "skipped": 1}
    assert pushes == []
    entry = _entry("worker-x")
    assert entry["rotated_at"] == entry["created_at"]  # 未轮换：时间戳不动


def test_rotation_disabled_when_period_zero(client, auth_headers, mock_agent_client, monkeypatch):
    _setup(client, auth_headers)
    monkeypatch.setenv("KURRENT_CP_CREDENTIAL_ROTATION_DAYS", "0")
    assert rotate_due_credentials() == {"rotated": 0, "failed": 0, "skipped": 0}
