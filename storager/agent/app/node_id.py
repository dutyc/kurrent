"""节点身份读取：12 位小写 hex，由 kurrent join 写入节点状态文件。

身份是节点状态，不属于声明式配置（storager/kurrent.yaml）。compose 把宿主
storager/state 只读挂载到 /etc/kurrent/state；KURRENT_NODE_ID_FILE 可覆盖路径
（测试注入用）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_NODE_ID_FILE = "/etc/kurrent/state/agent.id"

_AGENT_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def load_node_id(path: str | Path | None = None) -> str:
    """读取并校验节点身份；缺失或非法直接阻断启动。"""
    node_id_path = Path(
        path or os.environ.get("KURRENT_NODE_ID_FILE") or DEFAULT_NODE_ID_FILE)
    try:
        value = node_id_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            f"missing node identity: {node_id_path} "
            "(run 'kurrent join' to write it)") from exc
    if not _AGENT_ID_RE.fullmatch(value):
        raise RuntimeError(
            f"invalid node identity in {node_id_path}: "
            "expected 12 lowercase hex characters")
    return value
