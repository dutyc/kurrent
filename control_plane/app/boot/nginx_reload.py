"""nginx reload（共享卷哨兵）：CRL 文件更新后让 443 入口立即重新加载。

nginx 只在配置加载时读取 ssl_crl；不 reload 的话已吊销证书仍能握手。控制面不再经
docker.sock 操作 nginx 容器：把递增计数器写入 kurrent-run 卷，nginx 容器内的 watcher
执行 `nginx -s reload` 后把计数器写回 ack 文件；控制面轮询 ack 确认已生效（超时即
视为 reload 失败，调用方返回 500）。
"""

import logging
import time
from pathlib import Path

log = logging.getLogger("control-plane")

# 与控制面/nginx 共享的 kurrent-run 卷；nginx watcher 轮询两个文件
RELOAD_TRIGGER = Path("/run/kurrent/nginx.reload")
RELOAD_ACK = Path("/run/kurrent/nginx.ack")
ACK_TIMEOUT_SECONDS = 5.0


def _read_counter(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def reload_nginx(timeout: float = ACK_TIMEOUT_SECONDS) -> dict[str, str]:
    """请求 nginx reload 并等待 ack；不可写/超时抛 RuntimeError。"""
    counter = _read_counter(RELOAD_TRIGGER) + 1
    try:
        RELOAD_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
        RELOAD_TRIGGER.write_text(f"{counter}\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot write nginx reload trigger: {exc}") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _read_counter(RELOAD_ACK) >= counter:
            log.info("nginx: reloaded (counter=%d)", counter)
            return {"status": "ok", "counter": str(counter)}
        time.sleep(0.05)
    raise RuntimeError(f"nginx did not acknowledge reload within {timeout:g}s")
