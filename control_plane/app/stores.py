"""全局单例（模块导入时实例化，进程内共享）：存储、Agent 注册表、dnsmasq、运行时设置与审计记录。"""

import logging

from .config import settings
from .dnsmasq import DnsmasqHosts
from .scheduler import AgentRegistry
from .state import (CredentialPushStore, CredentialStore, DeviceStore, FileStateStore,
                    MasterTagStore, OperationLog, RuntimeSettings)

log = logging.getLogger("control-plane")

store = FileStateStore(settings.workers_file)
devices = DeviceStore(settings.devices_file)
operations = OperationLog(settings.operations_file)
agents = AgentRegistry(settings.agents_file, settings.agent_timeout)
dnsmasq = DnsmasqHosts(settings.dnsmasq_hosts_file)
runtime_settings = RuntimeSettings(settings.settings_file)
credentials = CredentialStore(settings.credentials_file)
credential_pushes = CredentialPushStore(settings.credential_push_file)
master_tags = MasterTagStore(settings.masters_file)


def record(op: str, status: str, **extra) -> None:
    """审计日志（写入失败仅记日志，不阻断业务）。"""
    try:
        operations.record(op, status, **extra)
    except Exception:
        log.exception("failed to write operation log")
