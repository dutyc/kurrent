"""声明配置状态端点：运行态加载的生效配置 + 派生件一致性 + 启动期错误。

回答「现在运行的是哪份声明、派生物是否与它一致」：
- spec/configHash：进程启动时加载的生效配置（默认值已注入）与规范化哈希，
  与 `kurrent config validate/show` 的输出可比对；
- derived.dnsmasqConf.matchesDeclaration：磁盘上 dnsmasq.conf 是否等于当前声明
  渲染结果（容器是否已重启读取该文件由 kurrent init 负责，不在本端点推断）；
- derived.serverCert：证书指纹/SAN/到期与声明漂移（rotateOnChange=false 时
  漂移只报告不轮换）；
- startup.errors/warnings：启动期派生失败（errors 同时使 /healthz 返回 503）。
"""

from fastapi import Depends

from .. import cert_bootstrap, config, dnsmasq_conf, runtime_status
from ..auth import authenticate, authorize_request
from ..principal import PrincipalRouter

router = PrincipalRouter(
    dependencies=[Depends(authenticate), Depends(authorize_request)],
    principal={"human", "automation"},
)


@router.get("/config")
def get_config():
    """当前生效声明与派生件状态（只读；不含任何私钥/密钥）。"""
    cfg = config.CONFIG
    settings = config.settings
    net = cfg.spec.networking
    rendered = dnsmasq_conf.render_dnsmasq_conf(
        net.interface, net.subnet, net.dhcp_range, net.gateway, net.dns)
    try:
        conf_matches = settings.dnsmasq_conf.read_text(encoding="utf-8") == rendered
    except OSError:
        conf_matches = False

    cert = cert_bootstrap.inspect_server_cert(
        settings.cert_dir, settings.cert_san, settings.cert_days)
    cert["rotateOnChange"] = bool(settings.cert_rotate_on_change)

    return {
        "apiVersion": cfg.api_version,
        "kind": "ControlPlaneConfiguration",
        "metadata": {"name": cfg.metadata.name},
        "spec": config.effective_config(cfg)["spec"],
        "configHash": config.config_hash(cfg),
        "derived": {
            "dnsmasqConf": {
                "path": str(settings.dnsmasq_conf),
                "matchesDeclaration": conf_matches,
            },
            "serverCert": cert,
        },
        "startup": {
            "errors": runtime_status.errors(),
            "warnings": runtime_status.warnings(),
        },
    }
