"""enroll 自动登记 / bootstrap-token 签发 / agent 删除（一键加入链路）。

首次引导自动登记（enroll）+ 绑定 agent_id 的 bootstrap token（TTL 内可复用）
+ 节点删除（仅清登记，节点续期自动重新登记）。
nvmet-host 组件凭据：agent enroll 上报 backend=nvmet 时控制面派生随响应下发。
"""

import json
import re

import pytest
import yaml

from control_plane.app import pki as pki_mod
from control_plane.app.config import settings
from control_plane.app.stores import agents

_FAKE_CERTS = {"client.crt": b"FAKE-CLIENT", "serving.crt": b"FAKE-SERVING"}


@pytest.fixture()
def tmp_pki(tmp_path):
    """隔离 pki_dir：不触碰真实 state/pki（enroll/token 相关操作全部落临时目录）。"""
    old = settings.pki_dir
    object.__setattr__(settings, "pki_dir", tmp_path)
    try:
        yield tmp_path
    finally:
        object.__setattr__(settings, "pki_dir", old)


def _mock_issue(monkeypatch):
    """mock 证书签发链路（不生成真实 CA/证书）。"""
    monkeypatch.setattr(
        pki_mod, "ensure_ca_set",
        lambda d: {"users": (object(), object()), "nodes": (object(), object()),
                   "control": (object(), object())},
    )
    monkeypatch.setattr(pki_mod, "sign_component_certs", lambda *a, **k: dict(_FAKE_CERTS))
    monkeypatch.setattr(pki_mod, "ca_cert_pem", lambda d, kind="nodes": b"FAKE-CA")


def _token(tmp_pki, agent_id, component="agent", max_uses=1):
    """签发绑定节点身份的 bootstrap token（默认一次性）。"""
    return pki_mod.issue_bootstrap_token(tmp_pki, agent_id=agent_id, component=component,
                                         max_uses=max_uses)


def _enroll(client, token, agent_id, component, base_url="", capabilities=None, remark=None):
    body = {
        "agent_id": agent_id, "component": component,
        "csr_client": "FAKE", "csr_serving": "FAKE",
        "serving_sans": ["127.0.0.1"], "base_url": base_url,
    }
    if capabilities is not None:
        body["capabilities"] = capabilities
    if remark is not None:
        body["remark"] = remark
    return client.post("/enroll", json=body, headers={"Authorization": f"Bearer {token}"})


