"""引导链资产状态：UEFI 固件、引导脚本链与菜单是否可用。

只回答状态，不展开菜单项：固件是否就位且具备 boot.ipxe 依赖的命令、脚本链是否齐全、
菜单是否可解析。工具菜单（Memtest/WinPE）与 iSCSI 安装器不属于 NVMe-oF 主路径，不检查。

固件能力按二进制中的命令/配置字符串判定——文件存在但缺 keygen/pubkey/sign 的 stock iPXE
同样无法完成设备认证，不能只看文件是否存在。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

_SET_RE = re.compile(r"^set\s+([A-Za-z0-9_-]+)\s+(.+?)\s*$")
_DNSMASQ_BOOT_RE = re.compile(r"^dhcp-boot=tag:([A-Za-z0-9_-]+),([^,\s]+)")
_VERSION_RE = re.compile(rb"\b\d+\.\d+\.\d+\+?(?: \(g[0-9a-f]+\))?")

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


def _menu_path(url: str) -> str | None:
    """menu-url 只接受 nginx 同源的 /tftp 前缀；其它地址无法从本地判定。"""
    for prefix in ("${https-url}tftp/",):
        if url.startswith(prefix):
            return url[len(prefix):]
    if url.startswith("tftp://"):
        _, _, rest = url.partition("://")
        return rest.split("/", 1)[1] if "/" in rest else ""
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


def inventory(dnsmasq_conf: str | Path, tftp_dir: str | Path) -> dict:
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
    cfg_name = next(
        (token.rstrip("\\") for line in boot_text.splitlines()
         if line.strip().startswith("chain")
         for token in line.split()[1:]
         if not token.startswith("--") and token.rstrip("\\").endswith(".cfg")),
        None,
    )
    cfg_text = _read_inside(tftp, cfg_name) or ""
    menu_url = next(
        (m.group(2) for raw in cfg_text.splitlines()
         if (m := _SET_RE.match(_strip_comment(raw))) and m.group(1) == "menu-url"),
        None,
    )
    menu_rel = _menu_path(menu_url) if menu_url else None
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
    }
