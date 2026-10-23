"""服务端会话登记（state/sessions.yml）。

Cookie 只携带随机 `jti`（经 HMAC 签名），身份与组一律以本登记为准：删条目即
下一请求失效，无需等待 Cookie 过期。登记键是 `sha256(jti)`，文件泄露也无法
还原可用的 Cookie。

结构：
    epoch: 3
    sessions:
      <sha256(jti)>: {user, groups, created_at, last_seen, expires_at, cert_fp}

`epoch` 用于“全部踢下线”：递增后所有既有 Cookie 立即失效（即使登记被重建）。
"""

import datetime as _dt
import hashlib
import os
import secrets
import threading
from pathlib import Path

import yaml

from .config import settings

# last_seen 节流：每个会话最多每分钟回写一次（滑动续期独立处理）
_TOUCH_INTERVAL_SECONDS = 60
_TOUCH_LOCK = threading.Lock()
_last_touch: dict[str, float] = {}

_LOCK = threading.RLock()


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _path() -> Path:
    return Path(settings.sessions_file)


def _load() -> dict:
    try:
        doc = yaml.safe_load(_path().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        doc = None
    if not isinstance(doc, dict):
        return {"epoch": 0, "sessions": {}}
    sessions = doc.get("sessions")
    return {
        "epoch": int(doc.get("epoch") or 0),
        "sessions": sessions if isinstance(sessions, dict) else {},
    }


def _save(doc: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _expired(entry: dict, now: _dt.datetime) -> bool:
    try:
        return _dt.datetime.fromisoformat(str(entry.get("expires_at"))) <= now
    except (TypeError, ValueError):
        return True


def _prune(doc: dict) -> bool:
    now = _now()
    stale = [k for k, v in doc["sessions"].items()
             if not isinstance(v, dict) or _expired(v, now)]
    for key in stale:
        doc["sessions"].pop(key, None)
    return bool(stale)


def epoch() -> int:
    return _load()["epoch"]


def create(user: str, groups: list[str], *, ttl_seconds: int, cert_fp: str = "",
           scopes: list[str] | None = None) -> tuple[str, int, int]:
    """登记新会话，返回 (jti, exp, epoch)；jti 只下发给客户端。"""
    now = _now()
    exp = int(now.timestamp()) + ttl_seconds
    jti = secrets.token_hex(16)
    key = hashlib.sha256(jti.encode()).hexdigest()
    with _LOCK:
        doc = _load()
        _prune(doc)
        doc["sessions"][key] = {
            "user": user,
            "groups": list(groups),
            "created_at": now.isoformat(),
            "last_seen": now.isoformat(),
            "expires_at": _dt.datetime.fromtimestamp(exp, _dt.timezone.utc).isoformat(),
            "cert_fp": cert_fp,
            "scopes": list(scopes or []),
        }
        _save(doc)
        return jti, exp, doc["epoch"]


def resolve(jti: str) -> tuple[str, dict] | None:
    """按 jti 查登记；不存在/过期返回 None（过期条目顺手清理）。"""
    if not jti:
        return None
    key = hashlib.sha256(jti.encode()).hexdigest()
    with _LOCK:
        doc = _load()
        entry = doc["sessions"].get(key)
        if not isinstance(entry, dict):
            return None
        if _expired(entry, _now()):
            doc["sessions"].pop(key, None)
            _save(doc)
            return None
        return key, entry


def touch(key: str) -> None:
    """更新 last_seen（节流：同一会话每分钟最多一次）。"""
    now = _now()
    with _TOUCH_LOCK:
        last = _last_touch.get(key, 0.0)
        if now.timestamp() - last < _TOUCH_INTERVAL_SECONDS:
            return
        _last_touch[key] = now.timestamp()
    with _LOCK:
        doc = _load()
        entry = doc["sessions"].get(key)
        if not isinstance(entry, dict):
            return
        entry["last_seen"] = now.isoformat()
        _save(doc)


def renew(key: str, ttl_seconds: int) -> int:
    """滑动续期：延长 expires_at，返回新的 exp 时间戳。"""
    exp = int(_now().timestamp()) + ttl_seconds
    with _LOCK:
        doc = _load()
        entry = doc["sessions"].get(key)
        if not isinstance(entry, dict):
            return exp
        entry["last_seen"] = _now().isoformat()
        entry["expires_at"] = _dt.datetime.fromtimestamp(exp, _dt.timezone.utc).isoformat()
        _save(doc)
    return exp


def revoke_by_jti(jti: str) -> bool:
    """登出：按客户端持有的 jti 删除登记。"""
    if not jti:
        return False
    key = hashlib.sha256(jti.encode()).hexdigest()
    with _LOCK:
        doc = _load()
        existed = doc["sessions"].pop(key, None) is not None
        if existed:
            _save(doc)
        return existed


def revoke(session_id: str) -> bool:
    """管理员逐个吊销：session_id = 列表返回的 sha256(jti)。"""
    session_id = (session_id or "").strip()
    if not session_id:
        return False
    with _LOCK:
        doc = _load()
        existed = doc["sessions"].pop(session_id, None) is not None
        if existed:
            _save(doc)
        return existed


def revoke_user(user: str) -> int:
    """删除某用户的全部会话（users.yml 删条目时联动）。"""
    with _LOCK:
        doc = _load()
        drop = [k for k, v in doc["sessions"].items()
                if isinstance(v, dict) and v.get("user") == user]
        for key in drop:
            doc["sessions"].pop(key, None)
        if drop:
            _save(doc)
        return len(drop)


def revoke_by_cert_fp(fingerprint: str) -> int:
    """删除某张客户端证书建立的会话（逐序列号吊销时只踢受影响的会话）。"""
    fingerprint = (fingerprint or "").strip().lower()
    if not fingerprint:
        return 0
    with _LOCK:
        doc = _load()
        drop = [k for k, v in doc["sessions"].items()
                if isinstance(v, dict)
                and str(v.get("cert_fp") or "").strip().lower() == fingerprint]
        for key in drop:
            doc["sessions"].pop(key, None)
        if drop:
            _save(doc)
        return len(drop)


def revoke_all() -> int:
    """全部踢下线：清空登记并递增 epoch（旧 Cookie 即使重放也失效）。"""
    with _LOCK:
        doc = _load()
        count = len(doc["sessions"])
        doc["sessions"] = {}
        doc["epoch"] = int(doc.get("epoch") or 0) + 1
        _save(doc)
        return count


def list_sessions() -> list[dict]:
    """列出未过期会话（session_id 是 jti 的 sha256，不是可用凭据）。"""
    with _LOCK:
        doc = _load()
        if _prune(doc):
            _save(doc)
        items = []
        for key, entry in doc["sessions"].items():
            if not isinstance(entry, dict):
                continue
            items.append({
                "session_id": key,
                "user": str(entry.get("user") or ""),
                "groups": list(entry.get("groups") or []),
                "created_at": str(entry.get("created_at") or ""),
                "last_seen": str(entry.get("last_seen") or ""),
                "expires_at": str(entry.get("expires_at") or ""),
                "cert_fp": str(entry.get("cert_fp") or ""),
            })
        items.sort(key=lambda e: e["created_at"])
        return items
