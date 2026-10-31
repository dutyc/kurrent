# 术语表

| 术语 | 含义 |
| --- | --- |
| Device（设备） | 一台裸机在控制面中的身份记录，以 MAC 为键。绑定权威在设备台账的 `bound_worker_id`。 |
| Worker（工作节点） | 计算身份与系统盘的归属对象。同一 Worker 可以先后运行在不同设备上。 |
| System disk（系统盘） | 从母盘克隆出的可引导磁盘镜像，归属某个 Worker，用 `os_tag` 区分。 |
| Master（母盘） | 存放于存储节点 `diskDir`、文件名含 `_tpl_` 的可克隆模板。必须为 4K 原生 GPT。 |
| `os_tag` | 系统盘的 12 位十六进制标识，用于在同一 Worker 下区分多个系统。 |
| Default disk（默认盘） | Worker 自动启动时选择的系统盘。Worker 只有一块盘时自动成为默认盘。 |
| Pooled | 设备已登记但未绑定 Worker。 |
| Bound | 设备已绑定 Worker，可以进入该 Worker 的引导链。 |
| Revoked | 设备被吊销，不再更新、不再入池，也不能被重新登记。 |
| Binding（绑定） | 设备到 Worker 的一对一授权。绑定即认证；换绑即冷迁移。 |
| Readiness | Worker 是否具备可引导条件：已绑定设备、有系统盘、数据面可用。 |
| Cold migration（冷迁移） | 关闭设备 A，把设备绑定切换到 B，由 B 启动同一 Worker 的系统盘。 |
| Drain（排空） | 将节点 `enabled` 置为否：不再接受新建盘，已有盘继续服务。 |
| Registration window（注册窗口） | 允许未知设备登记入池的时间窗口，受 TTL 与设备额度约束。 |
| Enrollment（注册/加入） | 组件使用 bootstrap token 换取客户端证书并登记到控制面的过程。 |
| Bootstrap token | 绑定节点身份的一次性引导凭据，用于节点 enroll。 |
| Management token | 供脚本与 WebUI 登录的管理面 Bearer token，只存摘要。 |
| Session（会话） | 服务端登记的登录状态；删除台账条目即失效。 |
| Principal class（主体类） | `human` / `component` / `automation` / `anonymous`；路由按类授权，默认拒绝。 |
| CA（证书颁发机构） | 控制面维护 users / nodes / control 三套根，分别对应人员、节点组件与控制面客户端。 |
| CRL | 证书吊销列表；组件面定期拉取并按序列号拒绝已吊销证书。 |
| TOFU | Trust On First Use：首次 HTTPS 握手固定服务器叶证书指纹，之后不匹配即拒绝。 |
| NQN | NVMe Qualified Name，NVMe-oF 子系统与主机的标识。 |
| Host NQN | 主机侧 NQN，按节点 `nqnBase` 与 Worker 派生，用于 target 侧主机授权。 |
| IQN | iSCSI Qualified Name，iSCSI 后端使用的标识。 |
| `nqnBase` | 节点声明的 NQN 命名域，Host NQN 与盘 NQN 都从它派生。 |
| DHHC-1 | DH-HMAC-CHAP 密钥的文本格式：`DHHC-1:01:<base64(key+CRC32)>`。 |
| TLS PSK | 由 DHHC-1 密钥派生的 NVMe/TCP TLS 预共享密钥，与认证密钥同生命周期。 |
| NVMe/TCP | NVMe over TCP，当前主数据面传输。 |
| Data plane（数据面） | 设备与存储节点之间的块设备传输，不经过控制面。 |
| SAN boot | 从网络存储盘直接引导，本项目中由 iPXE `sanboot` 实现。 |
| iPXE | 本项目使用的开源网络引导固件。 |
| PXE | 预启动执行环境，设备通过 DHCP + TFTP 取得首跳固件。 |
| TFTP | 首跳文件传输协议；当前只提供 `snponly.efi` 与 `boot.ipxe` 指针。 |
| `snponly` | 仅使用 UEFI SNP 栈的 iPXE 产物，用于原生驱动不可用的网卡。 |
| NBFT | NVMe Boot Firmware Table，描述网络引导目标的发现表。 |
| NBCT | NVMe Boot Credentials Table，固件交给 initramfs 的会话凭据表。 |
| initramfs | 内核启动早期使用的临时根文件系统，负责按 NBFT/NBCT 重连数据面。 |
| `keep-san` | iPXE 选项：启动后保留 SAN 连接，交给后续阶段使用。 |
| Reflink | 写时复制克隆；母盘到系统盘的快速复制依赖文件系统支持。 |
| `configHash` | 生效声明的规范化 SHA-256，用于确认运行态加载了哪份配置。 |
| Drift（漂移） | 声明与实际派生文件或证书不一致；`config status` 报告。 |
| Registration quota | 单个注册窗口允许放行的设备数上限。 |

## 相关页面

- [设备、Worker 与系统盘](../concepts/entities.md)
- [引导链](../concepts/boot-chain.md)
- [存储与数据面](../concepts/storage.md)
- [身份与访问](../concepts/identity.md)
- [固件契约](./firmware-contract.md)
