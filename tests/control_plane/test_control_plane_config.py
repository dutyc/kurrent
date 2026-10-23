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
    assert cfg.spec.boot.auto_boot_timeout_sec == 1
    assert cfg.spec.agent_timeout_sec == 10


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
