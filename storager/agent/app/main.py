import os
import re
import abc
import shutil
import threading
import time
import logging
import json
import datetime
from pathlib import Path
from contextlib import asynccontextmanager, contextmanager

import docker
from fastapi import FastAPI, HTTPException, Request, Depends
from pydantic import BaseModel
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agent")

FICLONE = 0x40049409


# ============================ 框架层：配置 ============================

# 节点声明式配置：业务配置收敛到 storager/kurrent.yaml
# （pydantic 校验：未知字段拒绝/必填缺失报错/默认值注入）；
# 容器内路径（盘目录挂载点/日志/缓存/pki/cp-ca/容器名）属部署清单 compose 职责，以模块常量固化；
# 一次性 bootstrap token 在独立凭据文件。
# .env 仅保留 compose 插值键。文件缺失/校验失败在 import 即抛错阻断启动。
from .config import (  # noqa: E402
    CONFIG, DEFAULT_BOOTSTRAP_TOKEN_FILE, DEFAULT_DISK_DIR, DEFAULT_ISCSI_CONTAINER,
    DEFAULT_LOG_FILE, DEFAULT_NVMET_CACHE_FILE, DEFAULT_NVMET_HOST_SOCKET, DEFAULT_PKI_DIR,
)
from . import security  # noqa: E402


def nqn_to_iqn(nqn: str) -> str:
    """NVMe NQN → iSCSI IQN（同后缀前缀变换，派生方向：NQN 权威，IQN 自动生成）：
    nqn.2026-07.com.kurrent:worker-01.ubuntu → iqn.2026-07.com.kurrent:worker-01.ubuntu。
    节点只配置 NQN 命名空间（KURRENT_NQN_BASE），iSCSI 数据面消费的 IQN 由此派生。"""
    nqn = nqn.strip().lower()
    if nqn.startswith("iqn."):
        return nqn
    if nqn.startswith("nqn."):
        return "iqn." + nqn[4:]
    return "iqn." + nqn


DISK_DIR = DEFAULT_DISK_DIR  # 容器内盘目录挂载点（宿主路径见 kurrent.yaml spec.agent.diskDir，compose 挂载关联）
NQN_BASE = CONFIG.spec.agent.nqn_base              # 权威：盘标识命名空间（NVMe-oF 首选协议）
IQN_BASE = nqn_to_iqn(NQN_BASE)                  # 派生：iSCSI 数据面（同后缀前缀变换）
BACKEND = CONFIG.spec.agent.backend
LOG_FILE = DEFAULT_LOG_FILE  # 容器内路径（compose 挂载目标，部署清单职责）

# 组件 PKI 引导/轮换：证书未就绪则抛错阻断启动（uvicorn 起服务前）
from .pki_client import ensure_pki  # noqa: E402
ensure_pki()


# ============================ 框架层：文件操作 ============================

def _file_exists(path: str) -> bool:
    return os.path.exists(path)


def _parse_size(s: str) -> int:
    s = s.strip().upper()
    units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    if s and s[-1] in units:
        return int(float(s[:-1]) * units[s[-1]])
    return int(s)


def _reflink(src: str, dst: str) -> bool:
    try:
        src_fd = os.open(src, os.O_RDONLY)
    except OSError as e:
        log.warning(f"reflink open src failed: {e}")
        return False
    try:
        dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    except OSError as e:
        os.close(src_fd)
        log.warning(f"reflink open dst failed: {e}")
        return False
    try:
        import fcntl
        fcntl.ioctl(dst_fd, FICLONE, src_fd)
        return True
    except OSError as e:
        log.info(f"reflink not used: errno={e.errno} ({e.strerror})")
        return False
    finally:
        os.close(src_fd)
        os.close(dst_fd)


