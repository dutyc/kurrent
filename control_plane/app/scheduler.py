from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .agent_client import AgentClient, AgentConfig


class AgentRegistry:
    def __init__(self, agents_file: Path, timeout: float):
        self.agents_file = agents_file
        self.timeout = timeout

    def load(self) -> list[AgentConfig]:
        self.agents_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.agents_file.exists():
            return []
        with self.agents_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        agents_data = data.get("agents", data)
        if not isinstance(agents_data, dict):
            raise ValueError(f"invalid agents file: {self.agents_file}")
        agents: list[AgentConfig] = []
        for agent_id, raw in agents_data.items():
            if not isinstance(raw, dict):
                continue
            role = raw.get("role") or {}
            agents.append(
                AgentConfig(
                    id=str(agent_id),
                    base_url=str(raw.get("base_url", "")).rstrip("/"),
                    role_disk=bool(role.get("disk", False)),
                    role_cd=bool(role.get("cd", False)),
                    storager_ip=raw.get("storager_ip") or None,
                    enabled=bool(raw.get("enabled", True)),
                    tags=tuple(raw.get("tags") or ()),
                    remark=str(raw.get("remark", "")),
                )
            )
        return agents

    def get(self, agent_id: str) -> AgentConfig:
        for agent in self.load():
            if agent.id == agent_id:
                return agent
        raise KeyError(agent_id)

    def add(
        self,
        agent_id: str,
        base_url: str,
        *,
        role_disk: bool = False,
        role_cd: bool = False,
        storager_ip: str | None = None,
        enabled: bool = True,
        tags: tuple[str, ...] = (),
        remark: str = "",
    ) -> None:
        """注册新 Agent：写入 agents.yml（重复 id 由调用方先行校验）。"""
        self.agents_file.parent.mkdir(parents=True, exist_ok=True)
        if self.agents_file.exists():
            with self.agents_file.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        agents_data = data.get("agents")
        if not isinstance(agents_data, dict):
            agents_data = {}
            data["agents"] = agents_data
        agents_data[agent_id] = {
            "base_url": base_url,
            "role": {"disk": role_disk, "cd": role_cd},
            "storager_ip": storager_ip or "",
            "tags": list(tags),
            "enabled": enabled,
            "remark": remark,
        }
        with self.agents_file.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    def update(
        self,
        agent_id: str,
        base_url: str,
        *,
        role_disk: bool = False,
        role_cd: bool = False,
        storager_ip: str | None = None,
        enabled: bool = True,
        tags: tuple[str, ...] = (),
        remark: str = "",
    ) -> None:
        """更新已有 Agent：覆盖 agents.yml 中对应条目（id 不可改，不存在抛 KeyError）。"""
        with self.agents_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        agents_data = data.get("agents")
        entry = agents_data.get(agent_id) if isinstance(agents_data, dict) else None
        if not isinstance(entry, dict):
            raise KeyError(agent_id)
        agents_data[agent_id] = {
            "base_url": base_url,
            "role": {"disk": role_disk, "cd": role_cd},
            "storager_ip": storager_ip or "",
            "tags": list(tags),
            "enabled": enabled,
            "remark": remark,
        }
        with self.agents_file.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    def refresh_from_declaration(
        self,
        agent_id: str,
        *,
        base_url: str,
        role_cd: bool | None,
        remark: str,
    ) -> bool:
        """按节点声明刷新声明拥有的字段（base_url / remark / role.cd）。

        A2 裁定（声明权威）：storager/kurrent.yaml 是节点事实的来源，enroll/renew 时
        以节点上报对齐台账。控制面属性（enabled / storager_ip / tags）与 role.disk
        不碰；base_url / remark 为空视为"未上报"（旧 agent 兼容），保留现有值；
        role_cd=None（capabilities 未含 cd 键）同样跳过。返回是否有实际变更。"""
        with self.agents_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        agents_data = data.get("agents")
        entry = agents_data.get(agent_id) if isinstance(agents_data, dict) else None
        if not isinstance(entry, dict):
            raise KeyError(agent_id)
        changed = False
        base_url = base_url.strip().rstrip("/")
        if base_url and entry.get("base_url") != base_url:
            entry["base_url"] = base_url
            changed = True
        remark = remark.strip()
        if remark and entry.get("remark") != remark:
            entry["remark"] = remark
            changed = True
        if role_cd is not None:
            role = dict(entry.get("role") or {})
            if bool(role.get("cd", False)) != role_cd:
                role["cd"] = role_cd
                entry["role"] = role
                changed = True
        if changed:
            with self.agents_file.open("w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        return changed

    def delete(self, agent_id: str) -> None:
        """删除注册条目（不存在抛 KeyError）。"""
        with self.agents_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        agents_data = data.get("agents")
        if not isinstance(agents_data, dict) or agent_id not in agents_data:
            raise KeyError(agent_id)
        del agents_data[agent_id]
        with self.agents_file.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    def client(self, agent: AgentConfig) -> AgentClient:
        return AgentClient(agent, self.timeout)

    def select_cd_agent(self) -> tuple[AgentConfig, dict[str, Any]]:
        return self._select(lambda agent: agent.role_cd, require_cd=True)

    def _select(self, role_predicate, *, require_cd: bool) -> tuple[AgentConfig, dict[str, Any]]:
        errors: list[str] = []
        for agent in self.load():
            if not agent.enabled or not role_predicate(agent):
                continue
            if not agent.base_url:
                errors.append(f"{agent.id}: missing base_url")
                continue
            client = self.client(agent)
            try:
                client.healthz()
                caps = client.capabilities()
            except Exception as exc:
                errors.append(f"{agent.id}: {exc}")
                continue
            if require_cd and not caps.get("cd"):
                errors.append(f"{agent.id}: cd not supported by backend")
                continue
            if "base_nqn" not in caps:
                errors.append(f"{agent.id}: capabilities missing base_nqn")
                continue
            return agent, caps
        role = "cd" if require_cd else "disk"
        detail = "; ".join(errors) if errors else "no enabled agent configured"
        raise RuntimeError(f"no available {role} agent: {detail}")

    def list_public(self, live: bool = True) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for agent in self.load():
            item = agent.public_dict()
            if live and agent.enabled and agent.base_url:
                try:
                    client = self.client(agent)
                    hz = client.healthz()
                    item["health"] = "ok"
                    # 数据面就绪度：agent 进程活着但后端不可用（nvmet-host 不可达 / configfs 未就绪）
                    # → degraded（把「节点掉线」与「在线但存储不可用」区分开）
                    if hz.get("backend") and hz.get("backend_ready") is False:
                        item["health"] = "degraded"
                        item["error"] = hz.get("backend_detail", "backend not ready")
                    try:
                        item["capabilities"] = client.capabilities()
                    except Exception as exc:
                        if item["health"] != "ok":
                            item.setdefault("error", str(exc))
                        else:
                            item["health"] = "error"
                            item["error"] = str(exc)
                except Exception as exc:
                    item["health"] = "error"
                    item["error"] = str(exc)
            result.append(item)
        return result

    def storager_ip_for(self, agent_id: str) -> str:
        """数据面地址：显式配置优先，缺省回退 base_url 主机名（boot-vars 投影时兜底）。"""
        agent = self.get(agent_id)
        if agent.storager_ip:
            return agent.storager_ip
        parsed = urlparse(agent.base_url)
        return parsed.hostname or agent.base_url.removeprefix("http://").removeprefix("https://").split(":", 1)[0]
