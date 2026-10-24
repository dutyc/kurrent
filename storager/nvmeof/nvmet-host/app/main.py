"""nvmet 宿主管理服务（NVMe-oF 存储接入 C4：宿主原生 nvmet + Agent HTTP 调用）。

运行形态：容器 `kurrent-nvmet-host`（`storager/nvmeof/docker-compose.yml`）——host 网络模式，
管理面只监听共享卷 UDS（`config.DEFAULT_HOST_SOCKET`，无 TCP 面），提供 mTLS（nodes CA，与 agent 同构：派生
bootstrap token 引导 + 证书轮换 + 客户端证书鉴权；token 绑 agent_id、TTL 内可复用）；因 Ubuntu
docker-default AppArmor 会拒绝 configfs 写入，容器需豁免 AppArmor（见 nvmet-host README）。
直接操作内核 configfs（/sys/kernel/config/nvmet）——subsystem/namespace/port/hosts(dhchap_key)。
数据面单端口：NVMe/TCP TLS 1.3（默认 4421，`addr_tsas=tls1.3`，内核同时把 `treq` 置
required）——默认全链路加密，固件段（iPXE sanboot）与内核段（GRUB 之后）共用本口；
历史明文口（4420）已撤除，升级时由启动清理收敛。
TLS 端不需要 configfs 侧密钥配置，只需把 psk key 注入内核全局 `.nvme` keyring（见 NvmePskKeyring）。
Agent 是唯一调用方（持有 nodes CA 签发的 client cert，经共享卷 UDS 访问）。
盘文件的创建/管理归 Agent（本服务不建盘、不删盘），但**必须能看见同一份盘目录**——
configfs 的 device_path 由写入进程（本容器）所在挂载命名空间解析，故 compose 把宿主盘目录
挂到 `/srv/nvmet-disks`，device_path 按该前缀重拼（`config.DEFAULT_HOST_DISK_DIR`）。
契约：blueprint/nvmeof-credential-design.md 第 6 节。

configfs 语义要点（Linux v7.x nvmet）：
- host 条目是全局的：mkdir 顶层 hosts/<HOSTNQN>（默认 SHA256）；DH-HMAC-CHAP 认证 =
  hosts/<HOSTNQN>/dhchap_key 写 DHHC-1 明文（固件契约格式），写 key 即启用认证（无独立 control 属性）
- attr_allow_any_host=0（严格）：host 准入 = 在 subsystems/<NQN>/allowed_hosts/ 下 symlink
  挂载全局 hosts/<HOSTNQN>（target 必须是 nvmet_host_type，否则 EINVAL）；
  symlink 目标由内核按进程 cwd 解析，须用绝对路径
- 删除子系统前须先摘除 port 挂载（symlink），否则 EBUSY
- 全局 hosts/<hostnqn> 被多个子系统 allowed_hosts 引用时 rmdir 返回 EBUSY（实测）；
  回收仅在无引用者时执行（_gc_host）——多盘同 worker 逐个摘链接的中间态须保留条目
"""

import ctypes
import errno
import logging
import os
import re
import shutil
import stat
import struct
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from .config import (  # noqa: E402
    CONFIG, DEFAULT_BOOTSTRAP_TOKEN_FILE, DEFAULT_HOST_DISK_DIR,
    DEFAULT_HOST_SOCKET, DEFAULT_PKI_DIR,
)
from .pki_client import build_client, ensure_pki
from . import security

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# 组件 PKI 引导/轮换：证书未就绪抛错阻断启动（与 storager-agent 同语义）
ensure_pki()

log = logging.getLogger("nvmet-host")

CONFIGFS_NVMET = os.getenv("NVMET_CONFIGFS", "/sys/kernel/config/nvmet")
# 数据面端口（唯一）：NVMe/TCP TLS 1.3。默认全链路加密——固件段与内核段共用本口，
# 不再有独立的明文口。空字符串 = 关闭数据面（联调用；控制面会因端口未就绪拒绝引导）。
TLS_PORT_ID = os.getenv("NVMET_TLS_PORT_ID", "2")
TLS_TRSVCID = os.getenv("NVMET_TLS_TRSVCID", "4421")
# 历史明文口（4420）：不再创建、不再挂载；启动时清理残留（拆除挂载并删口），
# 使升级后的宿主收敛到单口。仅作迁移用，无运行时作用。
LEGACY_PLAIN_PORT_ID = os.getenv("NVMET_PLAIN_PORT_ID", "1")
LEGACY_PLAIN_TRSVCID = os.getenv("NVMET_PLAIN_TRSVCID", "4420")
KEYS_FILE = os.getenv("NVMET_KEYS_FILE", "/proc/keys")
# 容器内磁盘目录：configfs 的 device_path 由写入进程（本容器）所在挂载命名空间解析，
# 必须指向本容器可见路径；Agent 传入的 backing 是其容器内路径（如 /home/iscsi_img/xxx.img），
# 本服务按 basename 重拼到 DEFAULT_HOST_DISK_DIR（compose 挂载目标，部署清单职责）。
HOST_DISK_DIR = DEFAULT_HOST_DISK_DIR


