"""Agent 装配入口：PKI 引导、后端实例化、启动扫描与凭据对账。

业务按层拆分：runtime/（声明派生常量）、disks/（盘文件与母盘扫描）、
backends/（stgt/lio 驱动与后端工厂）、oplog/（操作日志）、routes/（HTTP 接口）。
本模块只负责：导入时完成 PKI 引导与挂载属主对齐、装配 FastAPI 应用、启动后台线程。
"""

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from . import disks, routes
from .backends import backend
from .config import DEFAULT_BOOTSTRAP_TOKEN_FILE, DEFAULT_LOG_FILE, DEFAULT_PKI_DIR
from .oplog import oplog
from .ownership import align_tree
from .pki_client import ensure_pki
from .runtime import BACKEND, NQN_BASE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agent")

# 组件 PKI 引导/轮换：证书未就绪则抛错阻断启动（uvicorn 起服务前）
ensure_pki()
for _mount in (DEFAULT_PKI_DIR, Path(DEFAULT_BOOTSTRAP_TOKEN_FILE).parent, Path(DEFAULT_LOG_FILE).parent):
    align_tree(_mount)


_reconcile_stop = threading.Event()


def _startup() -> None:
    """启动扫描：宿主服务/CRL 可能晚于本容器就绪，失败按指数退避重试而不是一次性放弃。

    真实故障：nvmet-host 初始 CRL 拉取遇 502 时 fail-closed，Agent 的首次 scan 立刻 503；
    旧实现只跑一次，此后 4421/subsystems 永远不建，直到人工重启。"""
    delay = 2.0
    while not _reconcile_stop.is_set():
        try:
            backend.wait_ready()
            result = backend.startup()
            if result is not None:                       # stgt/nvmet 返回 scan 结果，lio 返回 None
                oplog.record("auto_scan", {}, "ok", "local",
                             created=len(result.get("created", [])),
                             skipped=len(result.get("skipped", [])))
            return
        except Exception as exc:
            log.warning(f"startup failed: {exc}; retrying in {delay:.0f}s")
            if _reconcile_stop.wait(delay):
                return
            delay = min(delay * 2, 30.0)


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
    disks.masters.start()
    threading.Thread(target=_reconcile_loop, daemon=True).start()
    log.info(f"agent started: backend={BACKEND}, base NQN={NQN_BASE}")
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(routes.router)