def _fs_type(path: str) -> str:
    """返回 path 所在挂载点的文件系统类型（解析 /proc/self/mounts，最长挂载点匹配）。"""
    real = os.path.realpath(path)
    best, best_len = "unknown", -1
    try:
        with open("/proc/self/mounts", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                fstype = parts[2]
                if len(mnt) > best_len and (real == mnt or real.startswith(mnt.rstrip("/") + "/") or mnt == "/"):
                    best, best_len = fstype, len(mnt)
    except OSError:
        pass
    return best


def _same_fs(src: str, dst: str) -> bool:
    """源与目标是否同一文件系统（ZFS 各数据集 st_dev 不同，文件级 reflink 要求同数据集）。"""
    try:
        return os.stat(src).st_dev == os.stat(dst).st_dev
    except OSError:
        return False


def _clone_master(master_path: str, backing: str) -> None:
    if _reflink(master_path, backing):
        log.info(f"clone by reflink (instant): {backing}")
    else:
        # 诊断回退原因：ZFS 需 OpenZFS >= 2.2 且母盘与克隆盘在同一数据集才支持文件级 reflink
        fs = _fs_type(backing)
        if fs == "zfs":
            if not _same_fs(master_path, backing):
                log.warning(f"clone by copy (ZFS reflink requires master and work disk in the same dataset): {backing}")
            else:
                log.warning(f"clone by copy (ZFS file-level reflink requires OpenZFS >= 2.2): {backing}")
        else:
            log.warning(f"clone by copy (reflink unsupported on {fs}): {backing}")
        shutil.copy(master_path, backing)


def _make_empty(backing: str, size: str) -> None:
    with open(backing, "wb") as f:
        f.truncate(_parse_size(size))


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
        log.info(f"deleted backing file: {path}")
    except OSError as e:
        # 不再静默吞掉：删除失败要留痕，便于排查路径/权限问题
        log.warning(f"failed to remove backing file {path}: {e}")


# ============================ 框架层：母盘扫描 ============================

class MasterScanner:
    """后台线程周期扫描 DISK_DIR，缓存 *_tpl_* 母盘清单（name/size/mtime/gpt）。

    母盘命名约定：文件名须包含 `_tpl_` 标记（大小写不敏感，如 _tpl_ubuntu_2204.img）。
    gpt 字段 = GPT 扇区布局探测（disk_gpt_layout）：512 布局母盘不可用于 nvmet 克隆
    （EDK2 找不到 ESP），前端据此标注。
    扫描结果带锁缓存，GET /masters 直接读缓存，不阻塞文件系统。
    """

    def __init__(self, disk_dir: str, interval: float = 30.0):
        self.disk_dir = disk_dir
        self.interval = interval
        self.lock = threading.Lock()
        self.cache: list = []
        self._stop = threading.Event()

    def _scan(self) -> list:
        from .nvmet import disk_gpt_layout
        items = []
        try:
            names = os.listdir(self.disk_dir)
        except OSError as e:
            log.warning(f"master scan failed: {e}")
            return items
        for n in sorted(names):
            # 母盘判定大小写不敏感（_tpl_ / _TPL_ / _Tpl_ 皆认），与后端 scan 的跳过条件严格一致
            if "_tpl_" not in n.lower():
                continue
            p = os.path.join(self.disk_dir, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if not os.path.isfile(p):
                continue
            items.append({"name": n, "size": st.st_size, "mtime": int(st.st_mtime),
                          "gpt": disk_gpt_layout(p)})
        return items

    def start(self) -> None:
        self.refresh()
        threading.Thread(target=self._loop, daemon=True).start()
        log.info(f"master scanner started: dir={self.disk_dir} interval={self.interval}s")

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.refresh()

    def refresh(self) -> None:
        items = self._scan()
        with self.lock:
            self.cache = items

    def list(self) -> list:
        with self.lock:
            return list(self.cache)


masters = MasterScanner(DISK_DIR)


def _iqn_to_filename(iqn: str) -> str:
    ident = iqn.split(":", 1)[1] if ":" in iqn else iqn
    return f"{ident}.img"


def _check_iqn(iqn: str) -> None:
    prefix = f"{IQN_BASE}:"
    if not iqn.startswith(prefix):
        raise HTTPException(400, f"iqn base mismatch: expect prefix '{prefix}', got '{iqn}'")


# ============================ 框架层：操作日志 ============================

class OperationLog:
    """append-only JSON Lines 操作日志。id 用行号当游标（MVP 不轮转前提下成立）。"""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.next_id = 0
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.next_id = sum(1 for line in f if line.strip())

    def record(self, op: str, req: dict, result: str, client: str, **extra) -> None:
        with self.lock:
            self.next_id += 1
            entry = {"id": self.next_id,
                     # 跟随容器时区（/etc/localtime 挂载或 TZ 环境变量），输出带偏移的本地时间
                     "ts": datetime.datetime.now().astimezone().isoformat(),
                     "op": op, "req": req, "result": result, "client": client}
            entry.update(extra)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read(self, since: int = 0, limit: int = 1000) -> dict:
        entries = []
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("id", 0) > since:
                        entries.append(e)
                        if len(entries) >= limit:
                            break
        next_cursor = entries[-1]["id"] if entries else since
        return {"next_cursor": next_cursor, "entries": entries}


oplog = OperationLog(LOG_FILE)


# ============================ 框架层：mTLS 客户端证书鉴权 ============================


def verify_client_cert(request: Request) -> None:
    """mTLS 客户端证书鉴权（control CA 信任模型 + 应用层 CRL/CN）。

    TLS 层以 ssl_ca_certs=ca-control.crt + CERT_REQUIRED 强制客户端证书链；
    应用层再从 peer cert 校验 issuer/CN 与 control CRL（可热更新，无需重启）。
    """
    security.verify_peer(
        request.scope,
        allowed_cns={security.CONTROL_PLANE_CN},
        issuer_cn=security.ca_subject_cn(str(Path(DEFAULT_PKI_DIR) / "ca-control.crt")),
        crl_kind="control",
        store=security.CRL_STORE,
    )


@contextmanager
def logged(op: str, req_dict: dict, client: str):
    """写操作外壳：成功记 ok，任何失败记 failed + 原因，再抛出。"""
    try:
        yield
        oplog.record(op, req_dict, "ok", client)
    except HTTPException as e:
        oplog.record(op, req_dict, "failed", client, error=str(e.detail))
        raise
    except Exception as e:
        oplog.record(op, req_dict, "failed", client, error=str(e))
        raise


# ============================ 驱动层：后端基类 ============================

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


# ============================ 驱动层：stgt ============================

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


# ============================ 驱动层：lio ============================

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


# ============================ 框架层：实例化 + 启动钩子 ============================

def _make_backend() -> Backend:
    if BACKEND == "stgt":
        return StgtBackend()
    if BACKEND == "lio":
        return LioBackend()
    if BACKEND == "nvmet":
        # 宿主原生 nvmet（C4）：经 nvmet-host 服务调 configfs；配置按需读取，不影响其他后端
        from .nvmet import NvmetBackend, NvmetCredentialCache, NvmetHostClient
        host = NvmetHostClient(DEFAULT_NVMET_HOST_SOCKET, Path(DEFAULT_PKI_DIR))
        cache_path = DEFAULT_NVMET_CACHE_FILE
        cache = NvmetCredentialCache(host, cache_path)
        return NvmetBackend(host, cache, DISK_DIR, NQN_BASE)
    raise RuntimeError(f"unknown backend: {BACKEND} (expect stgt|lio|nvmet)")


backend = _make_backend()


def _startup() -> None:
    backend.wait_ready()
    result = backend.startup()
    if result is not None:                       # stgt/nvmet 返回 scan 结果，lio 返回 None
        oplog.record("auto_scan", {}, "ok", "local",
                     created=len(result.get("created", [])),
                     skipped=len(result.get("skipped", [])))


_reconcile_stop = threading.Event()


def _reconcile_loop() -> None:
    """nvmet 凭据缓存周期重放（仅 nvmet 后端）：覆盖宿主服务不可达窗口的推送丢失。"""
    from .nvmet import NvmetBackend
    if not isinstance(backend, NvmetBackend):
        return
    log.info(f"nvmet credential reconcile loop started: interval={backend.cache.interval}s")
    while not _reconcile_stop.wait(backend.cache.interval):
        try:
            result = backend.cache.reconcile()
            if result["failed"]:
                log.warning(f"nvmet credential reconcile: applied={result['applied']} "
                            f"failed={result['failed']}")
        except Exception as exc:
            log.warning(f"nvmet credential reconcile failed: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_startup, daemon=True).start()
    masters.start()
    threading.Thread(target=_reconcile_loop, daemon=True).start()
    log.info(f"agent started: backend={BACKEND}, base NQN={NQN_BASE}")
    yield


app = FastAPI(lifespan=lifespan)


# ============================ 框架层：请求模型 + 路由 ============================

class DiskReq(BaseModel):
    iqn: str
    master: Optional[str] = None
    size: Optional[str] = None
    filename: Optional[str] = None

class CdReq(BaseModel):
    iso: str
    iqn: Optional[str] = None

class CredentialPushReq(BaseModel):
    """NVMe-oF 凭据推送（控制面驱动，仅 nvmet 后端）：
    secret=null 吊销该 worker；sub_nqns = 该 worker 在本节点的盘子系统 NQN，host_nqns = 绑定设备派生 NQN。
    tls_psk = 目标端 TLS-PSK（由控制面从认证密钥派生，十六进制）；随 secret 一并同步/吊销。"""
    worker_id: str
    secret: Optional[str] = None
    sub_nqns: list = []
    host_nqns: list = []
    tls_psk: Optional[str] = None


# ── 后端就绪度探针（healthz 投影，仅 nvmet 后端）──
# 短超时（探活要快，宿主服务不可达时秒回）+ 短缓存（控制面/编排会周期探测，避免把探活做成重操作）
BACKEND_PROBE_TTL = 5.0
BACKEND_PROBE_TIMEOUT = 2.0
_backend_probe_lock = threading.Lock()
_backend_probe_cache: dict = {"ts": 0.0, "value": None}


def _backend_probe() -> Optional[dict]:
    """宿主服务就绪度（nvmet 后端）：可达且 configfs 就绪才算 ready；其他后端无独立探针（None）。"""
    from .nvmet import NvmetBackend, NvmetHostError
    if not isinstance(backend, NvmetBackend):
        return None
    with _backend_probe_lock:
        if time.monotonic() - _backend_probe_cache["ts"] < BACKEND_PROBE_TTL:
            return _backend_probe_cache["value"]
    try:
        info = backend.host.healthz(timeout=BACKEND_PROBE_TIMEOUT)
        ready = bool(info.get("configfs"))
        value = {"ready": ready, "reachable": True,
                 "detail": None if ready else "nvmet-host reachable; configfs not ready"}
    except NvmetHostError as exc:
        value = {"ready": False, "reachable": False, "detail": exc.detail}
    with _backend_probe_lock:
        _backend_probe_cache.update(ts=time.monotonic(), value=value)
    return value


@app.get("/healthz")                             # 唯一不保护的接口，给健康检查/探活
def healthz():
    """进程存活（status 恒 ok）+（nvmet 后端）数据面就绪度。

    backend_ready=false 表示在线但存储不可用（nvmet-host 不可达或 configfs 未就绪）——
    控制面据此把节点标为 degraded，与「节点掉线（error）」区分开。"""
    resp = {"status": "ok", "crl": security.CRL_STORE.describe()}
    probe = _backend_probe()
    if probe is not None:
        resp["backend"] = BACKEND
        resp["backend_ready"] = probe["ready"]
        if probe["detail"]:
            resp["backend_detail"] = probe["detail"]
    return resp


@app.post("/credential", dependencies=[Depends(verify_client_cert)])
def push_credential(req: CredentialPushReq, request: Request):
    """NVMe-oF 凭据推送（控制面驱动，仅 nvmet 后端）：更新缓存 + 转调宿主服务同步 hosts 矩阵。
    secret=null 吊销该 worker 全部绑定设备认证；审计不记密钥本体。"""
    from .nvmet import NvmetBackend, NvmetHostError
    if not isinstance(backend, NvmetBackend):
        raise HTTPException(400, "credential push requires backend=nvmet")
    req_dict = {"worker_id": req.worker_id, "secret": bool(req.secret),
                "sub_nqns": req.sub_nqns, "host_nqns": req.host_nqns,
                "tls": bool(req.tls_psk)}
    with logged("credential", req_dict, request.client.host):
        try:
            return backend.cache.apply(req.worker_id, req.secret, req.sub_nqns,
                                       req.host_nqns, req.tls_psk)
        except NvmetHostError as exc:
            raise HTTPException(503, f"nvmet host error: {exc.detail}") from exc


@app.post("/lun/disk", dependencies=[Depends(verify_client_cert)])
def create_disk(req: DiskReq, request: Request):
    req_dict = {"iqn": req.iqn, "master": req.master, "size": req.size, "filename": req.filename}
    with logged("disk", req_dict, request.client.host):
        _check_iqn(req.iqn)
        iqn = req.iqn.lower()
        filename = req.filename or _iqn_to_filename(iqn)
        backing = f"{DISK_DIR}/{filename}"
        if _file_exists(backing):
            raise HTTPException(409, f"file exists: {backing}")
        if req.master:
            master_path = f"{DISK_DIR}/{req.master}"
            if not _file_exists(master_path):
                raise HTTPException(404, f"master not found: {master_path}")
            backend.validate_disk_file(master_path)
            _clone_master(master_path, backing)
        elif req.size:
            _make_empty(backing, req.size)
        else:
            raise HTTPException(400, "need master (clone) or size (empty disk)")
        try:
            backend.create_target(iqn, backing, cd=False)
        except HTTPException:
            _remove_file(backing)
            raise
        return {"iqn": iqn, "backing": backing}


@app.post("/lun/cd", dependencies=[Depends(verify_client_cert)])
def create_cd(req: CdReq, request: Request):
    req_dict = {"iso": req.iso, "iqn": req.iqn}
    with logged("cd", req_dict, request.client.host):
        iso_path = f"{DISK_DIR}/{req.iso}"
        if not _file_exists(iso_path):
            raise HTTPException(404, f"iso not found: {iso_path}")
        iqn = (req.iqn or f"{IQN_BASE}:{req.iso}").lower()
        _check_iqn(iqn)
        backend.create_target(iqn, iso_path, cd=True)
        return {"iqn": iqn, "backing": iso_path}


@app.post("/lun/scan", dependencies=[Depends(verify_client_cert)])
def scan(request: Request):
    try:
        result = backend.scan()
        oplog.record("scan", {}, "ok", request.client.host,
                     created=len(result["created"]), skipped=len(result["skipped"]))
        return result
    except HTTPException as e:
        oplog.record("scan", {}, "failed", request.client.host, error=str(e.detail))
        raise


@app.delete("/lun", dependencies=[Depends(verify_client_cert)])
def delete(iqn: str, request: Request, delete_file: bool = False):
    req_dict = {"iqn": iqn, "delete_file": delete_file}
    with logged("delete", req_dict, request.client.host):
        iqn = iqn.lower()
        backings = []
        # nvmet 后端 list_targets 的 iqn 键实际为子系统 NQN（nqn. 前缀），
        # stgt/lio 后端则为 iqn. 前缀：请求可能是任一形态，前缀互转后双匹配
        alt = (
            f"nqn.{iqn[4:]}" if iqn.startswith("iqn.")
            else f"iqn.{iqn[4:]}" if iqn.startswith("nqn.")
            else None
        )
        t = next(
            (x for x in backend.list_targets()
             if x["iqn"] == iqn or (alt and x["iqn"] == alt)),
            None,
        )
        if t:
            backings = [l["backing"] for l in t["luns"] if l.get("backing")]
        backend.delete_target(iqn)
        if delete_file:
            for b in backings:
                # backing 是宿主服务视角路径（如 /srv/nvmet-disks/x.img），
                # agent 容器挂载同一目录于 KURRENT_DISK_DIR：basename 重建容器内路径
                _remove_file(_agent_disk_path(b))
        return {"deleted": iqn, "delete_file": delete_file}


def _agent_disk_path(backing: str) -> str:
    """宿主服务视角的 backing 路径 → agent 容器内路径（同一宿主机目录）。"""
    name = os.path.basename(backing)
    if name and not backing.startswith(DISK_DIR):
        return os.path.join(DISK_DIR, name)
    return backing


@app.get("/lun", dependencies=[Depends(verify_client_cert)])
def list_luns():
    return backend.list_targets()


@app.get("/masters", dependencies=[Depends(verify_client_cert)])
def list_masters():
    """列出 DISK_DIR 下 *_tpl_* 母盘（后台线程周期扫描的缓存清单，供 Control Plane / WebUI 克隆选盘）。"""
    return {"masters": masters.list()}


@app.get("/capabilities", dependencies=[Depends(verify_client_cert)])
def capabilities():
    caps = backend.capabilities()
    # 盘标识命名空间权威 = NQN（控制面建盘生成盘 NQN 依赖此键；iSCSI 数据面 IQN 由 NQN 派生）
    caps["base_nqn"] = NQN_BASE
    # 文件系统类型决定克隆方式：btrfs / ZFS(>=2.2 同数据集) / xfs(reflink 特性) 走 FICLONE 秒级，其余回退全量拷贝
    fs = _fs_type(DISK_DIR)
    caps["fs_type"] = fs
    if fs == "zfs":
        caps["clone"] = "reflink (FICLONE on OpenZFS >= 2.2, master and work disk in the same dataset) -> shutil.copy fallback"
    elif fs in ("btrfs", "xfs"):
        caps["clone"] = "reflink (FICLONE; xfs requires the reflink feature enabled) -> shutil.copy fallback"
    else:
        caps["clone"] = f"full copy only (reflink unsupported on {fs})"
    caps["empty_disk"] = "truncate (sparse)"
    return caps


@app.get("/logs", dependencies=[Depends(verify_client_cert)])
def get_logs(since: int = 0, limit: int = 1000):
    return oplog.read(since, limit)