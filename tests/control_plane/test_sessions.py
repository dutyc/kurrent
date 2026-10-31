"""P3 会话登记：Cookie 只带 jti、服务端权威、逐个吊销/全踢、Origin 校验。"""

import time

import pytest
import yaml

from control_plane.app import auth as auth_mod
from control_plane.app import config as cp_config
from control_plane.app import pki, sessions
from control_plane.app.config import settings

ADMIN_DN = "/CN=admin/O=admin"


def _cert() -> dict:
    return {"X-Client-Cert-DN": ADMIN_DN, "X-Client-Class": "human"}


@pytest.fixture(autouse=True)
def users_file():
    settings.users_file.write_text(yaml.safe_dump({
        "users": {"admin": {"groups": ["admin"]}, "alice": {"groups": ["viewer"]}}
    }, sort_keys=True), encoding="utf-8")


@pytest.fixture()
def secure(monkeypatch):
    monkeypatch.setenv("KURRENT_CP_INSECURE", "")


def _admin_token(groups=None) -> str:
    return pki.issue_bootstrap_token(settings.pki_dir, component="", usage=["admin"],
                                     groups=groups or ["admin"], name="admin")


def _cookie_from(res) -> dict:
    raw = res.headers.get("set-cookie", "")
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == auth_mod.SESSION_COOKIE and value:
            return {"Cookie": f"{auth_mod.SESSION_COOKIE}={value}"}
    raise AssertionError(f"missing session cookie: {raw!r}")


def _login(client, token=None) -> dict:
    headers = {"Authorization": f"Bearer {token or _admin_token()}"}
    return _cookie_from(client.post("/session", headers=headers))


class TestSessionRegistry:
    def test_cookie_attributes_and_hash_only(self, client, secure):
        headers = _login(client)
        doc = yaml.safe_load(settings.sessions_file.read_text(encoding="utf-8"))
        assert set(doc) == {"epoch", "sessions"}
        (key, entry), = doc["sessions"].items()
        assert len(key) == 64 and "token:" not in key
        assert entry["user"].startswith("token:")
        assert entry["scopes"] == ["admin"]
        # 登记里没有 Cookie 明文 jti
        cookie_value = headers["Cookie"].split("=", 1)[1]
        assert cookie_value.split(".")[0] not in settings.sessions_file.read_text(encoding="utf-8")

    def test_individual_revoke_kills_only_that_session(self, client, secure):
        first = _login(client)
        second = _login(client)
        assert client.get("/whoami", headers=first).status_code == 200
        sid = sessions.list_sessions()[0]["session_id"]
        res = client.delete(f"/sessions/{sid}", headers=second)
        assert res.status_code == 200
        alive = [h for h in (first, second) if client.get("/whoami", headers=h).status_code == 200]
        assert len(alive) == 1

    def test_revoke_all_bumps_epoch(self, client, secure):
        first = _login(client)
        second = _login(client)
        before = sessions.epoch()
        res = client.delete("/sessions", headers=first)
        assert res.status_code == 200 and res.json() == {"revoked": 2}
        assert sessions.epoch() == before + 1
        assert client.get("/whoami", headers=first).status_code == 401
        assert client.get("/whoami", headers=second).status_code == 401

    def test_unknown_session_404(self, client, secure):
        assert client.delete("/sessions/" + "0" * 64, headers=_cert()).status_code == 404

    def test_session_management_requires_admin(self, client, secure):
        viewer = _login(client, _admin_token(groups=["viewer"]))
        assert client.get("/sessions", headers=viewer).status_code == 403
        assert client.delete("/sessions", headers=viewer).status_code == 403

    def test_user_removed_invalidates_session(self, client, secure):
        # 人类会话：用证书登录
        human = _cookie_from(client.post("/session", headers=_cert()))
        token_session = _login(client)  # 自动化会话，确认只删人类
        settings.users_file.write_text(yaml.safe_dump({"users": {}}), encoding="utf-8")
        assert client.get("/whoami", headers=human).status_code == 401
        assert [s for s in sessions.list_sessions() if not s["user"].startswith("token:")] == []
        assert client.get("/whoami", headers=token_session).status_code == 200

    def test_legacy_cookie_ignored(self, client, secure, monkeypatch):
        monkeypatch.setattr(cp_config.CONFIG.spec.auth, "session_ttl_hours", 1)
        jti, exp, epoch = sessions.create("token:x", ["admin"], ttl_seconds=3600)
        client.cookies.set("kurrent_session", auth_mod.sign_session(jti, exp, epoch))
        assert client.get("/whoami").status_code == 401


class TestSessionOrigin:
    def test_cookie_write_requires_same_origin(self, client, secure):
        cookie = _login(client)
        res = client.post("/devices", json={"mac": "00:11:22:33:44:55"},
                          headers={**cookie, "Origin": "https://evil.example"})
        assert res.status_code == 403
        res = client.post("/devices", json={"mac": "00:11:22:33:44:55"},
                          headers={**cookie, "Origin": "http://testserver"})
        assert res.status_code == 201

    def test_bearer_write_unaffected_by_origin(self, client, secure):
        token = _admin_token()
        res = client.post("/devices", json={"mac": "00:11:22:33:44:55"},
                          headers={"Authorization": f"Bearer {token}",
                                   "Origin": "https://evil.example"})
        assert res.status_code == 201


class TestSessionRenewal:
    def test_renews_only_when_less_than_half_left(self, client, secure):
        # 剩余 60s（< 12h 的一半）→ 续期并重发 Cookie
        jti, exp, epoch = sessions.create("token:x", ["admin"], ttl_seconds=60)
        near = auth_mod.sign_session(jti, exp, epoch)
        res = client.get("/whoami", headers={"Cookie": f"{auth_mod.SESSION_COOKIE}={near}"})
        assert res.status_code == 200
        assert auth_mod.SESSION_COOKIE in res.headers.get("set-cookie", "")
        # 新登记剩余时间已拉长
        entry_exp = sessions.list_sessions()[0]["expires_at"]
        assert entry_exp > time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 3600))

    def test_no_renew_when_fresh(self, client, secure):
        cookie = _login(client)
        res = client.get("/whoami", headers=cookie)
        assert res.status_code == 200
        assert auth_mod.SESSION_COOKIE not in res.headers.get("set-cookie", "")
