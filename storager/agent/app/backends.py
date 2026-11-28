"""iSCSI 后端驱动（stgt / lio）与后端工厂。

两个驱动都通过容器内命令行（tgtadm / targetcli）操作一个独立的 iSCSI 容器；
接口契约与 nvmet 后端（宿主原生，见 nvmet.py）一致，路由层不感知后端差异。
"""

import abc
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import docker
from fastapi import HTTPException

from .config import (DEFAULT_ISCSI_CONTAINER, DEFAULT_NVMET_CACHE_FILE,
                     DEFAULT_NVMET_HOST_SOCKET, DEFAULT_PKI_DIR)
from .runtime import BACKEND, DISK_DIR, IQN_BASE, NQN_BASE

log = logging.getLogger("agent")


class Backend(abc.ABC):
    def __init__(self):
        self.client = docker.from_env()
        self.container = DEFAULT_ISCSI_CONTAINER  # compose 服务名（部署清单职责）

    def _exec(self, cmd: str) -> str:
        try:
            c = self.client.containers.get(self.container)
        except docker.errors.NotFound:
            raise HTTPException(503, f"iscsi container not found: {self.container}")
        except docker.errors.DockerException as e:
            raise HTTPException(503, f"docker error: {e}")
        res = c.exec_run(["sh", "-c", cmd])
        out = res.output.decode(errors="replace") if res.output else ""
        if res.exit_code != 0:
            raise HTTPException(500, f"cmd failed rc={res.exit_code}: {out.strip()}")
        return out

    def _exec_safe(self, cmd: str) -> None:
        try:
            c = self.client.containers.get(self.container)
            c.exec_run(["sh", "-c", cmd])
        except Exception:
            pass

    def validate_disk_file(self, path: str) -> None:
        """克隆建盘前的母盘校验（默认无约束；nvmet 后端覆盖为 4K 原生 GPT 校验）。"""

    @abc.abstractmethod
    def create_target(self, iqn: str, backing: str, cd: bool) -> None: ...
    @abc.abstractmethod
    def delete_target(self, iqn: str) -> None: ...
    @abc.abstractmethod
    def list_targets(self) -> list: ...
    @abc.abstractmethod
    def capabilities(self) -> dict: ...
    @abc.abstractmethod
    def wait_ready(self) -> None: ...
    @abc.abstractmethod
    def startup(self) -> Optional[dict]: ...
    @abc.abstractmethod
    def scan(self) -> dict: ...


