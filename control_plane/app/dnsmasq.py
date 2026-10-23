import os
import re
from dataclasses import dataclass
from pathlib import Path

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


@dataclass(frozen=True)
class HostBinding:
    mac: str
    hostname: str


class DnsmasqHosts:
    """dhcp-hostsfile 台账。dnsmasq 自己 inotify 监听该文件（man: re-read when it changes），
    控制面只负责原子地改内容，不再经 docker.sock 向容器发 SIGHUP。"""

    def __init__(self, hosts_file: Path):
        self.hosts_file = hosts_file

    def list_bindings(self) -> list[HostBinding]:
        return [_parse_binding(line) for line in self._read_lines() if _parse_binding(line) is not None]

    def find_mac(self, hostname: str) -> str | None:
        for binding in self.list_bindings():
            if binding.hostname == hostname:
                return binding.mac
        return None

    def ensure_free(self, mac: str, hostname: str) -> None:
        mac = normalize_mac(mac)
        for binding in self.list_bindings():
            if binding.mac == mac:
                raise ValueError(f"mac already bound: {mac}")
            if binding.hostname == hostname:
                raise ValueError(f"hostname already bound in dnsmasq: {hostname}")

    def add_binding(self, mac: str, hostname: str) -> None:
        mac = normalize_mac(mac)
        self.ensure_free(mac, hostname)
        lines = self._read_lines()
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{mac},{hostname}")
        self._write_lines(lines)

    def remove_hostname(self, hostname: str) -> bool:
        lines = self._read_lines()
        kept: list[str] = []
        removed = False
        for line in lines:
            binding = _parse_binding(line)
            if binding and binding.hostname == hostname:
                removed = True
                continue
            kept.append(line)
        if removed:
            self._write_lines(kept)
        return removed

    def replace_binding(self, hostname: str, new_mac: str) -> str | None:
        """将该 hostname 的绑定 MAC 替换为 new_mac，返回旧 MAC；hostname 无绑定返回 None。
        新 MAC 已被其他 hostname 占用时抛 ValueError（不写入）。"""
        new_mac = normalize_mac(new_mac)
        lines = self._read_lines()
        old_mac: str | None = None
        new_lines: list[str] = []
        for line in lines:
            binding = _parse_binding(line)
            if binding and binding.hostname == hostname:
                old_mac = binding.mac
                new_lines.append(f"{new_mac},{hostname}")
            elif binding and binding.mac == new_mac:
                raise ValueError(f"mac already bound to {binding.hostname}: {new_mac}")
            else:
                new_lines.append(line)
        if old_mac is None:
            return None
        if old_mac == new_mac:
            return old_mac
        self._write_lines(new_lines)
        return old_mac

    def _read_lines(self) -> list[str]:
        self.hosts_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.hosts_file.exists():
            return []
        return self.hosts_file.read_text(encoding="utf-8").splitlines()

    def _write_lines(self, lines: list[str]) -> None:
        text = "\n".join(lines).rstrip() + "\n" if lines else ""
        # 直接截断写原文件，保持 inode 不变（不用 rename 原子替换）：
        # dhcp-hosts.conf 以文件级 bind mount 挂载进 dnsmasq 容器，rename 会更换 inode，
        # 容器内仍指向旧 inode，导致 dnsmasq 永远读不到新绑定（重建容器才生效）。
        self.hosts_file.parent.mkdir(parents=True, exist_ok=True)
        with self.hosts_file.open("w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())


def normalize_mac(mac: str) -> str:
    normalized = mac.strip().lower()
    if not MAC_RE.match(normalized):
        raise ValueError(f"invalid mac address: {mac}")
    return normalized


def _parse_binding(line: str) -> HostBinding | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    parts = [part.strip() for part in stripped.split(",")]
    if len(parts) < 2:
        return None
    mac = parts[0].lower()
    hostname = parts[1]
    if not MAC_RE.match(mac) or not hostname:
        return None
    return HostBinding(mac=mac, hostname=hostname)