class InvalidInput(ValueError):
    """标识/路径参数未通过白名单校验（路由层映射 400；与「已存在/未找到」区分）。"""


_SAFE_NQN_RE = re.compile(r"^nqn\.[a-z0-9._:-]+$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._:-]*$")


def _safe_nqn(value: str, field: str = "nqn") -> str:
    """NQN 白名单：不允许路径分隔符、`..`、空字节；configfs 路径拼接与 keyring identity 共用。"""
    v = (value or "").strip().lower()
    if (not v or len(v) > 300 or "/" in v or "\\" in v or ".." in v
            or "\x00" in v or not _SAFE_NQN_RE.fullmatch(v)):
        raise InvalidInput(f"invalid {field}")
    return v


def _safe_name(value: str, field: str) -> str:
    v = (value or "").strip()
    if (not v or len(v) > 255 or v in (".", "..") or ".." in v
            or "/" in v or "\\" in v or "\x00" in v
            or not _SAFE_NAME_RE.fullmatch(v)):
        raise InvalidInput(f"invalid {field}")
    return v


def _host_disk_path(backing: str) -> str:
    """Agent 容器内 backing 路径 → 本容器盘目录路径（按 basename 重拼 + realpath 限定在根内）。"""
    raw = (backing or "").strip()
    if not raw or "\x00" in raw or ".." in raw.split("/") or not os.path.isabs(raw):
        raise InvalidInput("invalid backing")
    name = _safe_name(os.path.basename(raw), "backing")
    root = os.path.realpath(HOST_DISK_DIR)
    path = os.path.realpath(os.path.join(root, name))
    if not path.startswith(root + os.sep):
        raise InvalidInput("backing escapes disk directory")
    return path


# ===================== TLS-PSK 目标端供给（内核全局 .nvme keyring） =====================


