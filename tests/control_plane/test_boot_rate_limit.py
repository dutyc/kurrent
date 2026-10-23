"""匿名引导端点限速：/devices/report 与 /devices/challenge 按来源 IP 滑窗。"""

import json

import pytest

from control_plane.app import ratelimit
from control_plane.app.config import settings

MAC = "00:11:22:33:44:55"


@pytest.fixture()
def limits(monkeypatch):
    def _set(report: int | None = None, challenge: int | None = None,
             boot_vars: int | None = None):
        if report is not None:
            monkeypatch.setenv("KURRENT_CP_RATE_LIMIT_REPORT", str(report))
        if challenge is not None:
            monkeypatch.setenv("KURRENT_CP_RATE_LIMIT_CHALLENGE", str(challenge))
        if boot_vars is not None:
            monkeypatch.setenv("KURRENT_CP_RATE_LIMIT_BOOT_VARS", str(boot_vars))
        ratelimit.reset()
    return _set


def _alarms() -> list[dict]:
    try:
        lines = settings.audit_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [doc for doc in (json.loads(line) for line in lines if line.strip())
            if doc.get("event") == "alarm"]


def test_report_rate_limited(client, limits):
    limits(report=2)
    src = {"X-Real-IP": "10.0.0.1"}
    assert client.get("/devices/report", params={"mac": MAC}, headers=src).status_code == 200
    assert client.get("/devices/report", params={"mac": MAC}, headers=src).status_code == 200
    res = client.get("/devices/report", params={"mac": MAC}, headers=src)
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) >= 1
    assert any(a.get("alarm") == "boot_rate_limited" for a in _alarms())


def test_challenge_rate_limited(client, limits):
    limits(challenge=1)
    src = {"X-Real-IP": "10.0.0.2"}
    # 未登记设备先 404；限速在设备查询之前生效
    assert client.get("/devices/challenge", params={"mac": MAC}, headers=src).status_code == 404
    assert client.get("/devices/challenge", params={"mac": MAC}, headers=src).status_code == 429


def test_scopes_and_sources_are_independent(client, limits):
    limits(report=1, challenge=1)
    a, b = {"X-Real-IP": "10.0.0.3"}, {"X-Real-IP": "10.0.0.4"}
    assert client.get("/devices/report", params={"mac": MAC}, headers=a).status_code == 200
    assert client.get("/devices/report", params={"mac": MAC}, headers=a).status_code == 429
    # 换来源 IP 与换 scope 都不受影响
    assert client.get("/devices/report", params={"mac": MAC}, headers=b).status_code == 200
    assert client.get("/devices/challenge", params={"mac": MAC}, headers=a).status_code == 404


def test_boot_vars_rate_limited(client, limits):
    """最贵的匿名端点（ECDSA 验签）同样限速：超限 429 + Retry-After。"""
    limits(boot_vars=2)
    src = {"X-Real-IP": "10.0.0.7"}
    assert client.get("/boot-vars", params={"hostname": "worker-01"}, headers=src).status_code == 200
    assert client.get("/boot-vars", params={"hostname": "worker-01"}, headers=src).status_code == 200
    res = client.get("/boot-vars", params={"hostname": "worker-01"}, headers=src)
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) >= 1
    assert any(a.get("alarm") == "boot_rate_limited" and a.get("route") == "/boot-vars"
               for a in _alarms())


def test_limit_zero_disables(client, limits):
    limits(report=0)
    src = {"X-Real-IP": "10.0.0.5"}
    for _ in range(5):
        assert client.get("/devices/report", params={"mac": MAC}, headers=src).status_code == 200


def test_window_expiry(monkeypatch):
    monkeypatch.setenv("KURRENT_CP_RATE_LIMIT_REPORT", "1")
    ratelimit.reset()
    assert ratelimit.check("report", "10.0.0.6", now=1000.0) is None
    assert ratelimit.check("report", "10.0.0.6", now=1001.0) is not None
    # 窗口滑过后重新放行
    assert ratelimit.check("report", "10.0.0.6", now=1061.0) is None
