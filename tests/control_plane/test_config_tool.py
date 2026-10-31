"""config_tool（容器内 validate/show）与运行时声明状态端点。"""

import yaml

from control_plane.app import config, config_tool, runtime_status


MINIMAL = """apiVersion: kurrent.io/v1
kind: ControlPlaneConfiguration
metadata:
  name: test-cp
spec:
  networking:
    interface: enp3s0
    subnet: 192.168.80.0/24
    dhcpRange: 192.168.80.50,192.168.80.100
    gateway: 192.168.80.2
    dns: 223.5.5.5
"""


def test_validate_ok_prints_hash(capsys):
    assert config_tool.run("validate", MINIMAL) == 0
    out = capsys.readouterr().out
    assert out.startswith("ok: test-cp")
    assert "configHash=" in out


def test_validate_reports_field_level_errors(capsys):
    assert config_tool.run("validate", MINIMAL.replace("192.168.80.0/24", "192.168.80.0")) == 2
    err = capsys.readouterr().err
    assert "subnet" in err


def test_validate_reports_unknown_field(capsys):
    bad = MINIMAL + "  unknownBlock: 1\n"
    assert config_tool.run("validate", bad) == 2
    assert "unknownBlock" in capsys.readouterr().err


def test_show_dumps_defaults(capsys):
    assert config_tool.run("show", MINIMAL) == 0
    data = yaml.safe_load(capsys.readouterr().out)
    assert data["apiVersion"] == "kurrent.io/v1"
    assert data["spec"]["auth"]["sessionTtlHours"] == 12
    assert data["spec"]["serverCert"]["rotateOnChange"] is False


def test_show_rejects_bad_yaml(capsys):
    assert config_tool.run("show", "spec: [unclosed\n") == 2
    assert "invalid YAML" in capsys.readouterr().err


def test_config_status_reports_effective_declaration(client, auth_headers):
    res = client.get("/config", headers=auth_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["apiVersion"] == "kurrent.io/v1"
    assert body["kind"] == "ControlPlaneConfiguration"
    assert body["spec"]["networking"]["interface"] == "eth-test0"
    assert body["spec"]["auth"]["sessionTtlHours"] == 12
    assert body["configHash"] == config.config_hash(config.CONFIG)
    assert body["derived"]["serverCert"]["rotateOnChange"] is False
    assert body["startup"]["errors"] == []
    assert "PRIVATE KEY" not in res.text


def test_config_status_surfaces_certificate_drift(client, auth_headers, monkeypatch):
    monkeypatch.setattr(config.CONFIG.spec.server_cert, "san", "IP:10.9.9.9,DNS:localhost")
    body = client.get("/config", headers=auth_headers).json()
    assert "san" in body["derived"]["serverCert"]["drift"]


def test_config_status_surfaces_dnsmasq_conf_mismatch(client, auth_headers, tmp_path, monkeypatch):
    conf = tmp_path / "dnsmasq.conf"
    conf.write_text("# stale\n", encoding="utf-8")
    monkeypatch.setattr(config, "DEFAULT_DNSMASQ_CONF", str(conf))
    body = client.get("/config", headers=auth_headers).json()
    assert body["derived"]["dnsmasqConf"]["matchesDeclaration"] is False


def test_startup_errors_degrade_health_but_detail_stays_authenticated(client, auth_headers):
    runtime_status.reset()
    runtime_status.record_error("dnsmasq.conf 生成失败", RuntimeError("read-only"))
    try:
        assert client.get("/healthz").status_code == 503
        body = client.get("/config", headers=auth_headers).json()
        assert any("dnsmasq.conf" in item for item in body["startup"]["errors"])
    finally:
        runtime_status.reset()
    assert client.get("/healthz").status_code == 200