class NvmePskKeyring:
    """把 TLS-PSK 注入内核全局 `.nvme` keyring（目标端供给）。

    机制（v7.0 内核 + ktls-utils 1.3.0 实测，见 blueprint/nvme-tcp-tls-in-kernel.md §5）：
    - nvmet 建 port 时即取该 keyring（`port->keyring`），所以目标端**无需任何 configfs 配置**，
      只要把 psk key 放进去；tlshd 握手时用 ClientHello 里的 identity 在 keyring 里搜同名 key。
    - 一条 psk key：**description == PSK identity**，**payload == PSK 字节**（两端共用同一条）。
    - identity = `NVMe0R01 <hostnqn> <subsysnqn>`：ver 0 不带 digest，内核按**前缀**匹配，
      省掉 digest 计算（发起端用同一条，见 initramfs-nbft）。
    - `.nvme` 是**独立 keyring、不挂任何 session**（按名字搜不到），只能读 /proc/keys 取 serial；
      `add_key`/`keyctl` 无 libkeyutils 也可，直接内核 syscall。

    容器前置（compose 的 `security_opt`，缺一即静默失败，故列明）：
    - `seccomp=unconfined`：Docker 默认 seccomp 拒 `add_key`/`keyctl`（EPERM）。
    - `systempaths=unconfined`：`/proc/keys` 被默认 MaskedPaths 屏蔽成 /dev/null（0 字节省略报错）。
    """

    _SYS_ADD_KEY = 248  # x86_64
    _SYS_KEYCTL = 250
    _KEYCTL_UNLINK = 9
    _KEYCTL_READ = 11
    _KEY_TYPE = "psk"

    def __init__(self, keys_file: str = KEYS_FILE, keyring_name: str = ".nvme"):
        self.keys_file = keys_file
        self.keyring_name = keyring_name
        self._libc = ctypes.CDLL(None, use_errno=True)

    @staticmethod
    def identity(hostnqn: str, subsysnqn: str) -> str:
        return f"NVMe0R01 {hostnqn} {subsysnqn}"

    def _entries(self) -> list[tuple[int, str, str]]:
        """解析 /proc/keys → [(serial, type, description)]。

        行格式：`<serial> <flags> <usage> perm <perms> <uid> <gid> <type> <desc>[: <n>]`；
        description 可含空格（NQN），故前 8 列用正则切，尾部 `: <n>`（keyring 为 key 数、
        其他类型为 datalen）单独剥离。

        空 keyring 的尾部是 `: empty` 而非数字（如 `.nvme: empty`）——必须一并剥离，
        否则 host 重启后 `.nvme` 被 nvme-keyring 重建为空时 keyring_serial() 会找不到它，
        `PUT /subsystems/{nqn}/tls` 恒定 500，reconcile 再也无法把 PSK 注回去。"""
        out: list[tuple[int, str, str]] = []
        with open(self.keys_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(
                    r"^([0-9a-f]+)\s+\S+\s+\d+\s+perm\s+\S+\s+\d+\s+\d+\s+(\S+)\s+(.*)$",
                    line.rstrip("\n"),
                )
                if m:
                    out.append((int(m.group(1), 16), m.group(2),
                                re.sub(r": (?:\d+|empty)$", "", m.group(3))))
        return out

    def keyring_serial(self) -> int:
        for serial, ktype, desc in self._entries():
            if ktype == "keyring" and desc == self.keyring_name:
                return serial
        raise RuntimeError(
            f"kernel keyring '{self.keyring_name}' not found in {self.keys_file}"
            "（宿主需 nvme-keyring 模块——随 nvmet-tcp 自动加载；"
            "容器需 security_opt systempaths=unconfined 才能读 /proc/keys）")

    def _linked_serials(self, keyring: int) -> list[int]:
        """KEYCTL_READ 一个 keyring → 其中挂载的 key serial（每项 4 字节小端）。

        判定「key 是否真在 .nvme 里」只能靠它：/proc/keys 列出的是**进程可见的所有
        key**（含其它 keyring 里的同名条目），按它找 key 会命中不在 .nvme 的条目，
        UNLINK 得 ENOENT（实测）；而 TLS 查找只认 .nvme，故淘汰也必须只认 .nvme。"""
        buf = ctypes.create_string_buffer(4096)
        ctypes.set_errno(0)
        n = self._libc.syscall(self._SYS_KEYCTL, self._KEYCTL_READ, keyring, buf, len(buf))
        if n < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"keyctl(READ keyring) failed: {os.strerror(err)}")
        return [struct.unpack("<i", buf.raw[i:i + 4])[0] for i in range(0, n, 4)]

    def _key_serials(self, identity: str) -> list[int]:
        """.nvme 里 description == identity 的全部 key serial（历史重复可能不止一条）。"""
        desc = {s: d for s, ktype, d in self._entries() if ktype == self._KEY_TYPE}
        return [s for s in self._linked_serials(self.keyring_serial())
                if desc.get(s) == identity]

    def set_key(self, hostnqn: str, subsysnqn: str, psk: bytes) -> int:
        """写入/更新 psk key（幂等），返回 key serial。

        psk key 的 possessor 权限不含 Write，`add_key` 无法就地更新同名 key（实测每次
        都新建一条），故须先摘除 .nvme 里同 identity 的旧 key 再新增——一条 identity
        只留一条，否则内核查找结果不确定。"""
        if not psk:
            raise ValueError("empty PSK")
        identity = self.identity(hostnqn, subsysnqn)
        kr = self.keyring_serial()
        for old in self._key_serials(identity):
            self._unlink(old, kr)
        ctypes.set_errno(0)
        serial = self._libc.syscall(self._SYS_ADD_KEY, self._KEY_TYPE.encode(),
                                    identity.encode(), psk, len(psk), kr)
        if serial < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"add_key({self._KEY_TYPE}) failed: {os.strerror(err)}")
        return int(serial)

    def delete_key(self, hostnqn: str, subsysnqn: str) -> bool:
        """删除 .nvme 里该 identity 的 psk key；无则返回 False。"""
        identity = self.identity(hostnqn, subsysnqn)
        kr = self.keyring_serial()
        serials = self._key_serials(identity)
        for serial in serials:
            self._unlink(serial, kr)
        return bool(serials)

    def _unlink(self, serial: int, keyring: int) -> None:
        ctypes.set_errno(0)
        if self._libc.syscall(self._SYS_KEYCTL, self._KEYCTL_UNLINK, serial, keyring) < 0:
            err = ctypes.get_errno()
            if err in (errno.ENOENT, errno.ENOKEY):
                return  # 并发下已被摘除：语义即「确保不在」，视为成功
            raise OSError(err, f"keyctl(UNLINK) failed: {os.strerror(err)}")


