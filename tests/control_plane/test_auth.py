"""认证器链专项：cert DN / Bearer 台账 / 会话 Cookie / RBAC / 凭据自举（2026-09-15）。

常规用例在 tests/conftest.py 的 KURRENT_CP_INSECURE=1 下运行（开发放行）；本模块
用例先关闭开发模式（secure fixture），再逐凭据断言链行为——对应
control_plane/app/auth.py 的 authenticate / authorize_request / sign_session。
"""

import pytest
from cryptography.x509.oid import NameOID

from control_plane.app import auth as auth_mod
from control_plane.app import pki
from control_plane.app.config import settings

ADMIN_DN = "/CN=admin/O=admin"
VIEWER_DN = "/CN=alice/O=viewer"
TEST_MAC = "00:11:22:33:44:99"


@pytest.fixture()
def secure(monkeypatch):
    """关闭开发模式：认证器链真实生效（无凭据必 401）。"""
    monkeypatch.setenv("KURRENT_CP_INSECURE", "")


def issue_admin_token(groups: list[str] | None = None, days: int = 7) -> str:
    """签发管理 token（usage=admin；groups 决定授权组）。"""
    return pki.issue_bootstrap_token(
        settings.pki_dir, component="", usage=["admin"],
        groups=groups or ["admin"], name="admin", days=days,
    )


