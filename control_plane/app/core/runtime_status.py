"""控制面启动期派生状态：按声明生成 dnsmasq.conf / 服务器证书 / PKI 的结果。

派生失败不阻断进程启动（管理面仍可用于排障），但 /healthz 返回 503——让
`kurrent init` 的健康检查失败并回滚声明，而不是把「控制面活着、声明未生效」
当成成功。错误详情只出现在已认证的 GET /config，匿名 healthz 不泄露路径。
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_errors: list[str] = []
_warnings: list[str] = []


def reset() -> None:
    with _lock:
        _errors.clear()
        _warnings.clear()


def _describe(component: str, exc: BaseException) -> str:
    return f"{component}: {exc}"


def record_error(component: str, exc: BaseException) -> None:
    with _lock:
        _errors.append(_describe(component, exc))


def record_warning(component: str, exc: BaseException) -> None:
    with _lock:
        _warnings.append(_describe(component, exc))


def errors() -> list[str]:
    with _lock:
        return list(_errors)


def warnings() -> list[str]:
    with _lock:
        return list(_warnings)