# ============================ configfs 操作封装 ============================


class NvmetManager:
    """configfs nvmet 操作：全部路径基于 self.root（默认 /sys/kernel/config/nvmet），
    测试可注入临时目录模拟 configfs 布局。"""

    def __init__(self, root: str = CONFIGFS_NVMET, tls_port_id: str = TLS_PORT_ID,
                 tls_trsvcid: str = TLS_TRSVCID,
                 legacy_plain_port_id: str = LEGACY_PLAIN_PORT_ID,
                 keyring: "NvmePskKeyring | None" = None):
        self.root = root
        self.tls_port_id = tls_port_id
        self.tls_trsvcid = tls_trsvcid
        self.legacy_plain_port_id = legacy_plain_port_id
        self.keyring = keyring if keyring is not None else NvmePskKeyring()
        # 数据面端口是否就绪：由 ensure_tls_port() 置位（失败则不置位，
        # /capabilities 如实报 false，控制面据此拒绝引导）
        self.tls_ready = False

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_port_id)

    def ready(self) -> bool:
        return os.path.isdir(os.path.join(self.root, "subsystems"))

    def exists(self, nqn: str) -> bool:
        return os.path.isdir(self._sub_path(nqn))

    def create_subsystem(self, nqn: str, backing: str) -> None:
        """创建子系统 + namespace/1（backing 盘文件）+ 严格模式（allow_any_host=0）。"""
        nqn = _safe_nqn(nqn)
        device_path = _host_disk_path(backing)
        sub = self._sub_path(nqn)
        if os.path.isdir(sub):
            raise ValueError(f"subsystem exists: {nqn}")
        os.makedirs(sub, exist_ok=True)
        try:
            self._write(sub, "attr_allow_any_host", "0")
            ns = os.path.join(sub, "namespaces", "1")
            os.makedirs(ns, exist_ok=True)
            self._write(ns, "device_path", device_path)
            self._write(ns, "enable", "1")
        except Exception:
            shutil.rmtree(sub, ignore_errors=True)
            raise

    def delete_subsystem(self, nqn: str) -> None:
        """删除子系统：先摘除各端口挂载，再逐个禁用/删除 namespace，最后移除 hosts 与本体。"""
        nqn = _safe_nqn(nqn)
        sub = self._sub_path(nqn)
        if not os.path.isdir(sub):
            raise ValueError(f"subsystem not found: {nqn}")
        self.detach_port(nqn)
        ns_base = os.path.join(sub, "namespaces")
        if os.path.isdir(ns_base):
            for name in sorted(os.listdir(ns_base)):
                ns = os.path.join(ns_base, name)
                enable = os.path.join(ns, "enable")
                if os.path.exists(enable):
                    self._write(ns, "enable", "0")
                shutil.rmtree(ns, ignore_errors=True)
        # 摘除 allowed_hosts/ 下的 host 准入挂载（default group 本身是内核目录，只 unlink 不 rmdir）；
        # 收集 host 名用于本体删除后回收全局条目（无引用才删，见 _gc_host）
        allowed = os.path.join(sub, "allowed_hosts")
        host_names: list[str] = []
        if os.path.isdir(allowed):
            for name in sorted(os.listdir(allowed)):
                link = os.path.join(allowed, name)
                if os.path.islink(link):
                    os.unlink(link)
                host_names.append(name)
        shutil.rmtree(sub, ignore_errors=True)
        for name in host_names:
            self._gc_host(name)

    def list_subsystems(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        base = os.path.join(self.root, "subsystems")
        if not os.path.isdir(base):
            return result
        for nqn in sorted(os.listdir(base)):
            sub = os.path.join(base, nqn)
            if not os.path.isdir(sub):
                continue
            namespaces = []
            ns_base = os.path.join(sub, "namespaces")
            if os.path.isdir(ns_base):
                for nsid in sorted(os.listdir(ns_base)):
                    namespaces.append({
                        "nsid": int(nsid),
                        "device_path": self._read(os.path.join(ns_base, nsid, "device_path")),
                    })
            hosts = []
            allowed_base = os.path.join(sub, "allowed_hosts")
            if os.path.isdir(allowed_base):
                hosts = sorted(os.listdir(allowed_base))
            result.append({"nqn": nqn, "namespaces": namespaces, "hosts": hosts})
        return result

    def set_host(self, nqn: str, hostnqn: str, secret: str) -> None:
        """登记/更新 host 认证：全局 hosts/<hostnqn> 写 DHHC-1 密钥（dhchap_key），
        再 symlink 挂到子系统 allowed_hosts/ 完成准入。
        前置：内核在 allow_any_host=1 时拒绝 link explicit host（-EINVAL），
        因此准入前先把该 subsystem 切为严格模式（host 白名单语义）。"""
        sub = self._sub_path(nqn)
        if not os.path.isdir(sub):
            raise ValueError(f"subsystem not found: {nqn}")
        hostnqn = _safe_nqn(hostnqn, "hostnqn")
        host_dir = os.path.join(self.root, "hosts", hostnqn)
        os.makedirs(host_dir, exist_ok=True)
        self._write(host_dir, "dhchap_key", secret, newline=False)
        self._write(sub, "attr_allow_any_host", "0")
        link = os.path.join(sub, "allowed_hosts", hostnqn)
        if not os.path.lexists(link):
            os.symlink(host_dir, link)

    def delete_host(self, nqn: str, hostnqn: str) -> None:
        """移除 host 认证：先摘子系统 allowed_hosts 链接（configfs symlink
        用 unlink，同 diag teardown 的 rm -f 语义），再回收全局 hosts/<hostnqn>
        ——仅当没有任何子系统仍链接它（见 _gc_host）。"""
        nqn = _safe_nqn(nqn)
        hostnqn = _safe_nqn(hostnqn, "hostnqn")
        sub = self._sub_path(nqn)
        link = os.path.join(sub, "allowed_hosts", hostnqn)
        if os.path.lexists(link):
            os.unlink(link)
        self._gc_host(hostnqn)

    def _gc_host(self, hostnqn: str) -> None:
        """全局 host 目录回收：无任何子系统引用时才删。

        多盘同 worker 共用一个 hostnqn（一个子系统一块盘）：逐个摘链接期间全局
        条目必须留着——实测内核 configfs 对仍被 allowed_hosts symlink 引用的目录
        rmdir 返回 EBUSY；若此处强行删/报错，吊销会卡在第一个子系统（链接已摘、
        目录删不掉），其余盘的旧密钥原样留存。"""
        host_dir = os.path.join(self.root, "hosts", hostnqn)
        if not os.path.isdir(host_dir) or self._host_referenced(hostnqn):
            return
        try:
            os.rmdir(host_dir)
        except OSError as exc:
            # 竞态兜底：扫描与删除之间若出现新链接（EBUSY）；未引用的残留条目本身无效果
            log.warning("host gc skipped (%s): %s", hostnqn, exc)

    def _host_referenced(self, hostnqn: str) -> bool:
        base = os.path.join(self.root, "subsystems")
        if not os.path.isdir(base):
            return False
        for nqn in os.listdir(base):
            if os.path.lexists(os.path.join(base, nqn, "allowed_hosts", hostnqn)):
                return True
        return False

    def ensure_port(self, trtype: str = "tcp", adrfam: str = "ipv4", traddr: str = "0.0.0.0",
                    trsvcid: str | None = None, tsas: str | None = None,
                    port_id: str | None = None) -> dict[str, Any]:
        """幂等创建/更新 NVMe/TCP 端口。

        `tsas="tls1.3"` 即声明 TLS 端口（内核同时把 `treq` 置 required）；`tsas=None`
        表示不写 `addr_tsas`。trsvcid/port_id 缺省取数据面（TLS）端口值。

        写入顺序敏感：nvmet 在第一个子系统挂载（allow_link）时才启用端口并监听，
        addr_* 属性只在端口未启用时可写（启用后 store 返回 -EACCES），故端口已配置
        （addr_trtype 非空）时直接跳过写入。注意：addr_tsas 属性仅接受 tls1.3，不写即无 TLS；
        写 tsas 的前置是 `port->keyring != NULL`（nvme-keyring 模块已加载），否则内核返回 EINVAL。"""
        port_id = port_id or self.tls_port_id
        trsvcid = trsvcid or self.tls_trsvcid
        port = os.path.join(self.root, "ports", port_id)
        os.makedirs(port, exist_ok=True)
        if self._read(os.path.join(port, "addr_trtype")):
            return {"port": port_id, "trtype": trtype, "traddr": traddr,
                    "trsvcid": self._read(os.path.join(port, "addr_trsvcid")) or trsvcid,
                    "tsas": self._read(os.path.join(port, "addr_tsas")) or "none",
                    "already_configured": True}
        self._write(port, "addr_trsvcid", trsvcid)
        self._write(port, "addr_traddr", traddr)
        self._write(port, "addr_adrfam", adrfam)
        self._write(port, "addr_trtype", trtype)
        if tsas:
            self._write(port, "addr_tsas", tsas)
        return {"port": port_id, "trtype": trtype, "traddr": traddr,
                "trsvcid": trsvcid, "tsas": tsas or "none"}

    def ensure_tls_port(self) -> dict[str, Any]:
        """创建/确认数据面端口（NVMe/TCP TLS 1.3）并置位 tls_ready。

        未配置（空 port id）时为 no-op 且不置位——控制面据此拒绝引导。"""
        if not self.tls_enabled:
            return {"enabled": False, "reason": "NVMET_TLS_PORT_ID 为空"}
        info = self.ensure_port(tsas="tls1.3", port_id=self.tls_port_id,
                                trsvcid=self.tls_trsvcid)
        self.tls_ready = True
        return {"enabled": True, **info}

    def remove_legacy_plain_port(self) -> str | None:
        """拆除历史明文口（4420）：先摘子系统挂载再删口，幂等。

        返回被删除的端口 id，未存在则返回 None。升级后的宿主由启动流程调用一次，
        使残留的明文数据面收敛掉；纯明文访问随之消失。"""
        pid = self.legacy_plain_port_id
        if not pid:
            return None
        port = os.path.join(self.root, "ports", pid)
        if not os.path.isdir(port):
            return None
        subs = os.path.join(port, "subsystems")
        if os.path.isdir(subs):
            for nqn in os.listdir(subs):
                link = os.path.join(subs, nqn)
                if os.path.islink(link):
                    os.unlink(link)
        os.rmdir(port)
        return pid

    def attach_port(self, nqn: str, port_id: str | None = None) -> None:
        """把子系统挂到数据面端口（symlink），幂等；创建子系统后必须挂载才对外可见。

        configfs 的 symlink 目标由内核 kern_path 按进程 cwd 解析，必须用绝对路径
        （相对路径会相对 uvicorn 的 cwd 解析而 ENOENT）。"""
        pid = port_id or self.tls_port_id
        nqn = _safe_nqn(nqn)
        link = os.path.join(self.root, "ports", pid, "subsystems", nqn)
        os.makedirs(os.path.dirname(link), exist_ok=True)
        if not os.path.islink(link):
            os.symlink(os.path.join(self.root, "subsystems", nqn), link)

    def detach_port(self, nqn: str, port_id: str | None = None) -> None:
        """从数据面端口摘除子系统挂载（幂等）。"""
        pid = port_id or self.tls_port_id
        if not pid:
            return
        nqn = _safe_nqn(nqn)
        link = os.path.join(self.root, "ports", pid, "subsystems", nqn)
        if os.path.islink(link):
            os.unlink(link)

    def set_tls_key(self, hostnqn: str, nqn: str, psk: bytes) -> int:
        """登记目标端 PSK（= 该 hostnqn 连该子系统的凭据），返回 key serial。"""
        if not self.tls_enabled:
            raise ValueError("TLS port disabled (NVMET_TLS_PORT_ID 为空)")
        hostnqn = _safe_nqn(hostnqn, "hostnqn")
        nqn = _safe_nqn(nqn)
        return self.keyring.set_key(hostnqn, nqn, psk)

    def delete_tls_key(self, hostnqn: str, nqn: str) -> bool:
        hostnqn = _safe_nqn(hostnqn, "hostnqn")
        nqn = _safe_nqn(nqn)
        return self.keyring.delete_key(hostnqn, nqn)

    def _sub_path(self, nqn: str) -> str:
        base = os.path.join(self.root, "subsystems")
        path = os.path.join(base, _safe_nqn(nqn))
        if not path.startswith(base + os.sep):
            raise InvalidInput("invalid nqn")
        return path

    @staticmethod
    def _write(path: str, attr: str, value: str, newline: bool = True) -> None:
        with open(os.path.join(path, attr), "w", encoding="utf-8") as f:
            f.write(value + ("\n" if newline else ""))

    @staticmethod
    def _read(path: str) -> str | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return None


manager = NvmetManager()


# ============================ 请求模型 + 鉴权 ============================


class SubsystemCreate(BaseModel):
    nqn: str
    backing: str


class HostSet(BaseModel):
    hostnqn: str
    secret: str


class TlsKeySet(BaseModel):
    """目标端 TLS-PSK 供给：psk 为 32 字节的十六进制（64 字符）。"""
    hostnqn: str
    psk: str


def verify_client_cert(request: Request) -> None:
    """mTLS 客户端证书鉴权（nodes CA 信任模型 + 应用层 CRL/CN）。

    TLS 层强制客户端证书链；应用层校验 issuer、CN（agent-<id>，健康检查自检
    另允许 nvmet-<id>）与 nodes CRL（可热更新，无需重启）。
    """
    allowed = {f"agent-{CONFIG.metadata.name}", f"nvmet-{CONFIG.metadata.name}"}
    security.verify_peer(
        request.scope,
        allowed_cns=allowed,
        issuer_cn=security.ca_subject_cn(str(Path(DEFAULT_PKI_DIR) / "ca-nodes.crt")),
        crl_kind="nodes",
        store=security.CRL_STORE,
    )


app = FastAPI(title="nvmet-host")


@app.get("/healthz")
def healthz():
    """唯一不鉴权端点（探活）：configfs 就绪则 ok；附 CRL 快照供灰度排查。"""
    return {"status": "ok", "configfs": manager.ready(), "crl": security.CRL_STORE.describe()}


@app.get("/capabilities", dependencies=[Depends(verify_client_cert)])
def capabilities():
    return {
        "backend": "nvmet",
        "cd": False,
        "persistent": "host-native configfs (nvmet-host service)",
        "configfs": manager.root,
        "port": ({
            "port_id": manager.tls_port_id, "trtype": "tcp",
            "trsvcid": manager.tls_trsvcid, "tsas": "tls1.3",
            "ready": manager.tls_ready,
        } if manager.tls_enabled else None),
    }


@app.post("/port", dependencies=[Depends(verify_client_cert)])
def ensure_port():
    """幂等确认数据面端口（NVMe/TCP TLS 1.3）。"""
    return manager.ensure_tls_port()


@app.get("/subsystems", dependencies=[Depends(verify_client_cert)])
def list_subsystems():
    return {"subsystems": manager.list_subsystems()}


@app.post("/subsystems", status_code=201, dependencies=[Depends(verify_client_cert)])
def create_subsystem(req: SubsystemCreate):
    try:
        nqn = _safe_nqn(req.nqn)
        manager.create_subsystem(nqn, req.backing)
    except InvalidInput as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        # 数据面端口未就绪时暂不挂载（挂载会指向未配置的端口）；PSK 供给
        # （PUT /subsystems/{nqn}/tls）时会补挂，且此时端口必然已就绪。
        if manager.tls_ready:
            manager.attach_port(nqn)
    except Exception as exc:
        raise HTTPException(500, f"attach port failed: {exc}") from exc
    return {"nqn": nqn, "backing": req.backing,
            "tls_port": manager.tls_port_id if manager.tls_ready else None}


@app.delete("/subsystems/{nqn}", dependencies=[Depends(verify_client_cert)])
def delete_subsystem(nqn: str):
    try:
        manager.delete_subsystem(nqn)
    except InvalidInput as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"deleted": _safe_nqn(nqn)}


