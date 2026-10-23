"""引导链状态：UEFI 固件能力、引导脚本链与菜单可用性。"""

from pathlib import Path

from control_plane.app import boot_assets, config

DNSMASQ = """\
dhcp-match=set:efi64,option:client-arch,7
dhcp-boot=tag:efi64,snponly.efi
dhcp-userclass=set:ipxe,iPXE
dhcp-boot=tag:ipxe,boot.ipxe
"""

BOOT_IPXE = """\
#!ipxe
chain --autofree boot.ipxe.cfg ||
chain --replace --autofree ${menu-url} ||
"""

BOOT_CFG = """\
#!ipxe
set https-url https://${controller_ip}:${https-port}/
set chain-url ${https-url}file
set menu-url ${https-url}tftp/menu.ipxe
"""

FIRMWARE = (
    b"iPXE 2.0.0+ (ge6e51)\x00HTTPS\x00efi_nvs\x00"
    b"keygen: device key already exists\x00pubkey: no device key\x00"
    b"Usage: sign\x00Trusted server certificate fingerprint (TOFU)\x00"
)

MENU = """\
#!ipxe
:start
menu iPXE boot menu for ${initiator-iqn}
item --key b boot-os    Boot ${os}
item --key d menu-diag  System tools...

:boot-os
set root-path nvme://${storager-ip}:${port}/${disk-nqn}
sanboot ${root-path} || goto failed

:menu-diag
menu Diagnostic tools
item memtest Memtest86+
item --gap
item setup  Settings

:memtest
set base-url ${chain-url}/tools
initrd ${base-url}/memtest.iso
chain memdisk iso raw || goto failed
"""


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    tftp = tmp_path / "tftp"
    tftp.mkdir()
    (tftp / "snponly.efi").write_bytes(FIRMWARE)
    (tftp / "boot.ipxe").write_text(BOOT_IPXE, encoding="utf-8")
    (tftp / "boot.ipxe.cfg").write_text(BOOT_CFG, encoding="utf-8")
    (tftp / "menu.ipxe").write_text(MENU, encoding="utf-8")
    conf = tmp_path / "dnsmasq.conf"
    conf.write_text(DNSMASQ, encoding="utf-8")
    return conf, tftp


def test_inventory_reports_uefi_firmware_chain_and_menu(tmp_path):
    conf, tftp = _tree(tmp_path)
    data = boot_assets.inventory(conf, tftp)

    assert len(data["firmware"]) == 1
    firmware = data["firmware"][0]
    assert firmware["mode"] == "uefi"
    assert firmware["file"] == "snponly.efi"
    assert firmware["status"] == "ok"
    assert firmware["version"] == "2.0.0+ (ge6e51)"
    assert all(cap["present"] for cap in firmware["capabilities"])
    assert firmware["host_path"] == "tftp/snponly.efi"

    assert {item["file"] for item in data["stage2"]} == {"boot.ipxe", "boot.ipxe.cfg", "menu.ipxe"}
    assert all(item["status"] == "ok" for item in data["stage2"])
    assert data["menu"] == {"status": "ok", "items": 4}


def test_firmware_capability_gap_is_flagged(tmp_path):
    """文件存在但缺少 boot.ipxe 依赖的命令时必须标出来，不能只看文件在不在。"""
    conf, tftp = _tree(tmp_path)
    (tftp / "snponly.efi").write_bytes(FIRMWARE.replace(b"Usage: sign\x00", b""))

    uefi = boot_assets.inventory(conf, tftp)["firmware"][0]
    assert uefi["status"] == "incomplete"
    assert [cap["id"] for cap in uefi["capabilities"] if not cap["present"]] == ["sign"]


def test_missing_firmware_and_empty_script_are_reported(tmp_path):
    conf, tftp = _tree(tmp_path)
    (tftp / "snponly.efi").unlink()
    (tftp / "boot.ipxe.cfg").write_bytes(b"")

    data = boot_assets.inventory(conf, tftp)
    assert data["firmware"][0]["status"] == "missing"
    chain = {item["file"]: item for item in data["stage2"]}
    assert chain["boot.ipxe.cfg"]["status"] == "empty"


def test_menu_unavailable_without_menu_file(tmp_path):
    conf, tftp = _tree(tmp_path)
    (tftp / "menu.ipxe").unlink()

    data = boot_assets.inventory(conf, tftp)
    assert data["menu"] == {"status": "unavailable", "items": 0}
    assert {item["file"]: item["status"] for item in data["stage2"]}["menu.ipxe"] == "missing"


def test_escaping_paths_are_rejected_and_never_read(tmp_path):
    (tmp_path / "outside.ipxe").write_text(
        "menu leaked\nitem a Leaked\n", encoding="utf-8"
    )
    tftp = tmp_path / "tftp"
    tftp.mkdir()
    (tftp / "snponly.efi").write_bytes(FIRMWARE)
    (tftp / "boot.ipxe").write_text(
        "chain --autofree ../outside.cfg ||\n", encoding="utf-8"
    )
    conf = tmp_path / "dnsmasq.conf"
    conf.write_text(
        "dhcp-boot=tag:efi64,../outside.efi\n"
        "dhcp-boot=tag:ipxe,boot.ipxe\n",
        encoding="utf-8",
    )

    data = boot_assets.inventory(conf, tftp)
    assert data["firmware"][0]["status"] == "rejected"
    chain = {item["file"]: item["status"] for item in data["stage2"]}
    assert chain == {"boot.ipxe": "ok", "../outside.cfg": "rejected"}
    assert data["menu"] == {"status": "unavailable", "items": 0}


def test_boot_assets_endpoint(tmp_path, client, auth_headers, monkeypatch):
    conf, tftp = _tree(tmp_path)
    monkeypatch.setattr(config, "DEFAULT_DNSMASQ_CONF", str(conf))
    monkeypatch.setattr(config, "DEFAULT_TFTP_DIR", str(tftp))

    res = client.get("/boot-assets", headers=auth_headers)
    assert res.status_code == 200
    body = res.json()
    assert body["firmware"][0]["file"] == "snponly.efi"
    assert body["menu"]["status"] == "ok"


def test_boot_assets_endpoint_reports_missing_sources(tmp_path, client, auth_headers, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_DNSMASQ_CONF", str(tmp_path / "nope.conf"))
    monkeypatch.setattr(config, "DEFAULT_TFTP_DIR", str(tmp_path))

    res = client.get("/boot-assets", headers=auth_headers)
    assert res.status_code == 404
