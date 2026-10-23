"""匿名引导端点限速（/devices/report 与 /devices/challenge）。

滑动窗口 + 进程内计数：控制面是单实例，重启即清空。用途是抑制扫描、放大与无意义
的公钥喷洒（两个端点都不鉴权），不作为身份或安全边界。计数按来源 IP（nginx 对这两
个 location 覆盖写入 X-Real-IP，因此不能被客户端伪造）。

阈值可用 KURRENT_CP_RATE_LIMIT_REPORT / _CHALLENGE 覆盖（每分钟次数，<=0 关闭）。
"""

import os
import threading
import time
from collections import deque

WINDOW_SECONDS = 60.0
DEFAULT_LIMITS = {"report": 120, "challenge": 120, "rotate": 10, "boot_vars": 240}
# 上限防护：来源 IP 很多时清掉窗口内已无请求的桶（正常规模远低于此）
_MAX_BUCKETS = 4096

_LOCK = threading.Lock()
_BUCKETS: dict[tuple[str, str], deque[float]] = {}


def limit_for(scope: str) -> int:
    raw = os.getenv(f"KURRENT_CP_RATE_LIMIT_{scope.upper()}")
    default = DEFAULT_LIMITS.get(scope, 0)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def check(scope: str, key: str, *, now: float | None = None) -> float | None:
    """记一次访问：放行返回 None；超限返回建议等待秒数（>=1）。key 为空不限。"""
    key = (key or "").strip()
    limit = limit_for(scope)
    if not key or limit <= 0:
        return None
    now = time.monotonic() if now is None else now
    with _LOCK:
        bucket = _BUCKETS.setdefault((scope, key), deque())
        while bucket and now - bucket[0] >= WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= limit:
            return max(1.0, WINDOW_SECONDS - (now - bucket[0]))
        bucket.append(now)
        if len(_BUCKETS) > _MAX_BUCKETS:
            _sweep(now)
    return None


def _sweep(now: float) -> None:
    for key in [k for k, v in _BUCKETS.items() if not v or now - v[-1] >= WINDOW_SECONDS]:
        _BUCKETS.pop(key, None)


def reset() -> None:
    """清空计数（测试隔离用）。"""
    with _LOCK:
        _BUCKETS.clear()
