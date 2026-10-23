"""管理用户目录（state/users.yml）：CN → 组。

证书只证明「CN 是谁」；组由本文件决定，因此改权限不需要重签证书，
删条目立即失去管理面授权（会话在每次请求时重新查表）。
未知 CN 返回 None，由调用方按未授权处理（默认拒绝）。
"""

import logging
import os
from pathlib import Path

import yaml

log = logging.getLogger("control-plane")


def ensure_users_file(path: Path, admin_cn: str) -> None:
    """首次启动创建管理员条目；已存在则原样保留（不覆盖人工修改）。"""
    path = Path(path)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"users": {admin_cn: {"groups": ["admin"]}}}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    log.info("users: created %s (admin=%s)", path, admin_cn)


def user_groups(path: Path, cn: str) -> list[str] | None:
    """返回 CN 的组列表；用户不存在/文件不可读返回 None。"""
    if not cn:
        return None
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    users = doc.get("users") if isinstance(doc, dict) else None
    if not isinstance(users, dict):
        return None
    entry = users.get(cn)
    if not isinstance(entry, dict):
        return None
    groups = entry.get("groups")
    if not isinstance(groups, list):
        return []
    return [g for g in groups if isinstance(g, str) and g]
