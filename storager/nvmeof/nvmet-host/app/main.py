"""nvmet 宿主管理服务（NVMe-oF 存储接入 C4：宿主原生 nvmet + Agent HTTP 调用）。

运行形态：容器 `kurrent-nvmet-host`（`storager/nvmeof/docker-compose.yml`）——host 网络模式，
监听 4841（`config.DEFAULT_HOST_PORT`），只对外提供 mTLS（内部 CA，与 agent 同构：派生
bootstrap token 引导 + 证书轮换 + 客户端证书鉴权；token TTL 内可复用、不消耗）；因 Ubuntu
docker-default AppArmor 会拒绝 configfs 写入，容器需豁免 AppArmor（见 nvmet-host README）。
直接操作内核 configfs（/sys/kernel/config/nvmet）——subsystem/namespace/port/hosts(dhchap_key)。
Agent 是唯一调用方（持有内部 CA 签发的 client cert，经 https://host.docker.internal:4841）。
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

import logging
import os
import shutil
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from .config import (  # noqa: E402
    CONFIG, DEFAULT_BOOTSTRAP_TOKEN_FILE, DEFAULT_HOST_ADDR, DEFAULT_HOST_DISK_DIR,
    DEFAULT_HOST_PORT, DEFAULT_PKI_DIR,
)
from .pki_client import ensure_pki

# 组件 PKI 引导/轮换：证书未就绪抛错阻断启动（与 storager-agent 同语义）
ensure_pki()

log = logging.getLogger("nvmet-host")

CONFIGFS_NVMET = os.getenv("NVMET_CONFIGFS", "/sys/kernel/config/nvmet")
PORT_ID = os.getenv("NVMET_PORT_ID", "1")
# 容器内磁盘目录：configfs 的 device_path 由写入进程（本容器）所在挂载命名空间解析，
# 必须指向本容器可见路径；Agent 传入的 backing 是其容器内路径（如 /home/iscsi_img/xxx.img），
# 本服务按 basename 重拼到 DEFAULT_HOST_DISK_DIR（compose 挂载目标，部署清单职责）。
HOST_DISK_DIR = DEFAULT_HOST_DISK_DIR


# ============================ configfs 操作封装 ============================


class NvmetManager:
    """configfs nvmet 操作：全部路径基于 self.root（默认 /sys/kernel/config/nvmet），
    测试可注入临时目录模拟 configfs 布局。"""

    def __init__(self, root: str = CONFIGFS_NVMET, port_id: str = PORT_ID):
        self.root = root
        self.port_id = port_id

    def ready(self) -> bool:
        return os.path.isdir(os.path.join(self.root, "subsystems"))

    def exists(self, nqn: str) -> bool:
        return os.path.isdir(self._sub_path(nqn))

    def create_subsystem(self, nqn: str, backing: str) -> None:
        """创建子系统 + namespace/1（backing 盘文件）+ 严格模式（allow_any_host=0）。"""
        sub = self._sub_path(nqn)
        if os.path.isdir(sub):
            raise ValueError(f"subsystem exists: {nqn}")
        os.makedirs(sub, exist_ok=True)
        try:
            self._write(sub, "attr_allow_any_host", "0")
            ns = os.path.join(sub, "namespaces", "1")
            os.makedirs(ns, exist_ok=True)
            device_path = (
                os.path.join(HOST_DISK_DIR, os.path.basename(backing))
                if HOST_DISK_DIR else backing
            )
            self._write(ns, "device_path", device_path)
            self._write(ns, "enable", "1")
        except Exception:
            shutil.rmtree(sub, ignore_errors=True)
            raise

    def delete_subsystem(self, nqn: str) -> None:
        """删除子系统：先摘除 port 挂载，再逐个禁用/删除 namespace，最后移除 hosts 与本体。"""
        sub = self._sub_path(nqn)
        if not os.path.isdir(sub):
            raise ValueError(f"subsystem not found: {nqn}")
        link = os.path.join(self.root, "ports", self.port_id, "subsystems", nqn)
        if os.path.islink(link):
            os.unlink(link)
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
                    trsvcid: str = "4420") -> dict[str, str]:
        """幂等创建/更新 NVMe/TCP 端口（默认 4420，无 TLS）。

        写入顺序敏感：nvmet 在第一个子系统挂载（allow_link）时才启用端口并监听，
        addr_* 属性只在端口未启用时可写（启用后 store 返回 -EACCES），故端口已配置
        （addr_trtype 非空）时直接跳过写入。注意：addr_tsas 属性仅接受 tls1.3，不写即无 TLS。"""
        port = os.path.join(self.root, "ports", self.port_id)
        os.makedirs(port, exist_ok=True)
        if self._read(os.path.join(port, "addr_trtype")):
            return {"port": self.port_id, "trtype": trtype, "traddr": traddr,
                    "trsvcid": trsvcid, "already_configured": True}
        self._write(port, "addr_trsvcid", trsvcid)
        self._write(port, "addr_traddr", traddr)
        self._write(port, "addr_adrfam", adrfam)
        self._write(port, "addr_trtype", trtype)
        return {"port": self.port_id, "trtype": trtype, "traddr": traddr,
                "trsvcid": trsvcid}

    def attach_port(self, nqn: str) -> None:
        """把子系统挂到端口（symlink），幂等；创建子系统后必须挂载才对外可见。

        configfs 的 symlink 目标由内核 kern_path 按进程 cwd 解析，必须用绝对路径
        （相对路径会相对 uvicorn 的 cwd 解析而 ENOENT）。"""
        link = os.path.join(self.root, "ports", self.port_id, "subsystems", nqn)
        os.makedirs(os.path.dirname(link), exist_ok=True)
        if not os.path.islink(link):
            os.symlink(os.path.join(self.root, "subsystems", nqn), link)

    def _sub_path(self, nqn: str) -> str:
        return os.path.join(self.root, "subsystems", nqn)

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


def verify_client_cert(request: Request) -> None:
    """mTLS 客户端证书鉴权（内部 CA 信任模型）。

    uvicorn 以 ssl_cert_reqs=CERT_REQUIRED + ssl_ca_certs=内部 CA 起服：TLS 层
    强制客户端证书且校验链到内部 CA（无证书/链不符握手即被拒）；身份边界 =
    内部 CA 签发范围（bootstrap token 一次性引导 + CSR CN 校验 + 组件登记，
    签发受控制面管控）。uvicorn 不透传客户端证书到 ASGI scope
    （Kludex/uvicorn#745），应用层无法做 CN 白名单。
    """
    if request.client is None:
        raise HTTPException(401, "unauthorized")


app = FastAPI(title="nvmet-host")


@app.get("/healthz")
def healthz():
    """唯一不鉴权端点（探活）：configfs 就绪则 ok。"""
    return {"status": "ok", "configfs": manager.ready()}


@app.get("/capabilities", dependencies=[Depends(verify_client_cert)])
def capabilities():
    return {
        "backend": "nvmet",
        "cd": False,
        "persistent": "host-native configfs (nvmet-host service)",
        "configfs": manager.root,
        "port": {"trtype": "tcp", "trsvcid": "4420", "tsas": "none"},
    }


@app.post("/port", dependencies=[Depends(verify_client_cert)])
def ensure_port(trsvcid: str = "4420"):
    return manager.ensure_port(trsvcid=trsvcid)


@app.get("/subsystems", dependencies=[Depends(verify_client_cert)])
def list_subsystems():
    return {"subsystems": manager.list_subsystems()}


@app.post("/subsystems", status_code=201, dependencies=[Depends(verify_client_cert)])
def create_subsystem(req: SubsystemCreate):
    try:
        manager.create_subsystem(req.nqn, req.backing)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        manager.attach_port(req.nqn)
    except Exception as exc:
        raise HTTPException(500, f"attach port failed: {exc}") from exc
    return {"nqn": req.nqn, "backing": req.backing, "port": PORT_ID}


@app.delete("/subsystems/{nqn}", dependencies=[Depends(verify_client_cert)])
def delete_subsystem(nqn: str):
    try:
        manager.delete_subsystem(nqn)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"deleted": nqn}


@app.put("/subsystems/{nqn}/hosts", dependencies=[Depends(verify_client_cert)])
def set_host(nqn: str, req: HostSet):
    """登记/更新 host 认证（DHHC-1 密钥）。Agent 按绑定关系同步调用。"""
    try:
        manager.set_host(nqn, req.hostnqn, req.secret)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"nqn": nqn, "hostnqn": req.hostnqn, "set": True}


@app.delete("/subsystems/{nqn}/hosts/{hostnqn}", dependencies=[Depends(verify_client_cert)])
def delete_host(nqn: str, hostnqn: str):
    manager.delete_host(nqn, hostnqn)
    return {"nqn": nqn, "hostnqn": hostnqn, "deleted": True}


if __name__ == "__main__":
    import uvicorn

    # 监听地址/端口为部署细节（host 网络，同机固定拓扑；agent 经 DEFAULT_NVMET_HOST_URL 访问）
    host = DEFAULT_HOST_ADDR
    port = DEFAULT_HOST_PORT
    # 启动即配置 NVMe/TCP 端口（幂等）：裸端口不允许挂子系统，须先写全 addr_* 属性。
    # 宿主前置未满足（nvmet 模块/configfs 挂载）时给出可操作报错——宿主侧由
    # nvmeof/host-prepare.sh 就位（见 README）
    try:
        manager.ensure_port()
    except OSError as exc:
        log.error("宿主前置未满足（nvmet 模块 + configfs 挂载）: %s——宿主侧修复: sudo bash storager/nvmeof/host-prepare.sh", exc)
        raise
    # mTLS：serving cert + 内部 CA 校验客户端证书链（CERT_REQUIRED），
    # 与 storager-agent 的 uvicorn 参数一致；host 网络直连（无 docker-proxy），协议版本不限
    pki_dir = DEFAULT_PKI_DIR
    uvicorn.run(
        app, host=host, port=port, log_level="info",
        ssl_keyfile=os.path.join(pki_dir, "serving.key"),
        ssl_certfile=os.path.join(pki_dir, "serving.crt"),
        ssl_ca_certs=os.path.join(pki_dir, "ca.crt"),
        ssl_cert_reqs=2,
    )
