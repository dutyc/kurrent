"""控制面声明式配置（kurrent.yaml）校验与 dnsmasq.conf 派生生成测试（pydantic v2）。

覆盖：未知字段拒绝（extra="forbid"）、必填缺失报错、默认值注入、networking 五键声明、
dnsmasq.conf 渲染（subnet CIDR → netmask 推导）与幂等写。
"""

import pytest

from control_plane.app.config import ControlPlaneConfiguration, load_config
from control_plane.app.dnsmasq_conf import ensure_dnsmasq_conf, render_dnsmasq_conf

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


def _load(tmp_path, body: str) -> ControlPlaneConfiguration:
    f = tmp_path / "kurrent.yaml"
    f.write_text(body, encoding="utf-8")
    return load_config(f)


def test_minimal_accepts_and_injects_defaults(tmp_path):
    cfg = _load(tmp_path, MINIMAL)
    assert cfg.kind == "ControlPlaneConfiguration"
    assert cfg.metadata.name == "test-cp"
    assert cfg.spec.networking.interface == "enp3s0"
    assert cfg.spec.networking.subnet == "192.168.80.0/24"
    assert cfg.spec.networking.dhcp_range == "192.168.80.50,192.168.80.100"
    assert cfg.spec.networking.gateway == "192.168.80.2"
    assert cfg.spec.networking.dns == "223.5.5.5"
    # 默认值注入（PKI 策略 / 服务器证书 / 引导行为 / 数据面参数）
    assert cfg.spec.pki.bootstrap_token_ttl_minutes == 60
    assert cfg.spec.pki.component_cert_days == 90
    assert cfg.spec.pki.renew_threshold == 0.2
    assert cfg.spec.server_cert.san == "IP:127.0.0.1,DNS:localhost"
    assert cfg.spec.server_cert.days == 3650
    assert cfg.spec.boot.default_arch == "x86_64"
    assert cfg.spec.boot.menu_timeout_ms == 5000
    assert cfg.spec.boot.auto_boot_timeout_ms == 1
    assert cfg.spec.agent_timeout_sec == 10
    # 新增策略块默认值
    assert cfg.spec.pki.bootstrap_token_max_ttl_minutes == 1440
    assert cfg.spec.pki.bootstrap_token_max_uses == 100
    assert cfg.spec.pki.crl_refresh_hours == 12
    assert cfg.spec.server_cert.rotate_on_change is False
    assert cfg.spec.auth.session_ttl_hours == 12
    assert cfg.spec.auth.admin_cert_days == 365
    assert cfg.spec.auth.user_cert_days_max == 3650
    assert cfg.spec.auth.token_ttl_hours == 24
    assert cfg.spec.auth.token_max_ttl_hours == 720
    assert cfg.spec.registration.window_max_ttl_minutes == 60
    assert cfg.spec.registration.window_max_devices == 1000
    assert cfg.spec.credentials.rotation_days == 90
    assert cfg.spec.credentials.reconcile_interval_seconds == 60


def test_unknown_field_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="invalid control plane configuration"):
        _load(tmp_path, MINIMAL + "  unknownField: 1\n")


def test_missing_networking_rejected(tmp_path):
    body = """apiVersion: kurrent.io/v1
kind: ControlPlaneConfiguration
metadata:
  name: test-cp
spec:
  pki:
    bootstrapTokenTtlMinutes: 60
"""
    with pytest.raises(RuntimeError, match="invalid control plane configuration"):
        _load(tmp_path, body)


def test_networking_partial_rejected(tmp_path):
    body = MINIMAL.replace("    dns: 223.5.5.5\n", "")
    with pytest.raises(RuntimeError, match="invalid control plane configuration"):
        _load(tmp_path, body)


def test_wrong_kind_rejected(tmp_path):
    body = MINIMAL.replace("kind: ControlPlaneConfiguration", "kind: NodeConfiguration")
    with pytest.raises(RuntimeError, match="invalid control plane configuration"):
        _load(tmp_path, body)


def test_unknown_field_inside_subblock_rejected(tmp_path):
    body = MINIMAL + "  boot:\n    unknownKey: 1\n"
    with pytest.raises(RuntimeError, match="invalid control plane configuration"):
        _load(tmp_path, body)


def test_renew_threshold_bounds(tmp_path):
    body = MINIMAL + "  pki:\n    renewThreshold: 1.5\n"
    with pytest.raises(RuntimeError, match="invalid control plane configuration"):
        _load(tmp_path, body)


def test_render_dnsmasq_conf():
    rendered = render_dnsmasq_conf("enp3s0", "192.168.80.0/24", "192.168.80.50,192.168.80.100",
                                   "192.168.80.2", "223.5.5.5")
    assert "interface=enp3s0" in rendered
    assert "bind-interfaces" in rendered
    # subnet CIDR → netmask 推导
    assert "dhcp-range=192.168.80.50,192.168.80.100,255.255.255.0,12h" in rendered
    assert "dhcp-option=3,192.168.80.2" in rendered
    assert "dhcp-option=6,223.5.5.5" in rendered
    # 固定部分（TFTP/架构识别/引导分发/hosts 台账）保留
    assert "enable-tftp" in rendered
    assert "dhcp-boot=tag:ipxe,boot.ipxe" in rendered
    assert "dhcp-hostsfile=/etc/dnsmasq.d/dhcp-hosts.conf" in rendered


