"""dnsmasq hosts 台账：绑定、吊销封禁与解除。"""

from control_plane.app.dnsmasq import IGNORE, DnsmasqHosts, HostBinding

MAC_A = "00:11:22:33:44:55"
MAC_B = "00:11:22:33:44:66"


def _hosts(tmp_path):
    return DnsmasqHosts(tmp_path / "dhcp-hosts.conf")


def test_block_removes_binding_and_writes_ignore(tmp_path):
    hosts = _hosts(tmp_path)
    hosts.add_binding(MAC_A, "worker-01")

    hosts.block(MAC_A)

    text = hosts.hosts_file.read_text(encoding="utf-8")
    assert f"{MAC_A},{IGNORE}" in text
    assert "worker-01" not in text
    assert hosts.list_bindings() == []


def test_add_binding_clears_stale_block(tmp_path):
    hosts = _hosts(tmp_path)
    hosts.block(MAC_A)

    hosts.add_binding(MAC_A, "worker-01")

    text = hosts.hosts_file.read_text(encoding="utf-8")
    assert f"{MAC_A},{IGNORE}" not in text
    assert hosts.list_bindings() == [HostBinding(mac=MAC_A, hostname="worker-01")]


def test_unblock_removes_only_ignore_and_is_idempotent(tmp_path):
    hosts = _hosts(tmp_path)
    hosts.add_binding(MAC_A, "worker-01")
    hosts.block(MAC_B)

    assert hosts.unblock(MAC_B) is True
    assert hosts.unblock(MAC_B) is False
    assert hosts.list_bindings() == [HostBinding(mac=MAC_A, hostname="worker-01")]


def test_replace_binding_clears_stale_block_for_new_mac(tmp_path):
    hosts = _hosts(tmp_path)
    hosts.add_binding(MAC_A, "worker-01")
    hosts.block(MAC_B)

    old = hosts.replace_binding("worker-01", MAC_B)

    assert old == MAC_A
    assert hosts.list_bindings() == [HostBinding(mac=MAC_B, hostname="worker-01")]
    assert f"{MAC_B},{IGNORE}" not in hosts.hosts_file.read_text(encoding="utf-8")
