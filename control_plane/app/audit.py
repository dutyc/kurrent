"""结构化认证审计与告警（state/audit.jsonl）。

与 operations.jsonl（业务操作）分离：这里只记认证/授权/凭据生命周期事件，字段固定，
便于机器检测。审计写入失败时调用方必须拒绝请求（fail-closed）——日志缺失意味着
无法追责的放行，宁可中断也不静默通过。

八条必须告警（blueprint 10.2）：
1. boot_vars 出现 injected && !verified
2. 管理路由出现 component/control 类
3. 未知 issuer / CRL 命中（nginx 校验失败证据）
4. 同一来源短时间大量 401/403
5. bootstrap token 的 CN 与登记不符
6. 管理员证书签发 / CRL 更新 / CA 轮换
7. users.yml 变更
8. 审计写入失败
"""

import datetime as _dt
import json
import logging
import os
import queue
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

from .config import settings

log = logging.getLogger("control-plane")

# 告警外投（可选）：KURRENT_CP_ALARM_WEBHOOK 非空时把 event=alarm 的审计事件 POST 过去。
# 有界队列 + 后台线程：投递失败/对端缓慢不影响请求路径；队列满时丢弃并记日志。
ALARM_WEBHOOK_QUEUE_MAX = 200
ALARM_WEBHOOK_TIMEOUT_SECONDS = 5.0
ALARM_WEBHOOK_RETRY_DELAYS = (1.0, 4.0)

# 认证失败风暴：同一来源 60 秒内 10 次 401/403 → 告警；同来源 60 秒内只告警一次
_BURST_WINDOW_SECONDS = 60
_BURST_THRESHOLD = 10
_BURST_COOLDOWN_SECONDS = 60
_BURST_LOCK = threading.Lock()
_BURST: dict[str, deque] = {}
_BURST_ALARMED: dict[str, float] = {}

_USERS_WATCH_LOCK = threading.Lock()
_USERS_SIGNATURE: tuple[int, int] | None = None

_WRITE_LOCK = threading.Lock()
_ALARM_QUEUE_LOCK = threading.Lock()
_ALARM_QUEUE: queue.Queue | None = None


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _write(doc: dict) -> None:
    path = Path(settings.audit_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n"
    with _WRITE_LOCK:
        new = not path.exists()
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        if new:
            os.chmod(path, 0o600)


def emit(event: str, *, principal_class: str = "", principal: str = "",
         issuer: str = "", serial: str = "", fingerprint_sha256: str = "",
         route: str = "", decision: str = "allow", reason: str = "",
         request_id: str = "", client_ip: str = "", **extra) -> dict:
    """追加一条审计事件并返回该事件；写失败抛 OSError（调用方转 503，fail-closed）。"""
    doc = {
        "ts": _now().isoformat(),
        "event": event,
        "principal_class": principal_class,
        "principal": principal,
        "issuer": issuer,
        "serial": serial,
        "fingerprint_sha256": fingerprint_sha256,
        "route": route,
        "decision": decision,
        "reason": reason,
        "request_id": request_id,
        "client_ip": client_ip,
    }
    for key, value in extra.items():
        if value not in (None, ""):
            doc[key] = value
    _write(doc)
    return doc


def _post_alarm(url: str, body: dict) -> None:
    """POST 一条告警 JSON；异常交给调用方重试。"""
    data = json.dumps(body, ensure_ascii=False, sort_keys=True).encode()
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=ALARM_WEBHOOK_TIMEOUT_SECONDS) as resp:
        resp.read()


def _deliver_alarm(payload: dict) -> None:
    """同步投递（含重试）；由后台线程调用，测试可直接调用。"""
    url = str(payload.get("webhook_url") or "")
    body = {k: v for k, v in payload.items() if k != "webhook_url"}
    attempts = len(ALARM_WEBHOOK_RETRY_DELAYS) + 1
    for index in range(attempts):
        try:
            _post_alarm(url, body)
            return
        except Exception as exc:
            if index == attempts - 1:
                log.error("alarm webhook delivery failed after %d attempts: %s", attempts, exc)
                return
            time.sleep(ALARM_WEBHOOK_RETRY_DELAYS[index])


def _alarm_delivery_loop(box: queue.Queue) -> None:
    while True:
        payload = box.get()
        try:
            _deliver_alarm(payload)
        except Exception:
            log.exception("alarm webhook worker failed")
        finally:
            box.task_done()


