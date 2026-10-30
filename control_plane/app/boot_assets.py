"""引导链资产状态：UEFI 固件、引导脚本链与菜单是否可用。

只回答状态，不展开菜单项：固件是否就位且具备 boot.ipxe 依赖的命令、脚本链是否齐全、
菜单是否可解析。工具菜单（Memtest/WinPE）与 iSCSI 安装器不属于 NVMe-oF 主路径，不检查。

固件能力按二进制中的命令/配置字符串判定——文件存在但缺 keygen/pubkey/sign 的 stock iPXE
同样无法完成设备认证，不能只看文件是否存在。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

_SET_RE = re.compile(r"^set\s+([A-Za-z0-9_-]+)\s+(.+?)\s*$")
_DNSMASQ_BOOT_RE = re.compile(r"^dhcp-boot=tag:([A-Za-z0-9_-]+),([^,\s]+)")
_VERSION_RE = re.compile(rb"\b\d+\.\d+\.\d+\+?(?: \(g[0-9a-f]+\))?")
_HTTPS_TFTP_RE = re.compile(r"^https?://[^/]+/tftp/(.+)$")

_FIRMWARE_TAG = "efi64"
_STAGE2_TAG = "ipxe"
_FIRMWARE_MARKERS = {
    "https": ("HTTPS 引导", b"HTTPS"),
    "nvs": ("NVS 持久化", b"efi_nvs"),
    "keygen": ("设备密钥生成", b"keygen: device key already exists"),
    "pubkey": ("公钥导出", b"pubkey: no device key"),
    "sign": ("挑战签名", b"Usage: sign"),
    "tofu": ("TOFU 锚定", b"Trusted server certificate fingerprint (TOFU)"),
}
_HOST_ROOT = "tftp"


def _strip_comment(line: str) -> str:
    return line.split("#", 1)[0].strip()


def _dnsmasq_boots(text: str) -> dict[str, str]:
    boots: dict[str, str] = {}
    for raw in text.splitlines():
        match = _DNSMASQ_BOOT_RE.match(_strip_comment(raw))
        if match:
            boots.setdefault(match.group(1), match.group(2))
    return boots


def _inside(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _record(root: Path, rel: str) -> dict:
    path = f"/tftp/{rel}"
    host_path = f"{_HOST_ROOT}/{rel}"
    resolved_root = root.resolve()
    target = (root / rel).resolve()
    if not rel or not _inside(resolved_root, target):
        return {"path": path, "host_path": host_path, "status": "rejected"}
    if not target.exists():
        return {"path": path, "host_path": host_path, "status": "missing"}
    if target.is_dir():
        return {"path": path, "host_path": host_path, "status": "rejected"}
    stat = target.stat()
    return {
        "path": path,
        "host_path": host_path,
        "status": "ok" if stat.st_size > 0 else "empty",
        "size": stat.st_size,
        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
    }


def _read_inside(root: Path, rel: str | None) -> str | None:
    """只读 tftp 根内的文本；引导脚本与菜单地址来自配置文件，不可信。"""
    if not rel:
        return None
    target = (root / rel).resolve()
    if not _inside(root.resolve(), target) or not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _firmware_meta(path: Path) -> dict:
    try:
        data = path.read_bytes()
    except OSError:
        return {"version": None, "capabilities": []}
    match = _VERSION_RE.search(data)
    return {
        "version": match.group(0).decode("ascii") if match else None,
        "capabilities": [
            {"id": key, "label": label, "present": marker in data}
            for key, (label, marker) in _FIRMWARE_MARKERS.items()
        ],
    }


def _local_rel(url: str | None) -> str | None:
    """把脚本里的配置/菜单地址映射回 tftp 根内的相对路径。

    支持 HTTPS 绝对地址、${https-url}tftp/ 前缀和旧的 tftp:// 形式；
    地址不在 /tftp/ 下时返回 None（无法从本地判定，也不去读）。
    """
    if not url:
        return None
    value = url.strip().rstrip("\\")
    if value.startswith("${https-url}tftp/"):
        return value[len("${https-url}tftp/"):]
    if value.startswith("tftp://"):
        _, _, rest = value.partition("://")
        return rest.split("/", 1)[1] if "/" in rest else None
    match = _HTTPS_TFTP_RE.match(value)
    if match:
        return match.group(1)
    if "://" in value:
        return None
    return value


def _find_cfg_name(boot_text: str) -> str | None:
    for raw in boot_text.splitlines():
        line = _strip_comment(raw)
        if not line.startswith("chain"):
            continue
        for token in line.split()[1:]:
            value = token.rstrip("\\")
            if value.startswith("--") or not value.endswith(".cfg"):
                continue
            rel = _local_rel(value)
            if rel:
                return rel
    return None


def _menu_status(text: str) -> dict:
    menus = 0
    items = 0
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if line.startswith("menu "):
            menus += 1
            continue
        if not line.startswith("item"):
            continue
        parts = line[4:].split()
        if not parts or "--gap" in parts:
            continue
        while parts and parts[0].startswith("--"):
            flag = parts.pop(0)
            if flag == "--key" and parts:
                parts.pop(0)
        if parts:
            items += 1
    return {
        "status": "ok" if menus and items else "unavailable",
        "items": items,
    }


def inventory(dnsmasq_conf: str | Path, tftp_dir: str | Path, *,
              secureboot_dir: str | Path | None = None,
              boot_ca_path: str | Path | None = None,
              boot_https_port: int = 4443) -> dict:
    conf = Path(dnsmasq_conf).read_text(encoding="utf-8")
    tftp = Path(tftp_dir)
    boots = _dnsmasq_boots(conf)

    firmware = []
    declared = boots.get(_FIRMWARE_TAG)
    if declared:
        record = _record(tftp, declared)
        record.update({"mode": "uefi", "label": "UEFI", "file": declared})
        if record["status"] == "ok":
            record.update(_firmware_meta((tftp / declared).resolve()))
            if any(not cap["present"] for cap in record["capabilities"]):
                record["status"] = "incomplete"
        firmware.append(record)

    boot_script = boots.get(_STAGE2_TAG)
    boot_text = _read_inside(tftp, boot_script) or ""
    cfg_name = _find_cfg_name(boot_text)
    cfg_text = _read_inside(tftp, cfg_name) or ""
    menu_url = next(
        (m.group(2) for raw in cfg_text.splitlines()
         if (m := _SET_RE.match(_strip_comment(raw))) and m.group(1) == "menu-url"),
        None,
    )
    menu_rel = _local_rel(menu_url)
    menu_text = _read_inside(tftp, menu_rel) or ""

    stage2 = []
    for name in (boot_script, cfg_name, menu_rel):
        if not name:
            continue
        record = _record(tftp, name)
        record["file"] = name
        stage2.append(record)

    return {
        "firmware": firmware,
        "stage2": stage2,
        "menu": _menu_status(menu_text) if menu_text else {"status": "unavailable", "items": 0},
        "secureboot": secureboot_status(
            secureboot_dir if secureboot_dir is not None else tftp / "secureboot",
            boot_ca_path,
            boot_https_port,
        ),
    }


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _boot_ca_sha256(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        cert = x509.load_pem_x509_certificate(Path(path).read_bytes())
    except (OSError, ValueError):
        return None
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def _as_port(value) -> int | None:
    """manifest 里的端口可能是字符串或非法值；无法判定即 None，不猜。"""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 0 < port <= 65535 else None


def secureboot_status(
    secureboot_dir: str | Path,
    boot_ca_path: str | Path | None = None,
    configured_port: int = 4443,
) -> dict:
    """可选 Secure Boot 产物状态：签名镜像 + manifest 哈希 + 固定 CA + 引导端口。"""
    directory = Path(secureboot_dir)
    artifact = directory / "snponly-sb.efi"
    manifest_path = directory / "manifest.json"
    ca_path = Path(boot_ca_path) if boot_ca_path else None
    ca_sha256 = _boot_ca_sha256(ca_path)
    record = {
        "present": artifact.is_file(),
        "status": "missing",
        "artifact": artifact.name,
        "path": f"/tftp/secureboot/{artifact.name}",
        "artifact_sha256": None,
        "artifact_size": None,
        "manifest_present": manifest_path.is_file(),
        "manifest_matches": False,
        "signer": None,
        "built_at": None,
        "boot_https_port": configured_port,
        "port_matches": False,
        "boot_ca_present": ca_sha256 is not None,
        "boot_ca_sha256": ca_sha256,
        "boot_ca_matches": False,
    }
    if not record["present"]:
        return record

    record["artifact_sha256"] = _sha256_file(artifact)
    record["artifact_size"] = artifact.stat().st_size
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    if isinstance(manifest, dict):
        record["manifest_matches"] = (
            manifest.get("artifact_sha256") == record["artifact_sha256"]
        )
        record["signer"] = manifest.get("signer")
        record["built_at"] = manifest.get("built_at")
        # 端口是固件构建时烘进签名镜像的：与部署端口不一致 = 签名代码拨错端口，
        # 后果与 CA 不一致相同（引导失败），且改端口不需要重刷固件，必须先校验。
        manifest_port = _as_port(manifest.get("boot_https_port"))
        record["boot_https_port"] = manifest_port or configured_port
        record["port_matches"] = manifest_port is not None and manifest_port == configured_port
        manifest_ca = manifest.get("boot_ca_sha256")
        record["boot_ca_sha256"] = manifest_ca or ca_sha256
        record["boot_ca_matches"] = bool(
            manifest_ca and ca_sha256
            and str(manifest_ca).lower() == ca_sha256.lower()
        )
    record["status"] = (
        "ok"
        if record["manifest_present"] and record["manifest_matches"]
        and record["port_matches"]
        and record["boot_ca_present"] and record["boot_ca_matches"]
        else "incomplete"
    )
    return record
