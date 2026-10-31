"""控制面后台维护：CRL 定期重签。

nginx 只在启动/reload 时读取 ssl_crl；CRL 的 nextUpdate 是 7 天，而吊销是低频事件。
若控制面连续运行超过 7 天且没有吊销操作，nginx 手里会是一张过期 CRL，OpenSSL 的
CRL 校验会拒绝全部客户端证书——管理面失联。这里按 spec.pki.crlRefreshHours
（默认 12h，schema 约束小于 7 天）重签并 reload，保证长跑安全边界。

间隔可用 KURRENT_CP_CRL_REFRESH_INTERVAL（秒）覆盖；<=0 关闭（测试环境）。
"""

import logging
import os
import threading

from . import audit, pki
from .config import settings
from .nginx_reload import reload_nginx

log = logging.getLogger("control-plane")


def refresh_interval_seconds() -> int:
    raw = os.getenv("KURRENT_CP_CRL_REFRESH_INTERVAL")
    if raw is None:
        return int(settings.crl_refresh_hours) * 3600
    try:
        return int(raw)
    except ValueError:
        return int(settings.crl_refresh_hours) * 3600


def refresh_crl_once(*, reload: bool = True) -> dict:
    """重签 CRL 并（可选）reload nginx；reload 失败告警但 CRL 文件已落盘。"""
    path = pki.write_nginx_crl(settings.pki_dir)
    if reload:
        try:
            reload_nginx()
        except RuntimeError as exc:
            audit.alarm("crl_refresh_failed", detail=str(exc), route="crl")
            raise
    log.info("crl: refreshed (%s)", path)
    return {"path": str(path)}


def start_crl_refresher() -> threading.Event | None:
    """启动守护线程；返回停止事件（关闭时置位），间隔 <=0 不启动。"""
    interval = refresh_interval_seconds()
    if interval <= 0:
        return None
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(interval):
            try:
                refresh_crl_once()
            except Exception:
                log.exception("crl: periodic refresh failed")

    threading.Thread(target=_loop, name="crl-refresh", daemon=True).start()
    log.info("crl: periodic refresh every %ds", interval)
    return stop