def test_render_netmask_derivation_for_16():
    rendered = render_dnsmasq_conf("enp3s0", "10.0.0.0/16", "10.0.0.50,10.0.0.100",
                                   "10.0.0.1", "223.5.5.5")
    assert "dhcp-range=10.0.0.50,10.0.0.100,255.255.0.0,12h" in rendered


def test_ensure_dnsmasq_conf_idempotent(tmp_path, monkeypatch):
    from control_plane.app import config as cfg

    class _Net:
        interface = "eth-test0"
        subnet = "192.168.80.0/24"
        dhcp_range = "192.168.80.50,192.168.80.100"
        gateway = "192.168.80.2"
        dns = "223.5.5.5"

    monkeypatch.setattr(cfg.CONFIG.spec, "networking", _Net())
    conf = tmp_path / "dnsmasq.conf"
    assert ensure_dnsmasq_conf(conf) is True  # 首次生成
    assert ensure_dnsmasq_conf(conf) is False  # 内容一致不重写
    assert "interface=eth-test0" in conf.read_text(encoding="utf-8")


# ── 声明期语义校验（错误在 kurrent init 之前暴露）──

@pytest.mark.parametrize("old,new,needle", [
    ("192.168.80.2", "10.1.1.1", "outside subnet"),                    # 网关不在服务网段
    ("192.168.80.50,192.168.80.100", "192.168.80.100,192.168.80.50", "start must not exceed end"),
    ("192.168.80.50,192.168.80.100", "192.168.80.0,192.168.80.100", "network or broadcast"),
    ("192.168.80.50,192.168.80.100", "10.0.0.10,10.0.0.20", "outside subnet"),
    ("223.5.5.5", "not-an-ip", "not an IP"),
])
def test_networking_semantic_errors(tmp_path, old, new, needle):
    with pytest.raises(RuntimeError, match=needle):
        _load(tmp_path, MINIMAL.replace(old, new, 1))


def test_networking_rejects_ipv6_subnet(tmp_path):
    with pytest.raises(RuntimeError, match="IPv4"):
        _load(tmp_path, MINIMAL.replace("192.168.80.0/24", "fd00::/64", 1))


def test_api_version_is_locked(tmp_path):
    with pytest.raises(RuntimeError, match="apiVersion"):
        _load(tmp_path, MINIMAL.replace("kurrent.io/v1", "kurrent.io/v2", 1))


def test_server_cert_san_rejected_when_invalid(tmp_path):
    body = MINIMAL + "  serverCert:\n    san: \"IP:not-an-ip,DNS:localhost\"\n"
    with pytest.raises(RuntimeError, match="san"):
        _load(tmp_path, body)


def test_bootstrap_default_ttl_must_not_exceed_max(tmp_path):
    body = MINIMAL + "  pki:\n    bootstrapTokenTtlMinutes: 120\n    bootstrapTokenMaxTtlMinutes: 60\n"
    with pytest.raises(RuntimeError, match="bootstrapTokenTtlMinutes"):
        _load(tmp_path, body)


def test_admin_cert_days_must_not_exceed_user_max(tmp_path):
    body = MINIMAL + "  auth:\n    adminCertDays: 4000\n    userCertDaysMax: 365\n"
    with pytest.raises(RuntimeError, match="adminCertDays"):
        _load(tmp_path, body)


def test_token_default_ttl_must_not_exceed_max(tmp_path):
    body = MINIMAL + "  auth:\n    tokenTtlHours: 900\n    tokenMaxTtlHours: 720\n"
    with pytest.raises(RuntimeError, match="tokenTtlHours"):
        _load(tmp_path, body)


def test_crl_refresh_must_be_below_next_update(tmp_path):
    body = MINIMAL + "  pki:\n    crlRefreshHours: 168\n"
    with pytest.raises(RuntimeError, match="crlRefreshHours"):
        _load(tmp_path, body)


def test_credentials_zero_disables(tmp_path):
    body = MINIMAL + "  credentials:\n    rotationDays: 0\n    reconcileIntervalSeconds: 0\n"
    cfg = _load(tmp_path, body)
    assert cfg.spec.credentials.rotation_days == 0
    assert cfg.spec.credentials.reconcile_interval_seconds == 0


def test_config_hash_is_stable_and_sensitive(tmp_path):
    from control_plane.app.config_schema import config_hash

    first = _load(tmp_path, MINIMAL)
    again = _load(tmp_path, MINIMAL)
    assert config_hash(first) == config_hash(again)
    changed = _load(tmp_path, MINIMAL.replace("enp3s0", "enp4s0", 1))
    assert config_hash(changed) != config_hash(first)


def test_settings_reflect_declaration(tmp_path, monkeypatch):
    from control_plane.app import config as cfg

    body = MINIMAL + '''  auth:
    sessionTtlHours: 2
  registration:
    windowMaxTtlMinutes: 15
    windowMaxDevices: 7
  credentials:
    rotationDays: 0
    reconcileIntervalSeconds: 0
'''
    loaded = _load(tmp_path, body)
    monkeypatch.setattr(cfg, "CONFIG", loaded)
    monkeypatch.delenv("KURRENT_CP_CREDENTIAL_ROTATION_DAYS", raising=False)
    monkeypatch.delenv("KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL", raising=False)
    assert cfg.settings.session_ttl_seconds == 7200
    assert cfg.settings.registration_window_max_ttl_minutes == 15
    assert cfg.settings.registration_window_max_devices == 7
    assert cfg.settings.credential_rotation_days == 0
    assert cfg.settings.credential_reconcile_interval == 0
