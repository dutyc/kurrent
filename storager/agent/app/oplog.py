"""append-only JSON Lines 操作日志（审计与运维查询共用）。"""

import datetime
import json
import os
import threading

from .runtime import LOG_FILE


class OperationLog:
    """append-only JSON Lines 操作日志。id 用行号当游标（MVP 不轮转前提下成立）。"""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.next_id = 0
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.next_id = sum(1 for line in f if line.strip())

    def record(self, op: str, req: dict, result: str, client: str, **extra) -> None:
        with self.lock:
            self.next_id += 1
            entry = {"id": self.next_id,
                     # 跟随容器时区（/etc/localtime 挂载或 TZ 环境变量），输出带偏移的本地时间
                     "ts": datetime.datetime.now().astimezone().isoformat(),
                     "op": op, "req": req, "result": result, "client": client}
            entry.update(extra)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read(self, since: int = 0, limit: int = 1000) -> dict:
        entries = []
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("id", 0) > since:
                        entries.append(e)
                        if len(entries) >= limit:
                            break
        next_cursor = entries[-1]["id"] if entries else since
        return {"next_cursor": next_cursor, "entries": entries}


oplog = OperationLog(LOG_FILE)
