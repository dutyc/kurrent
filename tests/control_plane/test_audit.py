"""认证审计字段（I9）与告警：allow/deny 证据、fail-closed、八条检测中的可测项。"""

import json

import pytest
import yaml

from control_plane.app import audit, pki
from control_plane.app.config import settings


def _events() -> list[dict]:
    path = settings.audit_file
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _event_for(response) -> dict:
    request_id = response.headers["x-request-id"]
    return next(e for e in _events() if e["request_id"] == request_id)


def _alarms(name: str) -> list[dict]:
    return [e for e in _events() if e.get("event") == "alarm" and e.get("alarm") == name]


@pytest.fixture(autouse=True)
def users_file():
    settings.users_file.write_text(yaml.safe_dump({
        "users": {"admin": {"groups": ["admin"]}, "alice": {"groups": ["viewer"]}}
    }, sort_keys=True), encoding="utf-8")


@pytest.fixture()
def secure(monkeypatch):
    monkeypatch.setenv("KURRENT_CP_INSECURE", "")


def test_allow_event_has_identity_evidence(client, secure):
    headers = {
        "X-Client-Cert-DN": "/CN=admin",
        "X-Client-Class": "human",
        "X-Client-Cert-Issuer": "/CN=kurrent-ca-users",
        "X-Client-Cert-Serial": "1a2b3c",
        "X-Client-Cert-Fingerprint": "ab" * 32,
        "X-Client-Cert-Verify": "SUCCESS",
    }
    res = client.get("/whoami", headers=headers)
    assert res.status_code == 200
    event = _event_for(res)
    assert event["event"] == "authn"
    assert event["decision"] == "allow"
    assert event["principal_class"] == "human"
    assert event["principal"] == "admin"
    assert event["issuer"] == "ca-users"
    assert event["serial"] == "1a2b3c"
    assert event["fingerprint_sha256"] == "ab" * 32
    assert event["route"] == "GET /whoami"
    assert event["request_id"]


def test_deny_event_records_reason_and_no_secret(client, secure):
    res = client.get("/whoami", headers={"Authorization": "Bearer 123456.0123456789abcdef"})
    assert res.status_code == 401
    event = _event_for(res)
    assert event["decision"] == "deny"
    assert event["reason"]
    raw = settings.audit_file.read_text(encoding="utf-8")
    assert "0123456789abcdef" not in raw  # token 明文不入审计


def test_fail_closed_when_audit_unwritable(client, secure, monkeypatch):
    def _broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(audit, "emit", _broken)
    res = client.get("/whoami")
    assert res.status_code == 503
    assert res.json()["detail"] == "audit unavailable"


def test_component_on_management_route_alarms(client, secure):
    res = client.get("/agents", headers={
        "X-Client-Cert-DN": "/CN=agent-node-01",
        "X-Client-Class": "component",
    })
    assert res.status_code == 403
    assert _alarms("component_on_management_route")


def test_unknown_issuer_or_crl_alarms(client, secure):
    res = client.get("/agents", headers={
        "X-Client-Cert-DN": "/CN=agent-node-01",
        "X-Client-Cert-Issuer": "/CN=kurrent-ca-evil",
        "X-Client-Cert-Serial": "44",
        "X-Client-Cert-Verify": "FAILED:certificate revoked",
    })
    assert res.status_code == 401
    assert _alarms("unknown_issuer_or_crl")


def test_auth_failure_burst_alarms(client, secure):
    headers = {"X-Real-IP": "10.9.9.9"}
    for _ in range(audit._BURST_THRESHOLD):
        assert client.get("/whoami", headers=headers).status_code == 401
    alarms = _alarms("auth_failure_burst")
    assert alarms and alarms[-1]["client_ip"] == "10.9.9.9"


def test_users_file_change_alarms(client, secure):
    audit.check_users_file_change()
    settings.users_file.write_text(yaml.safe_dump(
        {"users": {"admin": {"groups": ["admin"]}}}, sort_keys=True), encoding="utf-8")
    assert client.get("/agents", headers={
        "X-Client-Cert-DN": "/CN=admin", "X-Client-Class": "human"}).status_code == 200
    assert _alarms("users_file_changed")


