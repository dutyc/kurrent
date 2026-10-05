import contextvars
import datetime as _dt
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

# 当前请求身份（app/auth.py 认证器链写入）：OperationLog.record 自动附加审计 actor，
# 各业务调用点无需逐个传递（无认证上下文的内部调用记空）
current_actor: contextvars.ContextVar[str] = contextvars.ContextVar("kurrent_actor", default="")


class FileStateStore:
    def __init__(self, workers_file: Path):
        self.workers_file = workers_file
        self._lock = threading.RLock()

    @contextmanager
    def locked(self):
        with self._lock:
            yield

    def load_workers(self) -> dict[str, Any]:
        data = _load_yaml(self.workers_file, {"workers": {}})
        workers = data.get("workers")
        if workers is None:
            data["workers"] = {}
        if not isinstance(data["workers"], dict):
            raise ValueError(f"invalid workers file: {self.workers_file}")
        return data

    def save_workers(self, data: dict[str, Any]) -> None:
        if "workers" not in data:
            data["workers"] = {}
        _atomic_write_text(self.workers_file, yaml.safe_dump(data, sort_keys=True, allow_unicode=False))


class DeviceStore:
    """设备台账存储（devices.yml）：原子写 + 线程锁，模式同 FileStateStore。
    绑定关系唯一权威（bound_worker_id），worker 侧只投影不存储。"""

    def __init__(self, devices_file: Path):
        self.devices_file = devices_file
        self._lock = threading.RLock()

    @contextmanager
    def locked(self):
        with self._lock:
            yield

    def load(self) -> dict[str, Any]:
        data = _load_yaml(self.devices_file, {"devices": {}})
        devices = data.get("devices")
        if devices is None:
            data["devices"] = {}
        if not isinstance(data["devices"], dict):
            raise ValueError(f"invalid devices file: {self.devices_file}")
        return data

    def save(self, data: dict[str, Any]) -> None:
        if "devices" not in data:
            data["devices"] = {}
        _atomic_write_text(self.devices_file, yaml.safe_dump(data, sort_keys=True, allow_unicode=False))


class MasterTagStore:
    """母盘标签台账（masters.yml）：键 = agent_id → 母盘名 → {os, os_version}。
    标签为备注性质（人类理解用），由控制面登记；母盘本体在 Agent 侧文件系统扫描。
    按库表形态建模（未来 masters(agent_id, name, os, os_version) 表）：字段原子、
    空版本用 ''（避免 SQL NULL 不参与复合唯一键的语义陷阱）。"""

    def __init__(self, masters_file: Path):
        self.masters_file = masters_file
        self._lock = threading.RLock()

    @contextmanager
    def locked(self):
        with self._lock:
            yield

    def load(self) -> dict[str, Any]:
        data = _load_yaml(self.masters_file, {"masters": {}})
        masters = data.get("masters")
        if masters is None:
            data["masters"] = {}
        if not isinstance(data["masters"], dict):
            raise ValueError(f"invalid masters file: {self.masters_file}")
        return data

    def save(self, data: dict[str, Any]) -> None:
        if "masters" not in data:
            data["masters"] = {}
        _atomic_write_text(self.masters_file, yaml.safe_dump(data, sort_keys=True, allow_unicode=False))


class CredentialStore:
    """NVMe-oF 认证凭据库（credentials.yml）：键 = worker_id（按 Worker 跟盘裁定，2026-08-22）。
    原子写 + 线程锁，模式同 FileStateStore/DeviceStore。DHHC-1 明文（启动注入必需）与
    secret_hash（审计比对）并存——与控制面「只存哈希」原则的差异及理由见
    blueprint/nvmeof-credential-design.md 4.1 秘密分类。"""

    def __init__(self, credentials_file: Path):
        self.credentials_file = credentials_file
        self._lock = threading.RLock()

    @contextmanager
    def locked(self):
        with self._lock:
            yield

    def load(self) -> dict[str, Any]:
        data = _load_yaml(self.credentials_file, {"credentials": {}})
        creds = data.get("credentials")
        if creds is None:
            data["credentials"] = {}
        if not isinstance(data["credentials"], dict):
            raise ValueError(f"invalid credentials file: {self.credentials_file}")
        return data

    def save(self, data: dict[str, Any]) -> None:
        if "credentials" not in data:
            data["credentials"] = {}
        _atomic_write_text(self.credentials_file, yaml.safe_dump(data, sort_keys=True, allow_unicode=False))


class CredentialPushStore:
    """凭据推送状态台账（credential-push.yml）：键 = "<worker_id>/<agent_id>"。

    期望状态（credentials.yml）与观测状态（本文件）分离：记录每个 (worker, agent) 对
    最后一次推送的结果（ok/failed/n-a）与所推密钥的审计哈希，供控制面对账比较
    （跳过已送达对）与状态投影（push 视图）。secret_hash 为 "" 表示已推送吊销。
    """

    def __init__(self, push_file: Path):
        self.push_file = push_file
        self._lock = threading.RLock()

    @contextmanager
    def locked(self):
        with self._lock:
            yield

    def load(self) -> dict[str, Any]:
        data = _load_yaml(self.push_file, {"pushes": {}})
        pushes = data.get("pushes")
        if pushes is None:
            data["pushes"] = {}
        if not isinstance(data["pushes"], dict):
            raise ValueError(f"invalid credential push file: {self.push_file}")
        return data

    def save(self, data: dict[str, Any]) -> None:
        if "pushes" not in data:
            data["pushes"] = {}
        _atomic_write_text(self.push_file, yaml.safe_dump(data, sort_keys=True, allow_unicode=False))


class RuntimeSettings:
    """运行时设置（任意 JSON 值）：文件存在则覆盖环境变量默认值（进程内立即生效、重启保留），
    文件不存在时回退默认。布尔开关（auto_register 已废止）与结构化字段（注册窗口）共用。
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    @contextmanager
    def locked(self):
        with self._lock:
            yield

    def get(self, key: str, default: Any = None) -> Any:
        data = _load_yaml(self.path, {})
        if key in data:
            return data[key]
        return default

    def set(self, key: str, value: Any) -> Any:
        with self._lock:
            data = _load_yaml(self.path, {})
            data[key] = value
            _atomic_write_text(self.path, yaml.safe_dump(data, sort_keys=True))
            return value


class OperationLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._next_id = 0
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                self._next_id = sum(1 for line in f if line.strip())

    def record(self, op: str, status: str, **extra: Any) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            entry = {
                "id": self._next_id,
                # 跟随容器时区（/etc/localtime 挂载或 TZ 环境变量），输出带偏移的本地时间
                "ts": _dt.datetime.now().astimezone().isoformat(),
                "op": op,
                "status": status,
            }
            entry.update(extra)
            if "actor" not in entry and current_actor.get():
                entry["actor"] = current_actor.get()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return entry

    def read(self, since: int = 0, limit: int = 1000) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("id", 0) > since:
                        entries.append(entry)
                        if len(entries) >= limit:
                            break
        return {"next_cursor": entries[-1]["id"] if entries else since, "entries": entries}


def _load_yaml(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return dict(default)
    with path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"expected YAML mapping in {path}")
    return loaded


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

