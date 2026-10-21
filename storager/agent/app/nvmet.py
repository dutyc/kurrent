"""NVMe-oF 后端（C4，2026-08-22 裁定：宿主原生 nvmet + Agent HTTP 调用）。

组件（按依赖顺序）：
- NvmetHostClient：urllib 标准库 HTTPS 客户端（不加依赖），mTLS 用本组件 client cert 认证
- NvmetCredentialCache：按 Worker 跟盘的凭据缓存（JSON 落盘 0600）+ hosts 矩阵同步与幂等重放
- NvmetBackend：Backend 接口的第三实现（不继承基类——基类 __init__ 初始化 docker 客户端，
  本后端无 docker 依赖，且直接继承会与 main 循环 import；接口契约与 StgtBackend/LioBackend 一致：
  create/delete/list/capabilities/wait_ready/scan/startup）

数据流（控制面推送驱动）：
控制面（凭据设置/设备换绑/盘变更）→ Agent POST /credential → apply() 更新缓存并转调宿主服务
→ 宿主服务直写 configfs（全局 hosts/<hostnqn>/dhchap_key 写密钥即启用认证，
  再 symlink 挂到子系统 allowed_hosts/ 完成准入）。
reconcile() 幂等重放：Agent 启动（scan 后）+ 周期后台线程，覆盖宿主服务不可达窗口。
契约：blueprint/nvmeof-credential-design.md 第 6 节。
"""

import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import HTTPException

log = logging.getLogger("agent")


class NvmetHostError(Exception):
    """nvmet-host 服务调用失败：status=0 表示不可达（无 HTTP 响应）。"""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _quote(part: str) -> str:
    return urllib.parse.quote(part, safe="")