def test_bootstrap_identity_mismatch_alarms(client, tmp_path):
    original = settings.pki_dir
    object.__setattr__(settings, "pki_dir", tmp_path)
    try:
        token = pki.issue_bootstrap_token(tmp_path, agent_id="23456789abcd")
        res = client.post("/enroll", json={
            "agent_id": "3456789abcde", "component": "agent",
            "csr_client": "x", "csr_serving": "y",
        }, headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 401
        assert _alarms("bootstrap_identity_mismatch")
    finally:
        object.__setattr__(settings, "pki_dir", original)


def test_boot_injected_unverified_alarms():
    audit.boot_credential_observed(injected=True, verified=False,
                                   worker_id="w-1", mac="00:11:22:33:44:55")
    assert _alarms("boot_credential_unverified")


def test_pki_lifecycle_alarms(client, monkeypatch):
    """管理员证书签发 / CA 生成（启动时）与 CRL 更新（吊销时）都留告警。"""
    from control_plane.app.routers import certs as certs_mod
    monkeypatch.setattr(certs_mod, "reload_nginx", lambda: {"status": "ok"})
    assert _alarms("ca_generated")
    assert _alarms("admin_cert_issued")
    ca_cert, ca_key = pki.ensure_ca(settings.pki_dir, "users")
    pki.ensure_admin_credentials(settings.pki_dir, ca_cert, ca_key,
                                 settings.admin_conf.parent / "audit-user.conf",
                                 cn="audit-user", days=1)
    res = client.delete("/pki/users/audit-user")
    assert res.status_code == 200, res.text
    assert _alarms("crl_updated")


class TestAlarmWebhook:
    """可选告警外投：入队不阻塞、队列满丢弃、同步重试契约。"""

    def test_disabled_without_url(self, monkeypatch):
        monkeypatch.delenv("KURRENT_CP_ALARM_WEBHOOK", raising=False)
        assert audit._enqueue_alarm({"alarm": "x"}) is False

    def test_alarm_enqueues_audit_payload(self, monkeypatch):
        sent = []

        class FakeQueue:
            def put_nowait(self, payload):
                sent.append(payload)

        monkeypatch.setenv("KURRENT_CP_ALARM_WEBHOOK", "https://alerts.example/hook")
        monkeypatch.setattr(audit, "_alarm_queue", lambda: FakeQueue())
        audit.alarm("crl_updated", detail="cn=alice", route="pki")
        assert len(sent) == 1
        payload = sent[0]
        assert payload["alarm"] == "crl_updated" and payload["detail"] == "cn=alice"
        assert payload["webhook_url"] == "https://alerts.example/hook"
        assert payload["event"] == "alarm" and payload["ts"]

    def test_queue_full_drops(self, monkeypatch):
        import queue

        box = queue.Queue(maxsize=1)
        box.put_nowait({})
        monkeypatch.setenv("KURRENT_CP_ALARM_WEBHOOK", "https://alerts.example/hook")
        monkeypatch.setattr(audit, "_alarm_queue", lambda: box)
        assert audit._enqueue_alarm({"alarm": "x"}) is False

    def test_delivery_retries_then_gives_up(self, monkeypatch):
        calls = []

        def down(url, body):
            calls.append(url)
            raise OSError("unreachable")

        monkeypatch.setattr(audit, "_post_alarm", down)
        monkeypatch.setattr(audit, "ALARM_WEBHOOK_RETRY_DELAYS", (0, 0))
        audit._deliver_alarm({"webhook_url": "https://alerts.example/hook", "alarm": "x"})
        assert len(calls) == 3

    def test_delivery_retries_then_succeeds(self, monkeypatch):
        bodies = []

        def flaky(url, body):
            bodies.append(body)
            if len(bodies) == 1:
                raise OSError("blip")

        monkeypatch.setattr(audit, "_post_alarm", flaky)
        monkeypatch.setattr(audit, "ALARM_WEBHOOK_RETRY_DELAYS", (0, 0))
        audit._deliver_alarm({"webhook_url": "https://alerts.example/hook", "alarm": "x"})
        assert len(bodies) == 2
        assert bodies[-1] == {"alarm": "x"}  # webhook_url 是投递元数据，不进 JSON

    def test_post_contract(self, monkeypatch):
        captured = {}

        class Response:
            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout):
            captured.update(url=request.full_url, body=request.data,
                            timeout=timeout, method=request.get_method())
            return Response()

        monkeypatch.setattr(audit.urllib.request, "urlopen", fake_urlopen)
        audit._post_alarm("https://alerts.example/hook", {"alarm": "crl_updated"})
        assert captured["url"] == "https://alerts.example/hook"
        assert json.loads(captured["body"].decode()) == {"alarm": "crl_updated"}
        assert captured["timeout"] == audit.ALARM_WEBHOOK_TIMEOUT_SECONDS
        assert captured["method"] == "POST"