class StgtBackend(Backend):

    def _next_tid(self, targets: list) -> int:
        used = {t["tid"] for t in targets}
        tid = 1
        while tid in used:
            tid += 1
        return tid

    def list_targets(self) -> list:
        out = self._exec("tgtadm --lld iscsi --op show --mode target")
        targets, cur = [], None
        for line in out.splitlines():
            m = re.match(r"^Target\s+(\d+):\s+(\S+)", line)
            if m:
                cur = {"tid": int(m.group(1)), "iqn": m.group(2), "luns": []}
                targets.append(cur)
                continue
            if cur is None:
                continue
            ml = re.match(r"^\s+LUN:\s+(\d+)", line)
            if ml:
                cur["luns"].append({"lun": int(ml.group(1)), "backing": None})
                continue
            mb = re.match(r"^\s+Backing store path:\s+(.+)", line)
            if mb and cur["luns"]:
                val = mb.group(1).strip()
                cur["luns"][-1]["backing"] = None if val == "None" else val
        return targets

    def create_target(self, iqn: str, backing: str, cd: bool) -> None:
        targets = self.list_targets()
        if any(t["iqn"] == iqn for t in targets):
            raise HTTPException(409, f"iqn exists: {iqn}")
        tid = self._next_tid(targets)
        self._exec(f"tgtadm --lld iscsi --op new --mode target --tid {tid} --targetname {iqn}")
        dt = " --device-type cd" if cd else ""
        try:
            self._exec(f"tgtadm --lld iscsi --op new --mode logicalunit --tid {tid} --lun 1 "
                       f"--backing-store {backing}{dt}")
            self._exec(f"tgtadm --lld iscsi --op bind --mode target --tid {tid} --initiator-address ALL")
        except HTTPException:
            self._exec_safe(f"tgtadm --lld iscsi --op delete --mode target --tid {tid}")
            raise

    def delete_target(self, iqn: str) -> None:
        targets = self.list_targets()
        t = next((x for x in targets if x["iqn"] == iqn), None)
        if not t:
            raise HTTPException(404, f"iqn not found: {iqn}")
        tid = t["tid"]
        for lun in t["luns"]:
            if lun["lun"] == 0:
                continue
            self._exec(f"tgtadm --lld iscsi --op delete --mode logicalunit --tid {tid} --lun {lun['lun']}")
        self._exec(f"tgtadm --lld iscsi --op unbind --mode target --tid {tid} --initiator-address ALL")
        self._exec(f"tgtadm --lld iscsi --op delete --mode target --tid {tid}")

    def capabilities(self) -> dict:
        return {"backend": "stgt", "cd": True, "persistent": "auto-scan on startup"}

    def wait_ready(self, retries: int = 30, interval: int = 2) -> None:
        for i in range(retries):
            try:
                self._exec("tgtadm --lld iscsi --op show --mode target")
                return
            except HTTPException:
                log.info(f"waiting stgt ready... ({i+1}/{retries})")
                time.sleep(interval)
        raise RuntimeError("stgt not ready after retries")

    def scan(self) -> dict:
        # 母盘（*_tpl_*）由 MasterScanner 单独管理（克隆建盘专用），
        # 目录扫描跳过母盘文件，不为其创建普通 target
        names = [n for n in os.listdir(DISK_DIR)
                 if n.lower().endswith((".img", ".iso")) and "_tpl_" not in n.lower()]
        created, skipped = [], []
        existing = {t["iqn"] for t in self.list_targets()}
        for name in names:
            if name.lower().endswith(".iso"):
                iqn, cd = f"{IQN_BASE}:{name}".lower(), True
            else:
                iqn, cd = f"{IQN_BASE}:{name.rsplit('.', 1)[0]}".lower(), False
            if iqn in existing:
                skipped.append(iqn)
                continue
            try:
                self.create_target(iqn, f"{DISK_DIR}/{name}", cd=cd)
            except HTTPException as e:
                log.error(f"scan create failed for {name}: {e.detail}")
                continue
            existing.add(iqn)
            created.append({"iqn": iqn, "cd": cd})
        return {"created": created, "skipped": skipped}

    def startup(self) -> Optional[dict]:
        result = self.scan()
        log.info(f"stgt auto-scan done: created={len(result['created'])} skipped={len(result['skipped'])}")
        return result