@app.put("/subsystems/{nqn}/hosts", dependencies=[Depends(verify_client_cert)])
def set_host(nqn: str, req: HostSet):
    """登记/更新 host 认证（DHHC-1 密钥）。Agent 按绑定关系同步调用。"""
    try:
        nqn = _safe_nqn(nqn)
        hostnqn = _safe_nqn(req.hostnqn, "hostnqn")
        manager.set_host(nqn, hostnqn, req.secret)
    except InvalidInput as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"nqn": nqn, "hostnqn": hostnqn, "set": True}


@app.delete("/subsystems/{nqn}/hosts/{hostnqn}", dependencies=[Depends(verify_client_cert)])
def delete_host(nqn: str, hostnqn: str):
    try:
        nqn = _safe_nqn(nqn)
        hostnqn = _safe_nqn(hostnqn, "hostnqn")
        manager.delete_host(nqn, hostnqn)
    except InvalidInput as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"nqn": nqn, "hostnqn": hostnqn, "deleted": True}


@app.put("/subsystems/{nqn}/tls", dependencies=[Depends(verify_client_cert)])
def set_tls_key(nqn: str, req: TlsKeySet):
    """登记目标端 TLS-PSK（psk key 注入内核全局 `.nvme`），并把子系统挂到 TLS 端口。
    Agent 按 worker 凭据同步调用；identity = `NVMe0R01 <hostnqn> <nqn>`。"""
    try:
        nqn = _safe_nqn(nqn)
        hostnqn = _safe_nqn(req.hostnqn, "hostnqn")
    except InvalidInput as exc:
        raise HTTPException(400, str(exc)) from exc
    if not manager.exists(nqn):
        raise HTTPException(404, f"subsystem not found: {nqn}")
    try:
        psk = bytes.fromhex(req.psk)
    except ValueError as exc:
        raise HTTPException(400, f"psk must be hex: {exc}") from exc
    if len(psk) != 32:
        raise HTTPException(400, f"psk must be 32 bytes (64 hex chars): got {len(psk)}")
    try:
        serial = manager.set_tls_key(hostnqn, nqn, psk)
        manager.attach_port(nqn)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"keyring inject failed: {exc}") from exc
    return {"nqn": nqn, "hostnqn": hostnqn, "key_serial": serial,
            "tls_port": manager.tls_port_id}


