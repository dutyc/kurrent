"""Secure Boot 运维脚本：离线密钥、只导公开物、只读预检。"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools" / "secureboot"


def test_keygen_creates_only_the_artifact_key(tmp_path):
    out = tmp_path / "offline"
    subprocess.run([str(TOOLS / "keygen-offline.sh"), str(out), "Test Signer"], check=True)
    assert sorted(p.name for p in out.iterdir()) == ["artifact-signing.crt", "artifact-signing.key"]
    assert (out / "artifact-signing.key").stat().st_mode & 0o777 == 0o600
    assert (out / "artifact-signing.crt").stat().st_mode & 0o777 == 0o644

    again = subprocess.run(
        [str(TOOLS / "keygen-offline.sh"), str(out), "Test Signer"],
        capture_output=True, text=True,
    )
    assert again.returncode != 0
    assert "already exists" in again.stderr


def test_keygen_refuses_repository_paths():
    result = subprocess.run(
        [str(TOOLS / "keygen-offline.sh"), str(ROOT / "offline-keys-test")],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "refusing" in result.stderr
    assert not (ROOT / "offline-keys-test").exists()


def test_export_copies_public_material_only():
    script = (TOOLS / "export-usb.sh").read_text(encoding="utf-8")
    assert "sbverify --cert" in script
    assert 'cp "$SIGNED" "$DEST/snponly-sb.efi"' in script
    assert 'cp "$MANIFEST" "$DEST/manifest.json"' in script
    assert 'cp "$BOOT_CA" "$DEST/boot-ca.crt"' in script
    assert "-outform DER -out \"$DEST/db-cert.cer\"" in script
    # 私钥既不被读取也不被复制
    assert ".key" not in script.replace("artifact-signing.key", "")
    assert "cp " not in "\n".join(line for line in script.splitlines() if ".key" in line)


def test_check_host_is_read_only():
    script = (TOOLS / "check-host.sh").read_text(encoding="utf-8")
    for forbidden in ("rm ", "dd ", "> /sys", "efi-writevar", "mokutil --import"):
        assert forbidden not in script
    assert "SecureBoot" in script
    assert "SetupMode" in script
    assert "efi-readvar -v db" in script


def test_scripts_are_executable():
    for name in ("keygen-offline.sh", "export-usb.sh", "check-host.sh", "enroll-redfish.sh"):
        mode = (TOOLS / name).stat().st_mode
        assert mode & os.X_OK, name


def test_redfish_tool_keeps_credentials_off_the_command_line():
    script = (TOOLS / "enroll-redfish.sh").read_text(encoding="utf-8")
    assert "KURRENT_BMC_USER" in script
    assert "KURRENT_BMC_PASSWORD" in script
    assert "--password" not in script
    assert "0o600" in script
    # 默认校验证书；关闭校验必须显式 --insecure
    assert 'args.insecure' in script
    assert "CERT_NONE" in script
