from typing import Literal

from pydantic import BaseModel, Field


class BootSpec(BaseModel):
    menu_default: str | None = None
    menu_timeout: int | None = None


class CreateWorkerRequest(BaseModel):
    """注册 Worker 身份。mac 可选：不传 = 纯空转 Worker（仅 hostname 绑定）；
    传 = 校验设备在设备池中并直接绑定（一对一授权）。系统盘须另调 POST /workers/{id}/luns/disk。"""
    worker_id: str
    mac: str | None = None
    hostname: str | None = None
    arch: str | None = None
    windows_iso: str | None = None
    boot: BootSpec | None = None


class BatchCreateWorkersRequest(BaseModel):
    """批量创建 Worker（逐项独立）：count 数量 + name_prefix 命名规则，
    worker_id = name_prefix + 序号（从 01 起，位宽按 count 自适应）。
    macs 可选：提供时须与 count 等长，逐项校验设备池并直接绑定；不传 = 全部纯空转。不支持 windows_iso。"""
    count: int = Field(..., ge=1, le=100, description="数量（1-100）")
    name_prefix: str = "worker-"
    arch: str | None = None
    macs: list[str] | None = None
    boot: BootSpec | None = None


class BindPair(BaseModel):
    """批量绑定清单项：mac + worker_id 必填；指纹比对列为可选申报值。"""
    mac: str
    worker_id: str
    manufacturer: str | None = None
    product: str | None = None
    serial: str | None = None
    uuid: str | None = None


class BatchBindRequest(BaseModel):
    """批量绑定请求：mode=manifest 用 pairs 清单配对；mode=sequential 用 macs/worker_ids 下标对齐顺序配对。"""
    mode: str = "manifest"
    pairs: list[BindPair] | None = None
    macs: list[str] | None = None
    worker_ids: list[str] | None = None


class UpdateWorkerMacRequest(BaseModel):
    """修改 Worker 的 MAC 绑定（hostname 不变）：更新 dnsmasq/dhcp-hosts.conf 中该 hostname 的绑定。"""
    mac: str


class CredentialRequest(BaseModel):
    """NVMe-oF 认证密钥（DHHC-1）设置请求。secret 必填，服务端自检（前缀/类型/长度/CRC32）。"""
    secret: str


class CreateWorkerDiskRequest(BaseModel):
    """给指定 Worker 创建系统盘 LUN（母盘克隆或空白盘）。os 为系统备注（人类理解用）；
    os_version 可空（'' = 无版本）；os_tag 由服务端随机生成（盘级标识，进 NQN/文件名）；
    remark 为盘备注（自由文本，可空）。"""
    type: Literal["master", "empty"]
    os: str
    os_version: str = ""
    name: str | None = None
    size: str | None = None
    disk_agent: str | None = None
    remark: str = ""


class AgentRoleRequest(BaseModel):
    """Agent 角色：disk=可建系统盘（存储节点），cd=可挂载 ISO（光驱节点）。"""
    disk: bool = False
    cd: bool = False


class ProbeAgentRequest(BaseModel):
    """探测 Agent 并自动推导注册参数（预览，不写任何文件）。
    身份由 mTLS 组件证书承载，无需 token。"""
    base_url: str
    agent_id: str | None = None


class CreateAgentRequest(BaseModel):
    """注册新 Agent 到 agents.yml（id 为机器身份，自动登记场景由 enroll 带入）。
    tags 自由标签（如 storage/lio/stgt），仅作展示；remark 为备注（人类识别用，可空）。"""
    id: str
    base_url: str
    storager_ip: str | None = None
    role: AgentRoleRequest = Field(default_factory=AgentRoleRequest)
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    remark: str = ""


class UpdateAgentRequest(BaseModel):
    """更新已有 Agent 的配置（id 不可改，走路径参数）；remark 为备注（人类识别用，可空）。"""
    base_url: str
    storager_ip: str | None = None
    role: AgentRoleRequest = Field(default_factory=AgentRoleRequest)
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    remark: str = ""


class BatchDiskTarget(BaseModel):
    """批量创建时单个 Worker 的存储节点分配（显式点名，绕过放置策略）。"""
    worker_id: str
    agent: str


