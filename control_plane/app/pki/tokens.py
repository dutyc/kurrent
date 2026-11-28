"""pki 子模块：tokens。"""

import logging

from ..core import config
from ..core.utils import AGENT_ID_RE
from pathlib import Path
import datetime as _dt
import hashlib
import hmac
import os
import secrets
import time
import yaml
from .common import (TOKENS_FILENAME, _TOKENS_LOCK, _TOKEN_TOUCH, _TOKEN_TOUCH_INTERVAL_SECONDS,
                     _TOKEN_TOUCH_LOCK)
from .common import _align_owner

log = logging.getLogger("control-plane")

def _token_file(pki_dir: Path) -> Path:
    return pki_dir / TOKENS_FILENAME


def _load_tokens(pki_dir: Path) -> dict:
    try:
        data = yaml.safe_load(_token_file(pki_dir).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_tokens(pki_dir: Path, tokens: dict) -> None:
    """落盘 token 台账（写入前惰性清理过期条目）。"""
    now = _dt.datetime.now(_dt.timezone.utc)
    tokens = {
        tid: entry for tid, entry in tokens.items()
        if _entry_active(entry.get("expires_at"), now)
    }
    pki_dir.mkdir(parents=True, exist_ok=True)
    tmp = _token_file(pki_dir).with_suffix(".yml.tmp")
    tmp.write_text(yaml.safe_dump(tokens, allow_unicode=True, sort_keys=True), encoding="utf-8")
    _align_owner(tmp)
    os.replace(tmp, _token_file(pki_dir))
    os.chmod(_token_file(pki_dir), 0o600)


def _entry_active(expires_at: str, now: _dt.datetime) -> bool:
    try:
        return _dt.datetime.fromisoformat(expires_at) > now
    except (KeyError, TypeError, ValueError):
        return False


def normalize_token_scopes(scopes: list[str] | None) -> list[str]:
    """校验/去重自动化 token scope；空 = ["admin"]（兼容 P2 之前签发的 token）。

    scope 语义：admin=全部；readonly=只读；enroll=仅可签发 bootstrap token。
    权限只升不降：含 admin 时不再需要其余 scope。
    """
    if not scopes:
        return ["admin"]
    out: list[str] = []
    for scope in scopes:
        scope = str(scope).strip().lower()
        if not scope:
            continue
        if scope not in config.TOKEN_SCOPES:
            raise ValueError(f"unknown token scope: {scope}")
        if scope not in out:
            out.append(scope)
    if not out:
        raise ValueError("token scope cannot be empty")
    return out


def groups_for_scopes(scopes: list[str]) -> list[str]:
    """scope → 授权组（admin 覆盖其余）。"""
    if "admin" in scopes:
        return [config.ADMIN_GROUP]
    if "readonly" in scopes:
        return [config.VIEWER_GROUP]
    return ["enroll"]


def generate_agent_id() -> str:
    """控制面生成节点身份：12 位小写 hex（与盘 os_tag、短容器 ID 同构）。

    节点身份属于控制面分配的资源；CLI/WebUI 只是 API 客户端，不自行生成。
    """
    return secrets.token_hex(6)


def issue_bootstrap_token(pki_dir: Path, agent_id: str = "", component: str = "agent",
                          usage: list[str] | None = None,
                          groups: list[str] | None = None, name: str = "",
                          ttl_hours: int | None = None,
                          ttl_minutes: int | None = None,
                          max_uses: int = 1,
                          scopes: list[str] | None = None) -> str:
    """签发 token，返回明文（登记只存 secret 的 sha256；key = token_id）。

    无幂等沿用（每次新签；明文不可恢复）；
    bootstrap token（usage=enroll）必须绑定 agent_id + component，默认 60 分钟 TTL、
    一次性（max_uses=1，旧条目无该字段 = 不限）；管理 token（usage=["admin"] + groups）
    供认证器链 Bearer 与 WebUI 登录使用，不绑组件（component=""），scope 决定授权
    （admin/readonly/enroll），TTL 默认 24h、上限 30 天。
    """
    usage = usage or ["enroll"]
    if "admin" not in usage:
        if not agent_id:
            raise ValueError("bootstrap token must bind agent_id")
        if not AGENT_ID_RE.fullmatch(agent_id):
            raise ValueError("agent_id must be 12 lowercase hex characters")
        if ttl_hours is not None:
            raise ValueError("ttl_hours only applies to admin tokens")
        if scopes:
            raise ValueError("scopes only apply to admin tokens")
        minutes = (config.settings.bootstrap_token_ttl_minutes_default
                   if ttl_minutes is None else int(ttl_minutes))
        if not 1 <= minutes <= config.settings.bootstrap_token_ttl_minutes_max:
            raise ValueError(
                f"ttl_minutes must be between 1 and {config.settings.bootstrap_token_ttl_minutes_max}")
        if not 1 <= int(max_uses) <= config.settings.bootstrap_token_max_uses_upper:
            raise ValueError(
                f"max_uses must be between 1 and {config.settings.bootstrap_token_max_uses_upper}")
        ttl = _dt.timedelta(minutes=minutes)
        token_scopes: list[str] = []
        token_groups = groups or []
        max_uses = int(max_uses)
    else:
        token_scopes = normalize_token_scopes(scopes)
        hours = config.settings.token_ttl_hours_default if ttl_hours is None else int(ttl_hours)
        if not 1 <= hours <= config.settings.token_ttl_hours_max:
            raise ValueError(
                f"ttl_hours must be between 1 and {config.settings.token_ttl_hours_max}")
        ttl = _dt.timedelta(hours=hours)
        token_groups = groups if groups is not None else groups_for_scopes(token_scopes)
        max_uses = 0  # 管理 token 不按次数消耗

    token_id = secrets.token_hex(3)
    token_secret = secrets.token_hex(8)
    now = _dt.datetime.now(_dt.timezone.utc)
    entry = {
        "component": component,
        "agent_id": agent_id or "",
        # 明文仅在生成时可见：登记只存 secret 部分 hash（token_id 为台账 key，非机密）
        "token_secret_hash": hashlib.sha256(token_secret.encode()).hexdigest(),
        "created_at": now.isoformat(),
        "expires_at": (now + ttl).isoformat(),
        "last_used_at": "",
        "usage": usage,
        "groups": token_groups,
        "scopes": token_scopes,
    }
    if max_uses:
        entry["max_uses"] = max_uses
        entry["uses"] = 0
    if name:
        entry["name"] = name
    with _TOKENS_LOCK:
        tokens = _load_tokens(pki_dir)
        tokens[token_id] = entry
        _save_tokens(pki_dir, tokens)
    log.info("pki: token issued: usage=%s scopes=%s component=%s agent=%s ttl=%s max_uses=%s",
             entry["usage"], entry["scopes"], component or "-", agent_id or "-",
             ttl, max_uses or "-")
    return f"{token_id}.{token_secret}"


def revoke_agent_tokens(pki_dir: Path, agent_id: str, component: str = "nvmet-host") -> None:
    """删除某 agent 组件的全部登记条目（派生 token 重建前调用；agent 通用 token 不动）。"""
    with _TOKENS_LOCK:
        tokens = _load_tokens(pki_dir)
        drop = [tid for tid, e in tokens.items()
                if e.get("component") == component and e.get("agent_id") == agent_id]
        for tid in drop:
            tokens.pop(tid)
        if drop:
            _save_tokens(pki_dir, tokens)
    if drop:
        log.info("pki: revoked %d %s token(s) for agent %s", len(drop), component, agent_id)


def get_bootstrap_token(pki_dir: Path, token_id: str) -> dict | None:
    """只读返回 token 登记条目（剔除 secret hash）。"""
    entry = _load_tokens(pki_dir).get(token_id)
    if not entry:
        return None
    return {k: v for k, v in entry.items() if k != "token_secret_hash"}


def list_bootstrap_tokens(pki_dir: Path) -> list[dict]:
    """列出未过期 token 条目（台账可视化管理用；不返回 secret hash——明文本就不可恢复）。

    只读不落盘（不做过期清理，清理发生在下次保存）；按 expires_at 升序（先到期的靠前）。
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    items: list[dict] = []
    for token_id, entry in _load_tokens(pki_dir).items():
        if not isinstance(entry, dict) or not _entry_active(entry.get("expires_at"), now):
            continue
        items.append({
            "token_id": token_id,
            "usage": list(entry.get("usage") or []),
            "groups": list(entry.get("groups") or []),
            "scopes": list(entry.get("scopes") or []),
            "component": str(entry.get("component") or ""),
            "agent_id": str(entry.get("agent_id") or ""),
            "name": str(entry.get("name") or ""),
            "created_at": str(entry.get("created_at") or ""),  # 旧条目无此字段（签发时才注入）
            "expires_at": str(entry.get("expires_at") or ""),
            "last_used_at": str(entry.get("last_used_at") or ""),
            "uses": int(entry.get("uses") or 0),
            "max_uses": int(entry.get("max_uses") or 0),
        })
    items.sort(key=lambda e: e["expires_at"])
    return items


def revoke_bootstrap_token(pki_dir: Path, token_id: str) -> bool:
    """吊销（删除）一个 token 条目（无 tombstone），返回是否存在。

    删除即失效：「谁在何时吊销了哪个 token」由操作审计留痕；token 不存在返回 False。
    """
    with _TOKENS_LOCK:
        tokens = _load_tokens(pki_dir)
        if token_id not in tokens:
            return False
        tokens.pop(token_id)
        _save_tokens(pki_dir, tokens)
    log.info("pki: token revoked: %s", token_id)
    return True


def _split_token(token: str) -> tuple[str, str]:
    token = token.strip()
    token_id, _, secret = token.partition(".")
    if not token_id or not secret:
        raise ValueError("malformed bootstrap token")
    return token_id, secret


def _check_bootstrap_entry(entry: dict | None, token_id: str, secret: str,
                           agent_id: str, component: str) -> dict:
    if not entry:
        raise ValueError(f"no bootstrap token for token id {token_id}")
    if entry.get("component") != component:
        raise ValueError("bootstrap token component mismatch")
    if entry.get("agent_id") != agent_id:
        raise ValueError("bootstrap token agent mismatch")
    if not hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(),
                               entry.get("token_secret_hash", "")):
        raise ValueError("bootstrap token invalid")
    if not _entry_active(entry.get("expires_at"), _dt.datetime.now(_dt.timezone.utc)):
        raise ValueError("bootstrap token expired")
    return entry


def validate_bootstrap_token(pki_dir: Path, token: str, agent_id: str, component: str) -> None:
    """只校验 bootstrap token（不消耗使用额度）：格式、登记、组件/绑定、hash、TTL。

    失败抛 ValueError（调用方转 401）。enroll 实际走 reserve_bootstrap_token，
    先原子占额度、签发失败再释放。
    """
    token_id, secret = _split_token(token)
    _check_bootstrap_entry(_load_tokens(pki_dir).get(token_id), token_id, secret,
                           agent_id, component)
    log.info("pki: bootstrap token validated: component=%s agent=%s", component, agent_id)


def reserve_bootstrap_token(pki_dir: Path, token: str, agent_id: str,
                            component: str) -> str:
    """原子占用一次使用额度（校验与自增在同一把锁内），返回 token_id。

    旧条目无 max_uses/uses 字段 = 不限次数（P2 签发、尚未过期的 token 保持可用）；
    额度耗尽抛 ValueError（调用方转 401）。
    """
    token_id, secret = _split_token(token)
    with _TOKENS_LOCK:
        tokens = _load_tokens(pki_dir)
        entry = _check_bootstrap_entry(tokens.get(token_id), token_id, secret,
                                       agent_id, component)
        max_uses = int(entry.get("max_uses") or 0)
        uses = int(entry.get("uses") or 0)
        if max_uses and uses >= max_uses:
            raise ValueError("bootstrap token already used")
        entry["uses"] = uses + 1
        _save_tokens(pki_dir, tokens)
    log.info("pki: bootstrap token reserved: component=%s agent=%s uses=%d/%s",
             component, agent_id, uses + 1, max_uses or "unlimited")
    return token_id


def release_bootstrap_token(pki_dir: Path, token_id: str) -> None:
    """签发失败时退回已占用的额度（成功签发不调用）。"""
    if not token_id:
        return
    with _TOKENS_LOCK:
        tokens = _load_tokens(pki_dir)
        entry = tokens.get(token_id)
        if not isinstance(entry, dict):
            return
        entry["uses"] = max(0, int(entry.get("uses") or 0) - 1)
        _save_tokens(pki_dir, tokens)


def validate_admin_token(pki_dir: Path, token: str) -> dict:
    """校验管理 token（认证器链 Bearer 用）：格式、登记、usage 含 admin、hash、TTL。

    TTL 内可复用（不消耗）；失败抛 ValueError（调用方转 401）。返回台账条目（含 groups）。
    """
    token = token.strip()
    token_id, _, secret = token.partition(".")
    if not token_id or not secret:
        raise ValueError("malformed token")
    entry = _load_tokens(pki_dir).get(token_id)
    if not entry:
        raise ValueError(f"no token for token id {token_id}")
    if "admin" not in (entry.get("usage") or []):
        raise ValueError("token is not an admin token")
    if not hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(),
                               entry.get("token_secret_hash", "")):
        raise ValueError("token invalid")
    if not _entry_active(entry.get("expires_at"), _dt.datetime.now(_dt.timezone.utc)):
        raise ValueError("token expired")
    return entry


def touch_token(pki_dir: Path, token_id: str) -> None:
    """记录 token 最近使用时间（节流：每分钟最多回写一次）。"""
    now_monotonic = time.monotonic()
    with _TOKEN_TOUCH_LOCK:
        if now_monotonic - _TOKEN_TOUCH.get(token_id, 0.0) < _TOKEN_TOUCH_INTERVAL_SECONDS:
            return
        _TOKEN_TOUCH[token_id] = now_monotonic
    now = _dt.datetime.now(_dt.timezone.utc)
    with _TOKENS_LOCK:
        tokens = _load_tokens(pki_dir)
        entry = tokens.get(token_id)
        if not isinstance(entry, dict):
            return
        entry["last_used_at"] = now.isoformat()
        _save_tokens(pki_dir, tokens)