def _http_detail(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        if isinstance(payload, dict) and payload.get("detail"):
            return str(payload["detail"])
    except (ValueError, OSError):
        pass
    return f"HTTP {exc.code}"


class NvmetHostClient:
    """nvmet-host 宿主服务 HTTPS 客户端（urllib 标准库，避免加依赖）。

    本组件是宿主服务的唯一调用方：mTLS——
    用本组件的 client cert（pki_dir/client.{crt,key}）向 nvmet-host 认证，
    服务端证书由内部 CA（pki_dir/ca.crt）校验链 + SAN 校验。"""

    def __init__(self, base_url: str, pki_dir: Path, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        ctx = ssl.create_default_context(cafile=str(pki_dir / "ca.crt"))
        ctx.load_cert_chain(str(pki_dir / "client.crt"), str(pki_dir / "client.key"))
        self.ctx = ctx

    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: float | None = None) -> Any:
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raise NvmetHostError(exc.code, _http_detail(exc)) from None
        except (urllib.error.URLError, OSError) as exc:
            raise NvmetHostError(0, f"nvmet host unreachable: {exc}") from None

    def healthz(self, timeout: float | None = None) -> dict:
        """探活（configfs 就绪度）；healthz 投影用短超时，其他场景用客户端默认超时。"""
        return self._request("GET", "/healthz", timeout=timeout) or {}

    def capabilities(self) -> dict:
        return self._request("GET", "/capabilities") or {}

    def create_subsystem(self, nqn: str, backing: str) -> dict:
        return self._request("POST", "/subsystems", {"nqn": nqn, "backing": backing}) or {}

    def delete_subsystem(self, nqn: str) -> dict:
        return self._request("DELETE", f"/subsystems/{_quote(nqn)}") or {}

    def list_subsystems(self) -> list:
        return (self._request("GET", "/subsystems") or {}).get("subsystems", [])

    def set_host(self, nqn: str, hostnqn: str, secret: str) -> dict:
        return self._request("PUT", f"/subsystems/{_quote(nqn)}/hosts",
                             {"hostnqn": hostnqn, "secret": secret}) or {}

    def delete_host(self, nqn: str, hostnqn: str) -> dict:
        return self._request("DELETE", f"/subsystems/{_quote(nqn)}/hosts/{_quote(hostnqn)}") or {}

    def set_tls_key(self, nqn: str, hostnqn: str, psk: str) -> dict:
        """登记目标端 TLS-PSK（宿主服务把 psk key 注入内核全局 .nvme keyring；psk 为十六进制）。"""
        return self._request("PUT", f"/subsystems/{_quote(nqn)}/tls",
                             {"hostnqn": hostnqn, "psk": psk}) or {}

    def delete_tls_key(self, nqn: str, hostnqn: str) -> dict:
        return self._request("DELETE", f"/subsystems/{_quote(nqn)}/tls/{_quote(hostnqn)}") or {}


class NvmetCredentialCache:
    """按 Worker 跟盘的凭据缓存（控制面推送驱动）。

    期望状态 {worker_id: {secret, tls_psk, sub_nqns, host_nqns}} 由控制面推送（POST /credential），
    本缓存先落盘（0600 JSON，明文 DHHC-1 + TLS-PSK——与控制面 credentials.yml 同等密级）再转调宿主服务；
    宿主服务不可达时同步失败但缓存已更新，reconcile() 周期重放补上。
    reconcile 遇子系统 404（盘已删且未收到清理推送）→ 移除过期条目，避免死重试。
    目标端 TLS-PSK（宿主服务写入内核 .nvme keyring）与 hosts 矩阵同批同步；宿主服务无 TLS
    端口时返回 409 → 记警告跳过（明文端口仍可用，属降级而非故障，见 nvmet-host main.py）。
    """

    def __init__(self, host: NvmetHostClient, path: str, interval: float = 60.0):
        self.host = host
        self.path = path
        self.interval = interval
        self.lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            self._entries = {k: v for k, v in data.items() if isinstance(v, dict)}

    def _save(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._entries, f, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def apply(self, worker_id: str, secret: str | None, sub_nqns: list[str],
              host_nqns: list[str], tls_psk: str | None = None) -> dict:
        """应用控制面推送的期望状态：更新缓存（secret=None 移除条目）→ 落盘 → 同步宿主服务。"""
        with self.lock:
            if secret:
                self._entries[worker_id] = {"secret": secret, "tls_psk": tls_psk,
                                            "sub_nqns": list(sub_nqns), "host_nqns": list(host_nqns)}
            else:
                self._entries.pop(worker_id, None)
            self._save()
            return self._sync(worker_id, secret, sub_nqns, host_nqns, tls_psk)

    def reconcile(self) -> dict:
        """幂等重放全部缓存条目（Agent 启动 scan 后 + 周期线程）。"""
        with self.lock:
            applied, failed = 0, 0
            for worker_id, entry in list(self._entries.items()):
                try:
                    self._sync(worker_id, entry.get("secret"),
                               entry.get("sub_nqns", []), entry.get("host_nqns", []),
                               entry.get("tls_psk"))
                    applied += 1
                except NvmetHostError as exc:
                    if exc.status == 404:
                        self._entries.pop(worker_id, None)  # 子系统已删：过期条目，丢弃
                        self._save()
                        log.info(f"credential cache: dropped stale entry worker={worker_id}")
                        applied += 1
                    else:
                        log.warning(f"credential reconcile failed: worker={worker_id}: {exc.detail}")
                        failed += 1
            return {"applied": applied, "failed": failed}

    def _sync(self, worker_id: str, secret: str | None, sub_nqns: list[str],
              host_nqns: list[str], tls_psk: str | None = None) -> dict:
        """同步单个 worker 的 hosts 矩阵 + 目标端 TLS-PSK：
        secret 非空 → set_host + set_tls_key；空 → delete_host + delete_tls_key（404 幂等视为成功）。"""
        tls_ok = 0
        for sub in sub_nqns:
            for host in host_nqns:
                if secret:
                    self.host.set_host(sub, host, secret)
                else:
                    try:
                        self.host.delete_host(sub, host)
                    except NvmetHostError as exc:
                        if exc.status != 404:
                            raise
                if tls_psk:
                    try:
                        self.host.set_tls_key(sub, host, tls_psk)
                        tls_ok += 1
                    except NvmetHostError as exc:
                        # 409 = 宿主服务未启用 TLS 端口（降级为明文）；404 = 子系统已删（由
                        # reconcile 的 404 分支回收条目）。两者都不该让认证凭据同步失败。
                        if exc.status not in (404, 409):
                            raise
                        log.warning(f"tls psk not applied: worker={worker_id} sub={sub}: {exc.detail}")
                elif not secret:
                    # 吊销（secret=None）→ 同步吊销目标端 PSK；密钥在但未带 PSK（旧控制面）
                    # → 保持不动，避免误清已有 PSK
                    try:
                        self.host.delete_tls_key(sub, host)
                    except NvmetHostError as exc:
                        if exc.status != 404:
                            raise
        return {"worker_id": worker_id, "secret": bool(secret),
                "tls": bool(tls_psk), "tls_applied": tls_ok,
                "sub_count": len(sub_nqns), "host_count": len(host_nqns)}


def to_nqn(iqn: str) -> str:
    """盘 IQN 形态 → NVMe NQN（同后缀前缀变换）。
    盘标识权威 = NQN（NVMe-oF 首选），IQN 由 NQN 派生；create/delete 入参为控制面传的
    派生 IQN 形态，此处还原为 NQN 写 configfs（NVMe Base Spec §7.9 要求 nqn. 前缀，
    发起端拒绝 iqn. 前缀的子系统 NQN）。"""
    iqn = iqn.strip().lower()
    if iqn.startswith("nqn."):
        return iqn
    if iqn.startswith("iqn."):
        return "nqn." + iqn[4:]
    return "nqn." + iqn


def nqn_to_iqn(nqn: str) -> str:
    """NVMe NQN → iSCSI IQN（同后缀前缀变换，派生方向：NQN 权威，IQN 自动生成）。"""
    nqn = nqn.strip().lower()
    if nqn.startswith("iqn."):
        return nqn
    if nqn.startswith("nqn."):
        return "iqn." + nqn[4:]
    return "iqn." + nqn


def disk_gpt_layout(path: str) -> str:
    """盘文件 GPT 扇区布局探测：'4k' | '512' | 'none'（无 GPT 签名或读取失败）。

    GPT 头位于 LBA1：512 字节扇区盘 → offset 512；4096 字节扇区盘 → offset 4096。
    用途：nvmet file backend 的逻辑块大小跟随底层 FS 块（典型 4096 B），512 扇区布局的
    母盘在 4K 导出下被发起端按 4K 解释、EDK2 找不到 ESP（生产实测 sanboot EFI_NOT_FOUND），
    建盘前须拒绝（NvmetBackend.validate_disk_file）。"""
    try:
        with open(path, "rb") as f:
            for offset, layout in ((4096, "4k"), (512, "512")):
                f.seek(offset)
                if f.read(8) == b"EFI PART":
                    return layout
    except OSError:
        pass
    return "none"


class NvmetBackend:
    """nvmet 后端（宿主原生，第三实现）。

    不继承 Backend 基类（其 __init__ 初始化 docker 客户端，本后端无 docker 依赖；
    直接继承会与 main 循环 import）——接口契约与 StgtBackend/LioBackend 一致，
    由 main._make_backend 按声明 spec.agent.backend=nvmet 分支实例化。
    子系统标识使用 NQN（盘标识权威）：入参 iqn 为控制面盘标识的派生形态（由盘 NQN 同后缀
    变换而来），内部经 to_nqn 还原为 NQN 写入 configfs（nvmet 不接受 iqn. 前缀的子系统 NQN）。"""

    def __init__(self, host: NvmetHostClient, cache: NvmetCredentialCache,
                 disk_dir: str, nqn_base: str):
        self.host = host
        self.cache = cache
        self.disk_dir = disk_dir
        self.nqn_base = nqn_base.rstrip(":")
        self.iqn_base = nqn_to_iqn(self.nqn_base)  # 派生：scan 候选标识（iSCSI 形态）

    def validate_disk_file(self, path: str) -> None:
        """母盘克隆前的盘文件校验（nvmet file backend 4K 块导出的硬约束）。

        nvmet file backend 按底层 FS 块（典型 4096 B）导出逻辑块：512 字节扇区布局的
        GPT 母盘在 4K 导出下，发起端按 4K 扇区解释分区表，EDK2 找不到 ESP
        （生产实测 sanboot EFI_NOT_FOUND）——建盘阶段直接拒绝并给出重建指引；
        无 GPT 签名（raw/空盘）仅告警放行，由使用方自负。"""
        layout = disk_gpt_layout(path)
        if layout == "512":
            raise HTTPException(400, (
                f"master GPT is 512-byte-sector layout: {os.path.basename(path)}; "
                "nvmet file backend exports 4K logical blocks (backing FS block size) and "
                "EDK2 cannot find the ESP on such a master (sanboot EFI_NOT_FOUND). "
                "Rebuild master GPT with 4096-byte sectors "
                "(e.g. losetup --sector-size 4096 then sgdisk/parted, or partition on a 4K-native device)"
            ))
        if layout == "none":
            log.warning(f"master disk has no GPT signature (raw or MBR image?): {path}")

    def create_target(self, iqn: str, backing: str, cd: bool) -> None:
        if cd:
            raise HTTPException(400, "nvmet backend does not support cd (ISO optical drive); use stgt for cd")
        try:
            self.host.create_subsystem(to_nqn(iqn), backing)
        except NvmetHostError as exc:
            if exc.status == 409:
                raise HTTPException(409, exc.detail) from exc
            raise HTTPException(503, f"nvmet host error: {exc.detail}") from exc

    def delete_target(self, iqn: str) -> None:
        try:
            self.host.delete_subsystem(to_nqn(iqn))
        except NvmetHostError as exc:
            if exc.status == 404:
                raise HTTPException(404, exc.detail) from exc
            raise HTTPException(503, f"nvmet host error: {exc.detail}") from exc

    def list_targets(self) -> list:
        try:
            subs = self.host.list_subsystems()
        except NvmetHostError as exc:
            raise HTTPException(503, f"nvmet host error: {exc.detail}") from exc
        # nvmet 的 target 标识 = 子系统 NQN：nqn 键语义正确，iqn 键保留历史接口契约
        return [{"iqn": s["nqn"], "nqn": s["nqn"],
                 "luns": [{"lun": ns.get("nsid"), "backing": ns.get("device_path")}
                          for ns in s.get("namespaces", [])]}
                for s in subs]

    def capabilities(self) -> dict:
        try:
            caps = self.host.capabilities()
        except NvmetHostError as exc:
            raise HTTPException(503, f"nvmet host error: {exc.detail}") from exc
        # 宿主服务不知道 KURRENT_NQN_BASE：本地补 base_nqn（控制面建盘生成盘 NQN 依赖此键）
        caps["base_nqn"] = self.nqn_base
        return caps

    def wait_ready(self, retries: int = 30, interval: int = 2) -> None:
        for i in range(retries):
            try:
                if self.host.healthz().get("configfs"):
                    return
            except NvmetHostError:
                pass
            log.info(f"waiting nvmet host ready... ({i+1}/{retries})")
            time.sleep(interval)
        raise RuntimeError("nvmet host not ready after retries")

    def scan(self) -> dict:
        # 母盘（*_tpl_*）由 MasterScanner 单独管理（克隆建盘专用），
        # 目录扫描跳过母盘文件，不为其创建普通子系统
        names = [n for n in os.listdir(self.disk_dir)
                 if n.lower().endswith((".img", ".iso")) and "_tpl_" not in n.lower()]
        created, skipped = [], []
        # 子系统标识是 NQN（list_targets 返回）：existing 集合统一 NQN 形态
        existing = {t.get("nqn") or t.get("iqn") for t in self.list_targets()}
        for name in names:
            if name.lower().endswith(".iso"):
                log.info(f"nvmet skips iso (cd unsupported): {name}")
                skipped.append(f"{self.iqn_base}:{name}".lower())
                continue
            iqn = f"{self.iqn_base}:{name.rsplit('.', 1)[0]}".lower()
            if to_nqn(iqn) in existing:
                skipped.append(iqn)
                continue
            try:
                self.create_target(iqn, f"{self.disk_dir}/{name}", cd=False)
            except HTTPException as exc:
                log.error(f"scan create failed for {name}: {exc.detail}")
                continue
            existing.add(to_nqn(iqn))
            created.append({"iqn": iqn, "cd": False})
        return {"created": created, "skipped": skipped}

    def startup(self) -> dict | None:
        result = self.scan()
        res = self.cache.reconcile()
        log.info(f"nvmet auto-scan done: created={len(result['created'])} "
                 f"skipped={len(result['skipped'])}; credential reconcile: "
                 f"applied={res['applied']} failed={res['failed']}")
        return result