class TestEnrollAutoRegister:
    @pytest.mark.parametrize("bad_id", ["storage-01", "3F2A9C1D8B7E", "3f2a9c1d8b"])
    def test_enroll_rejects_non_hex_agent_id(self, client, bad_id):
        """enroll/renew 请求体同样只接受 12 位小写 hex。"""
        res = client.post("/enroll", json={
            "agent_id": bad_id, "component": "agent",
            "csr_client": "FAKE", "csr_serving": "FAKE",
        })
        assert res.status_code == 422

    def test_agent_auto_registered(self, client, tmp_pki, monkeypatch):
        """不在册 agent 用绑定 token 引导 → 201 + agents.yml 自动登记（base_url 上报）。"""
        _mock_issue(monkeypatch)
        token = _token(tmp_pki, "456789abcdef")
        resp = _enroll(client, token, "456789abcdef", "agent", "https://192.168.1.50:4840")
        assert resp.status_code == 201
        assert resp.json()["certificates"] == {k: v.decode() for k, v in _FAKE_CERTS.items()}
        assert resp.json()["ca_nodes_crt"] == "FAKE-CA"
        assert resp.json()["ca_control_crt"] == "FAKE-CA"
        a = agents.get("456789abcdef")
        assert a.base_url == "https://192.168.1.50:4840"
        assert a.role_disk and not a.role_cd and a.enabled

    def test_auto_registered_with_remark(self, client, tmp_pki, monkeypatch):
        """上报 remark（声明 metadata.remark）→ 随自动登记写入台账（备注，人类识别用）。"""
        _mock_issue(monkeypatch)
        token = _token(tmp_pki, "def012345678")
        resp = _enroll(client, token, "def012345678", "agent", "https://192.168.1.50:4840",
                       remark="storage-nvmet-01")
        assert resp.status_code == 201
        assert agents.get("def012345678").remark == "storage-nvmet-01"

    def test_auto_registered_without_base_url(self, client, tmp_pki, monkeypatch):
        """无 base_url 也自动登记（空地址，控制面不探测，WebUI/CLI 可补）。"""
        _mock_issue(monkeypatch)
        token = _token(tmp_pki, "56789abcdef0")
        resp = _enroll(client, token, "56789abcdef0", "agent")
        assert resp.status_code == 201
        assert agents.get("56789abcdef0").base_url == ""

    def test_reenroll_refreshes_declaration_fields(self, client, tmp_pki, monkeypatch):
        """A2 裁定（声明权威）：已在册 agent 再次 enroll/renew 时按节点声明刷新
        base_url / remark / role.cd；控制面属性（enabled/storager_ip/tags）原样保留。"""
        _mock_issue(monkeypatch)
        token = _token(tmp_pki, "aaaabbbbcccc", max_uses=2)
        assert _enroll(client, token, "aaaabbbbcccc", "agent", "https://10.0.0.5:4840",
                       capabilities={"backend": "nvmet", "cd": False},
                       remark="old-remark").status_code == 201
        # 运维在控制面侧设置属性（base_url 传现值——自动登记节点禁改地址）
        res = client.put("/agents/aaaabbbbcccc", json={
            "base_url": "https://10.0.0.5:4840", "storager_ip": "10.0.0.5",
            "role": {"disk": True, "cd": False}, "tags": ["custom"],
            "enabled": False, "remark": "old-remark",
        }, headers={"Authorization": "Bearer test-control-token"})
        assert res.status_code == 200, res.text
        # 节点搬家 + 改备注 + 光驱能力变化 → renew 按声明对齐
        assert _enroll(client, token, "aaaabbbbcccc", "agent", "https://10.0.0.9:4840",
                       capabilities={"backend": "nvmet", "cd": True},
                       remark="new-remark").status_code == 201
        a = agents.get("aaaabbbbcccc")
        assert a.base_url == "https://10.0.0.9:4840"  # 声明权威：地址随节点
        assert a.remark == "new-remark"
        assert a.role_cd is True
        assert a.enabled is False  # 控制面属性：renew 不碰
        assert a.storager_ip == "10.0.0.5"
        assert a.tags == ("custom",)

    def test_reenroll_unreported_fields_preserved(self, client, tmp_pki, monkeypatch):
        """旧 agent 兼容：再次 enroll 未上报 base_url/remark/capabilities → 不清空现有值。"""
        _mock_issue(monkeypatch)
        token = _token(tmp_pki, "bbbbccccdddd", max_uses=2)
        assert _enroll(client, token, "bbbbccccdddd", "agent", "https://10.0.0.5:4840",
                       remark="kept").status_code == 201
        assert _enroll(client, token, "bbbbccccdddd", "agent").status_code == 201
        a = agents.get("bbbbccccdddd")
        assert a.base_url == "https://10.0.0.5:4840"
        assert a.remark == "kept"
        assert not a.role_cd

    def test_nvmet_host_enroll_does_not_refresh(self, client, tmp_pki, monkeypatch):
        """仅 agent 组件承载声明字段：nvmet-host enroll 不触发台账刷新。"""
        _mock_issue(monkeypatch)
        token = _token(tmp_pki, "ccccddddeeee")
        assert _enroll(client, token, "ccccddddeeee", "agent", "https://10.0.0.5:4840").status_code == 201
        nvmet_token = _token(tmp_pki, "ccccddddeeee", "nvmet-host")
        assert _enroll(client, nvmet_token, "ccccddddeeee", "nvmet-host",
                       "https://10.9.9.9:4840").status_code == 201
        assert agents.get("ccccddddeeee").base_url == "https://10.0.0.5:4840"

    def test_token_bound_to_agent_rejects_other(self, client, tmp_pki, monkeypatch):
        """token 绑定 agent_id：同一 token 不能引导另一个节点（默认拒绝）。"""
        _mock_issue(monkeypatch)
        token = _token(tmp_pki, "23456789abcd")
        assert _enroll(client, token, "23456789abcd", "agent").status_code == 201
        assert _enroll(client, token, "3456789abcde", "agent").status_code == 401
        with pytest.raises(KeyError):
            agents.get("3456789abcde")

    def test_nvmet_derives_component_token(self, client, tmp_pki, monkeypatch):
        """agent 上报 backend=nvmet → 响应携带派生 nvmet-host token（绑 agent_id）。

        派生 token 供 nvmet-host 容器引导：该 agent 用派生 token enroll 组件成功；
        换 agent_id 使用 → 401（防串用）。
        """
        _mock_issue(monkeypatch)
        token = _token(tmp_pki, "abcdef012345")
        resp = _enroll(client, token, "abcdef012345", "agent", "https://192.168.1.50:4840",
                       capabilities={"backend": "nvmet", "cd": False})
        assert resp.status_code == 201
        a = agents.get("abcdef012345")
        assert a.tags == ("auto", "storage")  # 后端类型不进 tags（运行事实由 /capabilities 实时判定）
        assert a.role_disk and not a.role_cd
        nvmet_token = resp.json().get("nvmet_token")
        assert nvmet_token and re.fullmatch(r"[0-9a-f]{6}\.[0-9a-f]{16}", nvmet_token)
        # 派生 token 引导 nvmet-host（agent 已登记）→ 201
        assert _enroll(client, nvmet_token, "abcdef012345", "nvmet-host").status_code == 201
        # 派生 token 绑 agent_id：换 id 使用 → 401（对方 agent 在册时才走到 token 校验）
        assert _enroll(client, _token(tmp_pki, "fedcba987650"), "fedcba987650", "agent").status_code == 201
        assert _enroll(client, nvmet_token, "fedcba987650", "nvmet-host").status_code == 401

    def test_stgt_no_derivation(self, client, tmp_pki, monkeypatch):
        """stgt 后端（无 nvmet-host 组件）→ 响应不含 nvmet_token。"""
        _mock_issue(monkeypatch)
        token = _token(tmp_pki, "bcdef0123456")
        resp = _enroll(client, token, "bcdef0123456", "agent", "https://192.168.1.51:4840",
                       capabilities={"backend": "stgt", "cd": True})
        assert resp.status_code == 201
        assert "nvmet_token" not in resp.json()
        a = agents.get("bcdef0123456")
        assert a.tags == ("auto", "storage")
        assert a.role_disk and a.role_cd

    def test_auto_registered_without_capabilities_backward_compat(self, client, tmp_pki, monkeypatch):
        """旧 agent 不上报能力 → 兼容默认（仅 auto 标签、无 cd 角色、不派生）。"""
        _mock_issue(monkeypatch)
        token = _token(tmp_pki, "cdef01234567")
        resp = _enroll(client, token, "cdef01234567", "agent")
        assert resp.status_code == 201
        assert "nvmet_token" not in resp.json()
        a = agents.get("cdef01234567")
        assert a.tags == ("auto",)
        assert a.role_disk and not a.role_cd
        assert a.remark == ""  # 旧 agent 不上报 remark → 空备注

    def test_invalid_base_url_rejected(self, client, tmp_pki, monkeypatch):
        """上报地址格式非法 → 400（不自动登记）。"""
        _mock_issue(monkeypatch)
        token = _token(tmp_pki, "6789abcdef01")
        resp = _enroll(client, token, "6789abcdef01", "agent", "not-a-url")
        assert resp.status_code == 400
        with pytest.raises(KeyError):
            agents.get("6789abcdef01")

    def test_nvmet_not_auto_registered(self, client, tmp_pki, monkeypatch):
        """nvmet-host 不在册不自动登记（共享 agent_id，须 agent 组件先行在册）。"""
        _mock_issue(monkeypatch)
        token = _token(tmp_pki, "789abcdef012", "nvmet-host")
        resp = _enroll(client, token, "789abcdef012", "nvmet-host")
        assert resp.status_code == 400
        assert "not registered" in resp.json()["detail"]
        with pytest.raises(KeyError):
            agents.get("789abcdef012")

    def test_nvmet_token_not_burned_when_agent_missing(self, client, tmp_pki, monkeypatch):
        """nvmet-host 在 agent 未在册时 400 且 token 不消耗：agent 登记后重试仍成功。

        （token 校验本就 TTL 复用不消耗——400 先于 token 校验，重试语义天然成立）
        """
        _mock_issue(monkeypatch)
        nvmet_tok = _token(tmp_pki, "9abcdef01234", "nvmet-host")
        assert _enroll(client, nvmet_tok, "9abcdef01234", "nvmet-host").status_code == 400
        # agent 组件登记后，用同一 nvmet token 重试 → 201（token 未被消耗）
        agent_tok = _token(tmp_pki, "9abcdef01234")
        assert _enroll(client, agent_tok, "9abcdef01234", "agent").status_code == 201
        assert _enroll(client, nvmet_tok, "9abcdef01234", "nvmet-host").status_code == 201


