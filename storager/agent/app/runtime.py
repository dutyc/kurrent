"""运行态派生常量：从节点声明一次性计算（不再读环境）。

盘标识命名空间权威是 NQN；iSCSI 数据面消费的 IQN 由 NQN 同后缀派生。
容器内路径（盘目录挂载点/日志）属部署清单 compose 职责，取自 config 模块常量。
"""

from .config import CONFIG, DEFAULT_DISK_DIR, DEFAULT_LOG_FILE


def nqn_to_iqn(nqn: str) -> str:
    """NVMe NQN → iSCSI IQN（同后缀前缀变换，派生方向：NQN 权威，IQN 自动生成）：
    nqn.2026-07.com.kurrent:worker-01.ubuntu → iqn.2026-07.com.kurrent:worker-01.ubuntu。
    节点只声明 NQN 命名空间（spec.agent.nqnBase），iSCSI 数据面消费的 IQN 由此派生。"""
    nqn = nqn.strip().lower()
    if nqn.startswith("iqn."):
        return nqn
    if nqn.startswith("nqn."):
        return "iqn." + nqn[4:]
    return "iqn." + nqn


DISK_DIR = DEFAULT_DISK_DIR  # 容器内盘目录挂载点（宿主路径见 kurrent.yaml spec.agent.diskDir，compose 挂载关联）
NQN_BASE = CONFIG.spec.agent.nqn_base              # 权威：盘标识命名空间（NVMe-oF 首选协议）
IQN_BASE = nqn_to_iqn(NQN_BASE)                    # 派生：iSCSI 数据面（同后缀前缀变换）
BACKEND = CONFIG.spec.agent.backend
LOG_FILE = DEFAULT_LOG_FILE  # 容器内路径（compose 挂载目标，部署清单职责）