class TestCredentialChain:
    """认证器链：cert DN → Bearer → 会话，任一通过即止；无凭据 401。"""

    def test_no_credentials_401(self, client, secure):
        assert client.get("/whoami").status_code == 401
        assert client.get("/agents").status_code == 401

    def test_cert_dn_identity(self, client, secure):
        res = client.get("/whoami", headers={"X-Client-Cert-DN": ADMIN_DN})
        assert res.status_code == 200
        assert res.json() == {"name": "admin", "groups": ["admin"], "method": "cert"}

    def test_cert_dn_malformed_401(self, client, secure):
        """DN 无 CN（伪造/异常头）→ 无效凭据 401。"""
        assert client.get("/whoami", headers={"X-Client-Cert-DN": "garbage"}).status_code == 401

    def test_cert_dn_empty_falls_through(self, client, secure):
        """空 DN 头视为无该凭据 → 继续链上尝试（此处无其他凭据 → 401）。"""
        assert client.get("/whoami", headers={"X-Client-Cert-DN": ""}).status_code == 401

    def test_bearer_admin_token(self, client, secure):
        token = issue_admin_token()
        res = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        body = res.json()
        assert body["method"] == "token"
        assert body["groups"] == ["admin"]
        assert body["name"].startswith("token:")

    def test_bearer_bootstrap_token_rejected(self, client, secure):
        """bootstrap token（usage=enroll）不是管理凭据 → 401。"""
        token = pki.issue_bootstrap_token(settings.pki_dir)
        res = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 401

    def test_bearer_invalid_401(self, client, secure):
        res = client.get("/whoami", headers={"Authorization": "Bearer deadbe.0123456789abcdef"})
        assert res.status_code == 401

    def test_revoked_token_immediately_401(self, client, secure):
        """吊销（DELETE /pki/tokens/{id}）后 Bearer 立即 401：台账每请求重读，无缓存。"""
        token = issue_admin_token()
        token_id = token.split(".", 1)[0]
        assert client.get("/whoami", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        res = client.delete(f"/pki/tokens/{token_id}", headers={"X-Client-Cert-DN": ADMIN_DN})
        assert res.status_code == 200
        assert res.json() == {"revoked": token_id}
        assert client.get("/whoami", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_insecure_mode_allows(self, client):
        """开发模式（conftest 默认 env）：无凭据放行，身份 insecure/admin。"""
        res = client.get("/whoami")
        assert res.status_code == 200
        assert res.json()["method"] == "insecure"
        assert res.json()["groups"] == ["admin"]


class TestSession:
    """会话 Cookie：凭据换会话 → HttpOnly 签名票据；篡改/过期 401。

    注意：会话 Cookie 带 Secure 属性（仅 443 下发），TestClient 的 http scheme
    不会入 cookie jar——用例手动携带 Cookie 头模拟浏览器（真实场景 https 自动）。
    """

    @staticmethod
    def _cookie_header(res) -> dict:
        raw = res.headers.get("set-cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == auth_mod.SESSION_COOKIE and value:
                return {"Cookie": f"{auth_mod.SESSION_COOKIE}={value}"}
        raise AssertionError(f"set-cookie 缺 {auth_mod.SESSION_COOKIE}: {raw!r}")

    def test_session_roundtrip(self, client, secure):
        token = issue_admin_token()
        res = client.post("/session", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        assert "httponly" in res.headers["set-cookie"].lower()
        # 携带会话 Cookie 的后续请求：method 变为 session（浏览器 https 下自动）
        res = client.get("/whoami", headers=self._cookie_header(res))
        assert res.status_code == 200
        body = res.json()
        assert body["method"] == "session"
        assert body["name"].startswith("token:")

    def test_session_tampered_401(self, client, secure):
        client.cookies.set(auth_mod.SESSION_COOKIE, "deadbeef.deadbeef")
        assert client.get("/whoami").status_code == 401

    def test_session_expired_401(self, client, secure, monkeypatch):
        monkeypatch.setattr(auth_mod, "SESSION_TTL_DAYS", -1)
        client.cookies.set(auth_mod.SESSION_COOKIE, auth_mod.sign_session("admin", ["admin"]))
        monkeypatch.setattr(auth_mod, "SESSION_TTL_DAYS", 7)
        assert client.get("/whoami").status_code == 401

    def test_logout_clears_session(self, client, secure):
        """登出 = 下发清除 Cookie 指令（无状态签名会话，服务端无吊销态）。"""
        token = issue_admin_token()
        res = client.post("/session", headers={"Authorization": f"Bearer {token}"})
        res = client.delete("/session", headers=self._cookie_header(res))
        assert res.status_code == 200
        assert res.json() == {"status": "logged-out"}
        cleared = res.headers.get("set-cookie", "").lower()
        assert auth_mod.SESSION_COOKIE in cleared
        assert "max-age=0" in cleared


class TestRBAC:
    """授权：读请求 viewer 组；写请求需 admin 组（403）。"""

    def test_viewer_read_allowed_write_forbidden(self, client, secure):
        headers = {"X-Client-Cert-DN": VIEWER_DN}
        assert client.get("/agents", headers=headers).status_code == 200
        res = client.post("/devices", json={"mac": TEST_MAC}, headers=headers)
        assert res.status_code == 403

    def test_admin_write_allowed(self, client, secure):
        res = client.post("/devices", json={"mac": TEST_MAC}, headers={"X-Client-Cert-DN": ADMIN_DN})
        assert res.status_code == 201

    def test_viewer_token_groups(self, client, secure):
        """管理 token 的 groups 决定授权组：viewer token 可读不可写。"""
        token = issue_admin_token(groups=["viewer"])
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/agents", headers=headers).status_code == 200
        assert client.post("/devices", json={"mac": TEST_MAC}, headers=headers).status_code == 403


class TestCredentialBootstrap:
    """凭据自举：admin 证书（CN/O）+ 管理凭据文件 admin.conf（扁平四键）+ 会话密钥。"""

    def test_admin_conf_and_cert(self, client):
        conf = settings.admin_conf
        assert conf.exists()
        text = conf.read_text(encoding="utf-8")
        assert "server: https://127.0.0.1/api/cp" in text
        # 服务端信任锚 = TOFU server.crt（相对路径，宿主/容器视角一致）
        assert "certificate-authority: certs/server.crt" in text
        assert "client-certificate: pki/users/admin/client.crt" in text
        # kurrent 原生扁平格式：非 kubeconfig 结构（无 apiVersion/clusters/users 嵌套）
        assert "apiVersion" not in text and "clusters" not in text and "current-context" not in text

        cert_path = settings.pki_dir / "users" / "admin" / "client.crt"
        assert cert_path.exists()
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        orgs = [a.value for a in cert.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)]
        assert cn == "admin"
        assert orgs == ["admin"]
        assert (settings.pki_dir / "users" / "admin" / "client.key").exists()

    def test_session_key_bootstrapped(self, client):
        key_file = settings.session_key_file
        assert key_file.exists()
        assert len(key_file.read_bytes()) >= 32

    def test_admin_conf_legacy_migration(self, tmp_path):
        """历史 kubeconfig 结构 admin.conf：收敛时自动迁移为扁平格式（原 server 保留）；
        扁平格式且证书新鲜 → 原样保留（不重写）。"""
        pki_dir = tmp_path / "pki"
        conf = tmp_path / "admin.conf"
        ca_cert, ca_key = pki.ensure_ca(pki_dir)
        conf.write_text(
            "apiVersion: v1\nkind: Config\nclusters:\n- name: kurrent\n  cluster:\n"
            "    server: https://cp.example.com/api/cp\n"
            "    certificate-authority: certs/server.crt\n"
            "users:\n- name: admin\n  user:\n"
            "    client-certificate: pki/users/admin/client.crt\n"
            "    client-key: pki/users/admin/client.key\n"
            "contexts:\n- name: kurrent\n  context:\n    cluster: kurrent\n    user: admin\n"
            "current-context: kurrent\n",
            encoding="utf-8",
        )
        pki.ensure_admin_credentials(pki_dir, ca_cert, ca_key, conf)
        text = conf.read_text(encoding="utf-8")
        assert "clusters" not in text and "apiVersion" not in text
        assert "server: https://cp.example.com/api/cp" in text  # 原 server 无损保留
        # 已是扁平格式：再次收敛不改写（保留手改）
        pki.ensure_admin_credentials(pki_dir, ca_cert, ca_key, conf)
        assert conf.read_text(encoding="utf-8") == text


class TestDnParsing:
    """DN 解析（两种风格：nginx oneline / RFC2253）。"""

    def test_parse_slash_style(self):
        assert auth_mod.parse_dn("/CN=admin/O=admin") == ("admin", ["admin"])

    def test_parse_comma_style(self):
        assert auth_mod.parse_dn("CN=alice,O=viewer,O=ops") == ("alice", ["viewer", "ops"])

    def test_parse_empty(self):
        assert auth_mod.parse_dn("") == ("", [])