class TestTokenEndpoint:
    """POST /pki/tokens：绑定 agent_id 的 bootstrap token 签发。"""

    def test_issue(self, client, tmp_pki, auth_headers):
        """签发成功：token 格式 <6位>.<16位>，明文仅本次可见；响应含绑定字段。"""
        resp = client.post("/pki/tokens", json={"agent_id": "456789abcdef"}, headers=auth_headers)
        assert resp.status_code == 201
        body = resp.json()
        assert re.fullmatch(r"[0-9a-f]{6}\.[0-9a-f]{16}", body["token"])
        assert body["expires_at"]
        assert body["usage"] == ["enroll"]
        assert body["agent_id"] == "456789abcdef" and body["component"] == "agent"

    def test_requires_agent_id(self, client, tmp_pki, auth_headers):
        """bootstrap token 不绑 agent_id → 400（默认拒绝，不再签发通用 token）。"""
        resp = client.post("/pki/tokens", headers=auth_headers)
        assert resp.status_code == 400
        assert "agent_id" in resp.json()["detail"]

    def test_rejects_illegal_agent_id_charset(self, client, tmp_pki, auth_headers):
        """节点身份字符集与证书 CN 同规（小写字母/数字/./-）：非法值 400，不落台账。"""
        resp = client.post("/pki/tokens", json={"agent_id": "Bad Node!"}, headers=auth_headers)
        assert resp.status_code == 400
        assert "agent_id" in resp.json()["detail"]
        assert client.get("/pki/tokens", headers=auth_headers).json() == []

    def test_each_issue_is_new(self, client, tmp_pki, auth_headers):
        """每次签发都是新 token（多 token 并存；默认一次性）。"""
        first = client.post("/pki/tokens", json={"agent_id": "456789abcdef"},
                            headers=auth_headers).json()["token"]
        second = client.post("/pki/tokens", json={"agent_id": "456789abcdef"},
                             headers=auth_headers).json()["token"]
        assert first != second

    def test_requires_bearer(self, client, tmp_pki, monkeypatch):
        """无凭据 401（关闭开发模式；认证器链行为见 test_auth.py）。"""
        monkeypatch.setenv("KURRENT_CP_INSECURE", "")
        resp = client.post("/pki/tokens", json={"agent_id": "456789abcdef"})
        assert resp.status_code == 401

    def test_token_validate_rejects_unknown(self, client, tmp_pki, monkeypatch):
        """校验：未登记 token / 过期 token → 401（enroll 链路）。"""
        _mock_issue(monkeypatch)
        resp = _enroll(client, "deadbe.0123456789abcdef", "ddddeeeeffff", "agent")
        assert resp.status_code == 401


