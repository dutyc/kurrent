"""注册窗口上限来自声明 spec.registration（GET limits + POST 校验一致）。"""

from control_plane.app import config as cfg


def test_window_limits_reported(client, auth_headers):
    body = client.get("/settings/registration-window", headers=auth_headers).json()
    assert body["limits"] == {
        "ttl_minutes_min": 1,
        "ttl_minutes_max": 60,
        "max_devices": 1000,
    }


def test_window_post_enforces_declared_limits(client, auth_headers, monkeypatch):
    monkeypatch.setattr(cfg.CONFIG.spec.registration, "window_max_ttl_minutes", 15)
    monkeypatch.setattr(cfg.CONFIG.spec.registration, "window_max_devices", 2)

    res = client.post("/settings/registration-window",
                      json={"ttl_minutes": 30}, headers=auth_headers)
    assert res.status_code == 400
    assert "15" in res.json()["detail"]

    res = client.post("/settings/registration-window",
                      json={"ttl_minutes": 5, "max_devices": 3}, headers=auth_headers)
    assert res.status_code == 400
    assert "2" in res.json()["detail"]

    res = client.post("/settings/registration-window",
                      json={"ttl_minutes": 5, "max_devices": 2}, headers=auth_headers)
    assert res.status_code == 201
    assert res.json()["limits"]["ttl_minutes_max"] == 15
    assert res.json()["limits"]["max_devices"] == 2
