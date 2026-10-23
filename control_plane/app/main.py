"""控制面组装入口：装配共享存储（stores）、信任根域（trust）与资源域路由。

业务逻辑按域拆分到 app/utils.py（纯工具）、app/stores.py（全局单例 + 审计）、
app/auth.py（认证器链/RBAC/会话）、app/trust.py（注册窗口/强制/验签）与 app/routers/ 各域。
本模块仅负责：创建 FastAPI 应用、按序挂载路由、启动时执行一次旧数据迁移与凭据自举。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute

from . import (audit, audit_middleware, cert_bootstrap, config, dnsmasq_conf,
               maintenance, pki, users)
from .principal import enforce_principal
from .routers import (agents, boot, boot_assets, certs, devices, enroll,
                      operations, session, settings, tokens, workers)

log = logging.getLogger("control-plane")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 凭据对账线程：控制面→Agent 推送的幂等重放（间隔 ≤0 禁用，测试环境用）
    workers.start_credential_reconciler()
    # CRL 定期重签：防止长跑超过 nextUpdate 后 nginx 拒绝全部客户端证书
    crl_stop = maintenance.start_crl_refresher()
    yield
    if crl_stop is not None:
        crl_stop.set()


app = FastAPI(
    title="IPXE-All-Ready Control Plane",
    lifespan=lifespan,
    # 全路由主体类比对先于各路由的认证依赖执行（越类 403）
    dependencies=[Depends(enforce_principal)],
    # 生产不暴露 /openapi.json、/docs、/redoc：接口清单只对已认证管理面开放
    docs_url="/docs" if config.settings.openapi_enabled else None,
    redoc_url="/redoc" if config.settings.openapi_enabled else None,
    openapi_url="/openapi.json" if config.settings.openapi_enabled else None,
)

# 认证审计：每请求 allow/deny 事件（写失败 → 503）；引导链端点不在管理面审计范围
audit_middleware.install(app)

# boot 域最先挂载：/devices/report、/devices/challenge 必须先于 devices 域 /devices/{mac} 匹配
app.include_router(boot.router)
app.include_router(boot_assets.router)
app.include_router(devices.router)
app.include_router(settings.router)
app.include_router(agents.router)
app.include_router(workers.router)
app.include_router(operations.router)
app.include_router(tokens.router)
app.include_router(certs.router)
app.include_router(session.router)
app.include_router(enroll.router)

# 默认拒绝的导入期校验：任何 APIRoute 没有 principal 声明就启动失败，
# 防止后续新增路由绕过主体类矩阵。
for _route in app.routes:
    if isinstance(_route, APIRoute) and not hasattr(_route, "principal"):
        raise RuntimeError(f"route {_route.path} missing principal declaration")

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

# 启动时幂等生成组件 PKI：三分根（users/nodes/control）+ 控制面自身 client cert +
# 管理凭据（admin 证书/admin.conf/会话密钥）——CLI 自动发现 admin.conf 走 443 mTLS。
# 遗留单 CA 在原位提升为 nodes，存量组件证书继续有效；管理证书与 control 证书
# 按 issuer 检测后重签。nginx 只读挂载 nginx/ca-bundle.pem（users + nodes）。
try:
    ca_set = pki.ensure_ca_set(config.settings.pki_dir)
    users.ensure_users_file(config.settings.users_file, config.settings.admin_cn)
    audit.check_users_file_change()  # 建立变更基线（之后改动即告警）
    pki.ensure_control_plane_client_cert(
        config.settings.pki_dir, *ca_set["control"], config.settings.control_plane_component,
    )
    pki.ensure_admin_credentials(
        config.settings.pki_dir, *ca_set["users"], config.settings.admin_conf,
    )
    pki.write_nginx_trust_bundle(config.settings.pki_dir)
    # CRL 缺失会让 nginx 启动失败（ssl_crl 引用该文件）：启动时无条件重签
    # clients.crl（users + nodes），即使尚无吊销条目。
    pki.write_nginx_crl(config.settings.pki_dir)
    pki.ensure_session_key(config.settings.session_key_file)
except Exception:
    log.exception("pki: failed to ensure component PKI")
