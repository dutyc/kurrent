"""盘文件操作、母盘扫描与命名/路径白名单。

所有外部输入（盘名、母盘名、IQN/NQN）在进入文件系统或后端调用前必须经过
本模块校验：名称白名单 + DISK_DIR realpath 越界检查，防止路径穿越。
"""

import logging
import os
import re
import shutil
import threading

from fastapi import HTTPException

from .runtime import DISK_DIR, IQN_BASE

log = logging.getLogger("agent")

FICLONE = 0x40049409


def file_exists(path: str) -> bool:
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


def fs_type(path: str) -> str:
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


def clone_master(master_path: str, backing: str) -> None:
    if _reflink(master_path, backing):
        log.info(f"clone by reflink (instant): {backing}")
    else:
        # 诊断回退原因：ZFS 需 OpenZFS >= 2.2 且母盘与克隆盘在同一数据集才支持文件级 reflink
        fs = fs_type(backing)
        if fs == "zfs":
            if not _same_fs(master_path, backing):
                log.warning(f"clone by copy (ZFS reflink requires master and work disk in the same dataset): {backing}")
            else:
                log.warning(f"clone by copy (ZFS file-level reflink requires OpenZFS >= 2.2): {backing}")
        else:
            log.warning(f"clone by copy (reflink unsupported on {fs}): {backing}")
        shutil.copy(master_path, backing)


def make_empty(backing: str, size: str) -> None:
    with open(backing, "wb") as f:
        f.truncate(_parse_size(size))


def remove_file(path: str) -> None:
    try:
        os.remove(path)
        log.info(f"deleted backing file: {path}")
    except OSError as e:
        # 不再静默吞掉：删除失败要留痕，便于排查路径/权限问题
        log.warning(f"failed to remove backing file {path}: {e}")


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


def iqn_to_filename(iqn: str) -> str:
    ident = iqn.split(":", 1)[1] if ":" in iqn else iqn
    return f"{ident}.img"


_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._:-]*$")
_SAFE_ID_RE = re.compile(r"^[a-z0-9._:-]+$")


def safe_name(value: str, field: str) -> str:
    """文件名/母盘名白名单：拒绝路径分隔符、`..`、隐藏名与超长名。"""
    v = (value or "").strip()
    if (not v or len(v) > 255 or v in (".", "..") or ".." in v
            or "/" in v or "\\" in v or "\x00" in v
            or not _SAFE_NAME_RE.fullmatch(v)):
        raise HTTPException(400, f"invalid {field}")
    return v


def disk_path(name: str, field: str) -> str:
    """把名称限定在 DISK_DIR 内（realpath 解析后仍在根目录下）。"""
    name = safe_name(name, field)
    root = os.path.realpath(DISK_DIR)
    path = os.path.realpath(os.path.join(root, name))
    if not path.startswith(root + os.sep):
        raise HTTPException(400, f"invalid {field}: path escapes disk directory")
    return path


def check_iqn(iqn: str) -> None:
    prefix = f"{IQN_BASE}:"
    value = (iqn or "").strip().lower()
    if not value.startswith(prefix):
        raise HTTPException(400, f"iqn base mismatch: expect prefix '{prefix}', got '{iqn}'")
    suffix = value[len(prefix):]
    if (not suffix or len(suffix) > 200 or "/" in suffix or "\\" in suffix
            or ".." in suffix or not _SAFE_ID_RE.fullmatch(suffix)):
        raise HTTPException(400, "invalid iqn")


def safe_nqn(value: str, field: str) -> str:
    v = (value or "").strip().lower()
    if (not v or len(v) > 300 or "/" in v or "\\" in v or ".." in v
            or not v.startswith("nqn.") or not _SAFE_ID_RE.fullmatch(v)):
        raise HTTPException(400, f"invalid {field}")
    return v


def safe_identifier(value: str, field: str = "iqn") -> str:
    """盘标识（iqn./nqn. 任一形态）白名单：不允许路径分隔符与 `..`，防止拼进宿主 URL 后穿越。"""
    v = (value or "").strip().lower()
    if (not v or len(v) > 300 or "/" in v or "\\" in v or ".." in v
            or not v.startswith(("iqn.", "nqn.")) or not _SAFE_ID_RE.fullmatch(v)):
        raise HTTPException(400, f"invalid {field}")
    return v


def agent_disk_path(backing: str) -> str:
    """宿主服务视角的 backing 路径 → agent 容器内路径（同一宿主机目录）。

    configfs 里的 backing 可能被手工写入任意路径；按 basename 重拼到 DISK_DIR
    并做 realpath 越界检查，保证 delete_file 只删盘目录内的文件。"""
    return disk_path(os.path.basename(backing), "backing")