class TestTokenManagement:
    """token 台账管理：GET /pki/tokens（列表）+ DELETE /pki/tokens/{token_id}（吊销）。"""

    def test_list_fields_no_secret(self, client, tmp_pki, auth_headers):
        """列表条目字段完备且不含 secret hash；admin token 归属字段为 admin。"""
        resp = client.post("/pki/tokens", json={"usage": "admin"}, headers=auth_headers)
        assert resp.status_code == 201
        token_id = resp.json()["token"].split(".", 1)[0]
        items = client.get("/pki/tokens", headers=auth_headers).json()
        entry = next(i for i in items if i["token_id"] == token_id)
        assert set(entry) == {"token_id", "usage", "groups", "scopes", "uses", "max_uses",
                              "component", "agent_id", "name", "created_at", "expires_at",
                              "last_used_at"}
        assert entry["usage"] == ["admin"] and entry["groups"] == ["admin"]
        assert entry["scopes"] == ["admin"] and entry["max_uses"] == 0 and entry["uses"] == 0
        assert entry["name"] == "admin" and entry["component"] == "" and entry["agent_id"] == ""
        assert entry["created_at"] and entry["expires_at"]  # created_at 签发时注入

    def test_list_excludes_expired_and_legacy_created_at(self, client, tmp_pki, auth_headers):
        """过期条目不出现；旧条目（无 created_at 字段）返回空串（不伪造时间）。"""
        pki_mod._token_file(tmp_pki).write_text(yaml.safe_dump({
            "exp111": {"component": "", "agent_id": "", "token_secret_hash": "x" * 64,
                       "created_at": "2020-01-01T00:00:00+00:00",
                       "expires_at": "2020-01-02T00:00:00+00:00",
                       "usage": ["enroll"], "groups": []},
            "old111": {"component": "", "agent_id": "", "token_secret_hash": "y" * 64,
                       "expires_at": "2999-01-01T00:00:00+00:00",
                       "usage": ["enroll"], "groups": []},
        }), encoding="utf-8")
        items = client.get("/pki/tokens", headers=auth_headers).json()
        assert [i["token_id"] for i in items] == ["old111"]
        assert items[0]["created_at"] == ""

    def test_revoke_removes_and_audits(self, client, tmp_pki, auth_headers):
        """吊销 = 删条目：立即从列表消失；审计留痕（op + token_id）。"""
        token = client.post("/pki/tokens", json={"usage": "admin"},
                            headers=auth_headers).json()["token"]
        token_id = token.split(".", 1)[0]
        res = client.delete(f"/pki/tokens/{token_id}", headers=auth_headers)
        assert res.status_code == 200 and res.json() == {"revoked": token_id}
        assert all(i["token_id"] != token_id
                   for i in client.get("/pki/tokens", headers=auth_headers).json())
        ops = [json.loads(l) for l in settings.operations_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        revokes = [e for e in ops if e.get("op") == "pki.token.revoke"]
        assert revokes and revokes[-1]["token_id"] == token_id
        assert revokes[-1]["status"] == "ok"

    def test_revoke_unknown_404(self, client, tmp_pki, auth_headers):
        assert client.delete("/pki/tokens/beef99", headers=auth_headers).status_code == 404


class TestEnrollDualComponent:
    def test_agent_and_nvmet_enroll_independent(self, client, tmp_pki, monkeypatch):
        """agent token + nvmet 派生 token（各自绑 agent_id）：互不影响。"""
        _mock_issue(monkeypatch)
        agent_tok = _token(tmp_pki, "89abcdef0123")
        nvmet_tok = _token(tmp_pki, "89abcdef0123", "nvmet-host")
        assert agent_tok and nvmet_tok
        assert _enroll(client, agent_tok, "89abcdef0123", "agent").status_code == 201
        # agent 在册后，nvmet 组件用自己 token 引导
        resp = _enroll(client, nvmet_tok, "89abcdef0123", "nvmet-host")
        assert resp.status_code == 201


class TestDeleteAgent:
    def test_delete(self, client, auth_headers):
        client.post("/agents", json={"id": "ef0123456789", "base_url": "https://127.0.0.1:4840",
                                     "role": {"disk": True, "cd": False}}, headers=auth_headers)
        resp = client.delete("/agents/ef0123456789", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "ef0123456789"
        assert client.get("/agents?live=false", headers=auth_headers).json() == []

    def test_missing_404(self, client, auth_headers):
        resp = client.delete("/agents/nope", headers=auth_headers)
        assert resp.status_code == 404
