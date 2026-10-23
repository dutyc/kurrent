"""自动化 token scope / TTL / last_used：权限上限、签发权限与台账字段。"""

import datetime as dt

import pytest
import yaml

from control_plane.app import pki
from control_plane.app.config import settings

ADMIN_DN = "/CN=admin/O=admin"
TEST_MAC = "00:11:22:33:44:ab"


def cert_headers(dn: str) -> dict:
    return {"X-Client-Cert-DN": dn, "X-Client-Class": "human"}


@pytest.fixture(autouse=True)
def users_file():
    settings.users_file.write_text(yaml.safe_dump({
        "users": {"admin": {"groups": ["admin"]}, "alice": {"groups": ["viewer"]}}
    }, sort_keys=True), encoding="utf-8")


@pytest.fixture()
def secure(monkeypatch):
    monkeypatch.setenv("KURRENT_CP_INSECURE", "")


def _issue(client, **body):
    res = client.post("/pki/tokens", json={"usage": "admin", **body},
                      headers=cert_headers(ADMIN_DN))
    assert res.status_code == 201, res.text
    return res.json()


def test_admin_token_default_scope_and_ttl(client, secure):
    out = _issue(client)
    assert out["usage"] == ["admin"]
    assert out["scopes"] == ["admin"]
    assert out["groups"] == ["admin"]
    remaining = dt.datetime.fromisoformat(out["expires_at"]) - dt.datetime.now(dt.timezone.utc)
    assert dt.timedelta(hours=23) < remaining <= dt.timedelta(hours=24)


def test_readonly_token_is_read_only(client, secure):
    out = _issue(client, scopes=["readonly"], ttl_hours=2)
    assert out["scopes"] == ["readonly"]
    assert out["groups"] == ["viewer"]
    token = out["token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/whoami", headers=headers).json()["groups"] == ["viewer"]
    assert client.get("/agents", headers=headers).status_code == 200
    assert client.post("/devices", json={"mac": TEST_MAC}, headers=headers).status_code == 403
    # readonly token 不能签发任何 token（含 admin / bootstrap）
    assert client.post("/pki/tokens", json={"usage": "admin"}, headers=headers).status_code == 403


def test_enroll_token_can_only_issue_bootstrap_tokens(client, secure):
    out = _issue(client, scopes=["enroll"])
    token = out["token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post("/pki/tokens", json={"usage": "bootstrap", "agent_id": "node-01"},
                          headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["agent_id"] == "node-01"
    # 提权：enroll token 不得签发 admin token，也不得做其他写操作
    assert client.post("/pki/tokens", json={"usage": "admin"}, headers=headers).status_code == 403
    assert client.post("/devices", json={"mac": TEST_MAC}, headers=headers).status_code == 403
    # scope 语义是「仅签发」：读接口同样不放行，防止借 enroll 凭据枚举管理面
    for path in ("/agents", "/devices", "/workers", "/pki/tokens",
                 "/pki/components", "/operations", "/settings/enforcement"):
        assert client.get(path, headers=headers).status_code == 403, path
    # 换会话也必须受限：会话继承 scope，不能借 Cookie 绕过读限制
    assert client.post("/session", headers=headers).status_code == 403


def test_scope_and_ttl_validation(client, secure):
    assert client.post("/pki/tokens", json={"usage": "admin", "scopes": ["root"]},
                       headers=cert_headers(ADMIN_DN)).status_code == 400
    for ttl in (0, -1, 721):
        res = client.post("/pki/tokens", json={"usage": "admin", "ttl_hours": ttl},
                          headers=cert_headers(ADMIN_DN))
        assert res.status_code == 400, (ttl, res.text)


def test_last_used_at_recorded(client, secure):
    out = _issue(client)
    token_id = out["token"].split(".", 1)[0]
    assert client.get("/whoami", headers={"Authorization": f"Bearer {out['token']}"}).status_code == 200
    items = client.get("/pki/tokens", headers=cert_headers(ADMIN_DN)).json()
    entry = next(i for i in items if i["token_id"] == token_id)
    assert entry["scopes"] == ["admin"]
    assert entry["last_used_at"]


def test_bootstrap_token_rejects_admin_fields():
    with pytest.raises(ValueError, match="ttl_hours only applies"):
        pki.issue_bootstrap_token(settings.pki_dir, agent_id="node-02", ttl_hours=2)
    with pytest.raises(ValueError, match="scopes only apply"):
        pki.issue_bootstrap_token(settings.pki_dir, agent_id="node-02", scopes=["readonly"])