def _alarm_queue() -> queue.Queue:
    global _ALARM_QUEUE
    with _ALARM_QUEUE_LOCK:
        if _ALARM_QUEUE is None:
            _ALARM_QUEUE = queue.Queue(maxsize=ALARM_WEBHOOK_QUEUE_MAX)
            threading.Thread(target=_alarm_delivery_loop, args=(_ALARM_QUEUE,),
                             name="alarm-webhook", daemon=True).start()
        return _ALARM_QUEUE


def _enqueue_alarm(doc: dict) -> bool:
    """按 KURRENT_CP_ALARM_WEBHOOK 入队外投；未配置返回 False，队列满丢弃并记日志。"""
    url = os.getenv("KURRENT_CP_ALARM_WEBHOOK", "").strip()
    if not url:
        return False
    payload = dict(doc)
    payload["webhook_url"] = url
    try:
        _alarm_queue().put_nowait(payload)
    except queue.Full:
        log.error("alarm webhook queue full (%d); dropped alarm %s",
                  ALARM_WEBHOOK_QUEUE_MAX, payload.get("alarm", ""))
        return False
    return True


def alarm(name: str, *, severity: str = "high", detail: str = "",
          route: str = "", principal: str = "", **extra) -> None:
    """告警：ERROR 日志 + 审计事件（event=alarm）+ 可选 webhook 外投。

    审计失败只记日志，不递归；外投只入队（后台线程重试），永不阻塞请求。
    """
    log.error("security alarm: %s%s", name, f" ({detail})" if detail else "")
    try:
        doc = emit("alarm", decision="deny", reason=name, alarm=name,
                   severity=severity, detail=detail, route=route, principal=principal, **extra)
    except Exception:
        log.exception("alarm %s could not be written to audit", name)
        _alarm_write_failure(name)
        return
    _enqueue_alarm(doc)


def _alarm_write_failure(name: str) -> None:
    """审计不可写时把第 8 条告警降级到进程日志（已在上方记录）与操作审计。"""
    try:
        from .stores import record
        record("audit.write_failure", "failed", scope=name)
    except Exception:
        pass


def issuer_name(raw: str) -> str:
    """把 nginx 的 issuer DN 归一为 ca-users / ca-nodes / ca-control（不认识的保留原值）。"""
    raw = (raw or "").strip()
    for kind in ("users", "nodes", "control"):
        if f"CN=kurrent-ca-{kind}" in raw:
            return f"ca-{kind}"
    return raw


def check_users_file_change() -> None:
    """users.yml 变更检测：启动后首次调用建基线，之后 mtime/size 变化即告警。"""
    global _USERS_SIGNATURE
    try:
        st = Path(settings.users_file).stat()
    except OSError:
        return
    signature = (st.st_mtime_ns, st.st_size)
    with _USERS_WATCH_LOCK:
        if _USERS_SIGNATURE is None:
            _USERS_SIGNATURE = signature
            return
        if signature == _USERS_SIGNATURE:
            return
        _USERS_SIGNATURE = signature
    alarm("users_file_changed", detail=str(settings.users_file),
          route="users.yml")


def note_auth_failure(client_ip: str, *, route: str, reason: str,
                      verify: str = "", issuer: str = "",
                      serial: str = "", fingerprint: str = "") -> None:
    """401/403 证据：未知 issuer / CRL 命中 + 同源失败风暴。"""
    if verify and not verify.upper().startswith("SUCCESS") and (issuer or serial):
        alarm("unknown_issuer_or_crl", detail=verify, route=route)
    if not client_ip:
        return
    now = time.monotonic()
    with _BURST_LOCK:
        bucket = _BURST.setdefault(client_ip, deque())
        bucket.append(now)
        while bucket and now - bucket[0] > _BURST_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) < _BURST_THRESHOLD:
            return
        if now - _BURST_ALARMED.get(client_ip, 0.0) < _BURST_COOLDOWN_SECONDS:
            return
        _BURST_ALARMED[client_ip] = now
    alarm("auth_failure_burst", detail=f"{len(bucket)} failures/{_BURST_WINDOW_SECONDS}s",
          route=route, client_ip=client_ip)


def boot_credential_observed(*, injected: bool, verified: bool,
                             worker_id: str, mac: str) -> None:
    """第 1 条：凭据已注入但验签未通过（目标架构下应恒为零）。"""
    if injected and not verified:
        alarm("boot_credential_unverified", detail="injected=true verified=false",
              principal=worker_id or mac, mac=mac)