@app.delete("/subsystems/{nqn}/tls/{hostnqn}", dependencies=[Depends(verify_client_cert)])
def delete_tls_key(nqn: str, hostnqn: str):
    try:
        nqn = _safe_nqn(nqn)
        hostnqn = _safe_nqn(hostnqn, "hostnqn")
        removed = manager.delete_tls_key(hostnqn, nqn)
    except InvalidInput as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"keyring delete failed: {exc}") from exc
    return {"nqn": nqn, "hostnqn": hostnqn, "deleted": removed}


if __name__ == "__main__":
    import uvicorn

    # 管理面只走共享卷 UDS：无 TCP 监听，局域网不可达；目录 0700 限制同机非 root 访问
    sock = DEFAULT_HOST_SOCKET
    sock_dir = os.path.dirname(sock)
    os.makedirs(sock_dir, mode=0o700, exist_ok=True)
    os.chmod(sock_dir, 0o700)
    if os.path.exists(sock):
        if not stat.S_ISSOCK(os.stat(sock).st_mode):
            raise RuntimeError(f"refusing to replace non-socket path: {sock}")
        os.unlink(sock)
    # 启动即配置数据面端口（幂等）：裸端口不允许挂子系统，须先写全 addr_* 属性。
    # 宿主前置未满足（nvmet 模块/configfs 挂载）时给出可操作报错——宿主侧由
    # nvmeof/host-prepare.sh 就位（见 README）
    try:
        info = manager.ensure_tls_port()
        if info.get("enabled"):
            log.info("数据面端口就绪: %s (tsas=tls1.3)", info)
    except OSError as exc:
        # 创建失败不阻断管理面：tls_ready 保持 false，/capabilities 如实上报，
        # 控制面据此拒绝引导（默认全链路加密，不做明文降级）
        manager.tls_ready = False
        log.error("数据面端口创建失败，控制面将拒绝引导本节点的盘: %s"
                  "（宿主需 nvmet 模块 + nvme-keyring，后者随 nvmet-tcp 自动加载）", exc)
    # 迁移：拆除历史明文口（4420），使升级后的宿主收敛到单口
    try:
        removed = manager.remove_legacy_plain_port()
        if removed:
            log.warning("已拆除历史明文数据面端口 ports/%s (trsvcid=%s)",
                        removed, LEGACY_PLAIN_TRSVCID)
    except OSError as exc:
        log.error("历史明文口拆除失败（手工处理: 摘 ports/%s/subsystems/* 后 rmdir）: %s",
                  LEGACY_PLAIN_PORT_ID, exc)
    # mTLS：serving cert + nodes CA 校验客户端证书链（CERT_REQUIRED；只信同伴组件）；
    # issuer/CN 与 nodes CRL 在应用层校验（可热更新，见 security.py）
    security.install_peer_cert_hook()
    if os.environ.get(security.INSECURE_ENV) == "1":
        log.warning("component security disabled: %s=1", security.INSECURE_ENV)
    pki_dir = DEFAULT_PKI_DIR
    context = security.build_server_context(Path(pki_dir), "ca-nodes.crt")
    security.start_refresh_thread(build_client(), Path(pki_dir), security.CRL_STORE)
    uvicorn.run(
        app, uds=sock, http="h11", log_level="info", proxy_headers=False,
        ssl_context_factory=lambda _config, _default: context,
    )