class DiskPlacementRequest(BaseModel):
    """放置约束（仅对 workers 自动放置生效）：candidates 限定候选节点范围（空 = 全部启用磁盘角色
    节点）；pins 为逐 Worker 钉死（显式覆盖，不必在 candidates 内）。"""
    candidates: list[str] = Field(default_factory=list)
    pins: dict[str, str] = Field(default_factory=dict)


class BatchDeleteWorkersRequest(BaseModel):
    """批量删除 Worker：移除 dnsmasq 绑定与系统盘 target。
    delete_disk=true 时连系统盘 .img 一并删除；ignore_missing_target=true 容忍 target 已缺失。"""
    worker_ids: list[str]
    delete_disk: bool = False
    ignore_missing_target: bool = False


class BatchCreateWorkerDiskRequest(BaseModel):
    """批量创建系统盘：同一套盘参数应用到多个 Worker。同一 (os, os_version) 至多一块，
    重复自动跳过（不算失败）；remark 为盘备注（自由文本，可空）。

    两种定位方式二选一：
    - targets：逐项强制指定存储节点（显式覆盖）；
    - workers + placement：只声明要给哪些 Worker 建盘，存储节点由控制面放置
      （候选筛选含母盘本地存在性，按现有盘数最少均摊）。
    dry_run=true 只返回计划（plan/warnings）不建盘，与执行共用同一份放置实现。
    """
    type: Literal["master", "empty"]
    os: str
    os_version: str = ""
    name: str | None = None
    size: str | None = None
    targets: list[BatchDiskTarget] = Field(default_factory=list)
    workers: list[str] = Field(default_factory=list)
    placement: DiskPlacementRequest | None = None
    dry_run: bool = False
    remark: str = ""


class SetWorkerDefaultBootRequest(BaseModel):
    """设置 Worker 默认启动配置。disk=默认启动盘（os_tag，须为已挂系统盘）；
    menu_default/menu_timeout=菜单项覆盖；传 null 清除对应项。推导链：default_disk > boot.menu_default > reboot。"""
    disk: str | None = None
    menu_default: str | None = None
    menu_timeout: int | None = None


class MasterTagRequest(BaseModel):
    """母盘标签登记（控制面台账，备注性质）：os 为系统备注，os_version 可空（'' = 无版本），
    remark 为自由文本备注（可空，不做枚举校验）。"""
    os: str
    os_version: str = ""
    remark: str = ""


class OpenRegistrationWindowRequest(BaseModel):
    """开启注册窗口（2026-08-21 裁定：注册只在窗口期，取代 auto_register 永久开关）。
    ttl_minutes 为窗口时长，硬上限 60 分钟（代码层强制自动关闭，不可配置为永久）；
    max_devices 为放行额度（台），省略/null = 不限（仍受 TTL 约束）——
    只约束未知设备自动入池，不约束密钥认领与管理员手动登记。"""
    ttl_minutes: int
    max_devices: int | None = None


class SetEnforcementRequest(BaseModel):
    """切换设备身份验签强制开关（2026-08-21 裁定：显式强制开关）。
    enabled=true 后，无 key_hash 设备的 /boot-vars 请求直接拒绝下发（注入四条件第 4 条硬性）。"""
    enabled: bool


class CreateDiskLunRequest(BaseModel):
    """在指定 Agent 上创建磁盘 LUN（母盘克隆或空白盘）。"""
    iqn: str
    filename: str | None = None
    master: str | None = None
    size: str | None = None


class CreateCdLunRequest(BaseModel):
    """在指定 Agent 上创建 CD（ISO 虚拟光驱）LUN，仅 stgt 后端支持。"""
    iso: str
    iqn: str | None = None


class CreateDeviceRequest(BaseModel):
    """手动注册设备：MAC（+可选 SMBIOS UUID）入设备池。
    型号/序列号等申报信息仅作台账初始值，设备上报后以申报值为准更新（申报性质，不用于认证）。"""
    mac: str
    uuid: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    serial: str | None = None


class ImportDeviceEntry(BaseModel):
    """批量导入清单单行：mac 必填；uuid/型号/序列号为可选台账信息（比对列）。"""
    mac: str
    uuid: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    serial: str | None = None


class ImportDevicesRequest(BaseModel):
    """批量导入设备清单（MAC 清单预导入）：逐项独立，重复跳过，非法/吊销计 failed。"""
    entries: list[ImportDeviceEntry]
