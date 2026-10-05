"""控制面组装入口：装配共享存储（stores）、信任根域（trust）与资源域路由。

业务逻辑按域拆分到 app/utils.py（纯工具）、app/stores.py（全局单例 + 审计）、
app/auth.py（认证器链/RBAC/会话）、app/trust.py（注册窗口/强制/验签）与 app/routers/ 各域。
本模块仅负责：创建 FastAPI 应用、按序挂载路由、启动时执行一次旧数据迁移与凭据自举。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import cert_bootstrap, config, dnsmasq_conf, pki
from .routers import agents, boot, devices, enroll, operations, session, settings, tokens, workers

log = logging.getLogger("control-plane")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 凭据对账线程：控制面→Agent 推送的幂等重放（间隔 ≤0 禁用，测试环境用）
    workers.start_credential_reconciler()
    yield


app = FastAPI(title="IPXE-All-Ready Control Plane", lifespan=lifespan)

# boot 域最先挂载：/devices/report、/devices/challenge 必须先于 devices 域 /devices/{mac} 匹配
app.include_router(boot.router)
app.include_router(devices.router)
app.include_router(settings.router)
app.include_router(agents.router)
app.include_router(workers.router)
app.include_router(operations.router)
app.include_router(tokens.router)
app.include_router(session.router)
app.include_router(enroll.router)

# 启动时执行一次旧数据迁移（幂等；失败仅记日志，不阻断启动）
devices.migrate_legacy_devices()

# 启动时按 spec.networking 生成 dnsmasq.conf（yml 是权威，conf 为派生物；失败仅记日志，不阻断启动）
try:
    dnsmasq_conf.ensure_dnsmasq_conf(config.DEFAULT_DNSMASQ_CONF)
except Exception:
    log.exception("dnsmasq: failed to render dnsmasq.conf")

# 启动时幂等生成 TOFU 引导链服务器证书（失败仅记日志，不阻断启动；轮换 = 删 state/certs/ 重启）
try:
    cert_bootstrap.ensure_server_cert(config.settings.cert_dir, config.settings.cert_san, config.settings.cert_days)
except Exception:
    log.exception("cert: failed to ensure server certificate")

# 启动时幂等生成组件 PKI：内部 CA + 控制面自身 client cert + 管理凭据（admin 证书/
# admin.conf/会话密钥）——CLI 自动发现 admin.conf 走 443 mTLS
try:
    ca_cert, ca_key = pki.ensure_ca(config.settings.pki_dir)
    pki.ensure_control_plane_client_cert(
        config.settings.pki_dir, ca_cert, ca_key, config.settings.control_plane_component,
    )
    pki.ensure_admin_credentials(
        config.settings.pki_dir, ca_cert, ca_key, config.settings.admin_conf,
    )
    pki.ensure_session_key(config.settings.session_key_file)
except Exception:
    log.exception("pki: failed to ensure component PKI")
