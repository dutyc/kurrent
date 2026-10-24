"""Agent 服务入口：mTLS 上下文 + peer cert hook + CRL 刷新 + uvicorn。

证书引导/轮换仍在 app.main 导入时执行（ensure_pki）；本模块只负责把校验层
（CN/issuer/CRL）装到运行中的服务上。显式固定 http="h11"：peer cert hook 依赖
h11 的 RequestResponseCycle。
"""

import logging
import os
from pathlib import Path

import uvicorn

from . import security
from .config import DEFAULT_PKI_DIR
from .main import app
from .pki_client import build_client

log = logging.getLogger("agent")


def main() -> None:
    security.install_peer_cert_hook()
    if os.environ.get(security.INSECURE_ENV) == "1":
        log.warning("component security disabled: %s=1", security.INSECURE_ENV)
    pki_dir = Path(DEFAULT_PKI_DIR)
    context = security.build_server_context(pki_dir, "ca-control.crt")
    security.start_refresh_thread(build_client(), pki_dir, security.CRL_STORE)
    uvicorn.run(
        app, host="0.0.0.0", port=8080, http="h11",
        log_level="info", proxy_headers=False,
        ssl_context_factory=lambda _config, _default: context,
    )


if __name__ == "__main__":
    main()