class LioBackend(Backend):

    @staticmethod
    def _bs_name(iqn: str) -> str:
        return iqn.split(":", 1)[1] if ":" in iqn else iqn

    def list_targets(self) -> list:
        out = self._exec("targetcli ls /iscsi")
        targets, cur = [], None
        for line in out.splitlines():
            m = re.search(r"o- (iqn\.\S+?)\s+\.", line)
            if m:
                cur = {"iqn": m.group(1), "luns": []}
                targets.append(cur)
                continue
            ml = re.search(r"o- lun\d+\s+.*\[fileio/\S+\s+\((.+?)\)", line)
            if ml and cur is not None:
                cur["luns"].append({"backing": ml.group(1)})
        return targets

    def create_target(self, iqn: str, backing: str, cd: bool) -> None:
        if cd:
            raise HTTPException(400, "lio backend does not support cd (ISO optical drive); use stgt for cd")
        if any(t["iqn"] == iqn for t in self.list_targets()):
            raise HTTPException(409, f"iqn exists: {iqn}")
        bs = self._bs_name(iqn)
        self._exec(f"targetcli /backstores/fileio create name={bs} file_or_dev={backing} write_back=true")
        try:
            self._exec(f"targetcli /iscsi create {iqn}")
            self._exec(f"targetcli /iscsi/{iqn}/tpg1/luns create /backstores/fileio/{bs}")
            self._exec(f"targetcli /iscsi/{iqn}/tpg1 set attribute generate_node_acls=1")
            self._exec(f"targetcli /iscsi/{iqn}/tpg1 set attribute demo_mode_write_protect=0")
            self._exec("targetcli saveconfig")
        except HTTPException:
            self._exec_safe(f"targetcli /iscsi delete {iqn}")
            self._exec_safe(f"targetcli /backstores/fileio delete {bs}")
            self._exec_safe("targetcli saveconfig")
            raise

    def delete_target(self, iqn: str) -> None:
        if not any(t["iqn"] == iqn for t in self.list_targets()):
            raise HTTPException(404, f"iqn not found: {iqn}")
        bs = self._bs_name(iqn)
        self._exec(f"targetcli /iscsi delete {iqn}")
        self._exec_safe(f"targetcli /backstores/fileio delete {bs}")
        self._exec("targetcli saveconfig")

    def capabilities(self) -> dict:
        return {"backend": "lio", "cd": False, "persistent": "saveconfig (auto-load on start)"}

    def wait_ready(self, retries: int = 30, interval: int = 2) -> None:
        for i in range(retries):
            try:
                self._exec("targetcli ls /iscsi")
                return
            except HTTPException:
                log.info(f"waiting lio (targetclid) ready... ({i+1}/{retries})")
                time.sleep(interval)
        raise RuntimeError("lio not ready after retries")

    def scan(self) -> dict:
        # 母盘（*_tpl_*）由 MasterScanner 单独管理（克隆建盘专用），
        # 目录扫描跳过母盘文件，不为其创建普通 target
        names = [n for n in os.listdir(DISK_DIR)
                 if n.lower().endswith((".img", ".iso")) and "_tpl_" not in n.lower()]
        created, skipped = [], []
        existing = {t["iqn"] for t in self.list_targets()}
        for name in names:
            if name.lower().endswith(".iso"):
                log.info(f"lio skips iso (cd unsupported): {name}")
                skipped.append(f"{IQN_BASE}:{name}".lower())
                continue
            iqn = f"{IQN_BASE}:{name.rsplit('.', 1)[0]}".lower()
            if iqn in existing:
                skipped.append(iqn)
                continue
            try:
                self.create_target(iqn, f"{DISK_DIR}/{name}", cd=False)
            except HTTPException as e:
                log.error(f"scan create failed for {name}: {e.detail}")
                continue
            existing.add(iqn)
            created.append({"iqn": iqn, "cd": False})
        return {"created": created, "skipped": skipped}

    def startup(self) -> Optional[dict]:
        log.info("lio backend: targets auto-loaded from saveconfig, no startup scan needed")
        return None


def create_backend() -> Backend:
    if BACKEND == "stgt":
        return StgtBackend()
    if BACKEND == "lio":
        return LioBackend()
    if BACKEND == "nvmet":
        # 宿主原生 nvmet（C4）：经 nvmet-host 服务调 configfs；配置按需读取，不影响其他后端
        from .nvmet import NvmetBackend, NvmetCredentialCache, NvmetHostClient
        host = NvmetHostClient(DEFAULT_NVMET_HOST_SOCKET, Path(DEFAULT_PKI_DIR))
        cache = NvmetCredentialCache(host, DEFAULT_NVMET_CACHE_FILE)
        return NvmetBackend(host, cache, DISK_DIR, NQN_BASE)
    raise RuntimeError(f"unknown backend: {BACKEND} (expect stgt|lio|nvmet)")


backend = create_backend()
