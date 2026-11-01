"""bootstrap token：默认一次性、使用额度原子占用/退回、短 TTL。"""

import datetime as dt
import hashlib

import pytest
import yaml

from control_plane.app import pki
from control_plane.app.config import settings


def _issue(**kw) -> str:
    return pki.issue_bootstrap_token(settings.pki_dir, agent_id="f0123456789a", **kw)


def test_default_single_use():
    token = _issue()
    token_id = pki.reserve_bootstrap_token(settings.pki_dir, token, "f0123456789a", "agent")
    assert token_id == token.split(".", 1)[0]
    with pytest.raises(ValueError, match="already used"):
        pki.reserve_bootstrap_token(settings.pki_dir, token, "f0123456789a", "agent")


def test_max_uses_allowance():
    token = _issue(max_uses=2)
    pki.reserve_bootstrap_token(settings.pki_dir, token, "f0123456789a", "agent")
    pki.reserve_bootstrap_token(settings.pki_dir, token, "f0123456789a", "agent")
    with pytest.raises(ValueError, match="already used"):
        pki.reserve_bootstrap_token(settings.pki_dir, token, "f0123456789a", "agent")


def test_release_returns_allowance():
    token = _issue()
    token_id = pki.reserve_bootstrap_token(settings.pki_dir, token, "f0123456789a", "agent")
    pki.release_bootstrap_token(settings.pki_dir, token_id)
    assert pki.reserve_bootstrap_token(settings.pki_dir, token, "f0123456789a", "agent") == token_id


def test_legacy_entry_without_quota_stays_reusable():
    """P2 签发的存量 token（无 max_uses 字段）保持不限次数，直到过期。"""
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    tokens = pki._load_tokens(settings.pki_dir)
    tokens["legacy"] = {
        "component": "agent", "agent_id": "f0123456789a",
        "token_secret_hash": hashlib.sha256(b"0123456789abcdef").hexdigest(),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "expires_at": expires.isoformat(),
        "usage": ["enroll"], "groups": [],
    }
    pki._token_file(settings.pki_dir).write_text(
        yaml.safe_dump(tokens, sort_keys=True), encoding="utf-8")
    token = "legacy.0123456789abcdef"
    for _ in range(3):
        pki.reserve_bootstrap_token(settings.pki_dir, token, "f0123456789a", "agent")


def test_ttl_default_and_bounds():
    token = _issue()
    token_id = token.split(".", 1)[0]
    entry = pki.get_bootstrap_token(settings.pki_dir, token_id)
    created = dt.datetime.fromisoformat(entry["created_at"])
    expires = dt.datetime.fromisoformat(entry["expires_at"])
    assert dt.timedelta(minutes=59) < expires - created <= dt.timedelta(minutes=60)

    short = _issue(ttl_minutes=5)
    short_entry = pki.get_bootstrap_token(settings.pki_dir, short.split(".", 1)[0])
    delta = (dt.datetime.fromisoformat(short_entry["expires_at"])
             - dt.datetime.fromisoformat(short_entry["created_at"]))
    assert delta == dt.timedelta(minutes=5)

    with pytest.raises(ValueError, match="ttl_minutes"):
        _issue(ttl_minutes=1441)
    with pytest.raises(ValueError, match="max_uses"):
        _issue(max_uses=0)


@pytest.mark.parametrize("bad_id", ["storage-01", "node-01", "3F2A9C1D8B7E", "3f2a9c1d8b"])
def test_issue_rejects_non_hex_agent_id(bad_id):
    """节点身份只接受 12 位小写 hex；描述性名称归 metadata.remark。"""
    with pytest.raises(ValueError, match="12 lowercase hex"):
        pki.issue_bootstrap_token(settings.pki_dir, agent_id=bad_id)


def test_issue_endpoint_rejects_non_hex_agent_id(client, auth_headers):
    res = client.post("/pki/tokens", json={"agent_id": "storage-01"}, headers=auth_headers)
    assert res.status_code == 400
    assert "12 lowercase hex" in res.json()["detail"]


def test_issue_endpoint_reports_quota_and_short_ttl(client, auth_headers):
    res = client.post("/pki/tokens", json={"agent_id": "0123456789ac", "max_uses": 3},
                      headers=auth_headers)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["max_uses"] == 3 and body["uses"] == 0
    created = dt.datetime.fromisoformat(body["expires_at"]) - dt.datetime.now(dt.timezone.utc)
    assert dt.timedelta(minutes=59) < created <= dt.timedelta(minutes=60)
    bad = client.post("/pki/tokens", json={"agent_id": "0123456789ac", "max_uses": 0},
                      headers=auth_headers)
    assert bad.status_code == 400
