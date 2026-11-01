# 架构总览

Kurrent 由控制面、存储节点与设备三部分组成。控制面经 HTTPS 处理身份、台账、调度与配置；数据面承载设备与存储节点之间的块读写，永远不经过控制面。

![架构](/architecture.svg)

## 设计不变量

| 不变量 | 含义 |
| --- | --- |
| 控制面 / 数据面分离 | 控制面故障影响开通与引导，不中断已建立的数据面 I/O |
| 节点不持有系统状态 | 系统、身份与数据来自网络；设备只保留启动身份锚点 |
| 每个事实只有一个权威 | 绑定以设备台账为准，盘以 NQN 为准，声明以 YAML 为准，派生物不反向写入 |
| 默认拒绝 | 管理面按主体类声明放行；数据面缺凭据或端口未就绪即拒绝引导 |
| 收敛幂等 | `init` / `join` 可重复执行；失败回滚，不产生半应用状态 |

## 组件

- **控制面服务**：控制面容器，unix socket；设备与 Worker 台账、绑定、存储调度、引导变量、审计、配置状态。
- **nginx**：控制面容器，`:443`；唯一对外入口：WebUI、`/api/cp/`、引导链只读端点、iPXE 资产与菜单。
- **dnsmasq**：控制面宿主，host 网络；DHCP / TFTP；`dnsmasq.conf` 由声明生成。
- **WebUI**：nginx 静态资源；REST API 的客户端，使用会话 Cookie 或 mTLS。
- **CLI**：运维终端；声明生成、校验、收敛、凭据与台账操作。
- **Agent**：存储节点，`:4840` mTLS；盘文件生命周期、母盘扫描、后端指令。
- **nvmet-host**：存储节点，共享卷 UDS；configfs 操作、host 矩阵、DHHC-1、TLS PSK 注入。
- **宿主内核 nvmet**：存储节点宿主；NVMe/TCP target，数据面端口 `4421`。
- **设备固件**：设备；iPXE 网络引导、设备密钥、TOFU、NVMe/TCP 与 NBCT。

固件由独立仓库构建，见[固件概述](../firmware/index.md)。

## 端口

### 控制面宿主

| 端口 | 协议 | 用途 |
| --- | --- | --- |
| 53 | UDP/TCP | dnsmasq DNS |
| 67 / 69 | UDP | DHCP / TFTP |
| 80 | TCP | 仅 301 升级到 443 |
| 443 | TCP | 唯一 HTTPS 入口 |
| — | unix socket | 控制面管理面；nginx 与健康检查访问 |

控制面不监听 TCP，不挂载 `docker.sock`。

### 存储节点

| 端口 | 协议 | 用途 |
| --- | --- | --- |
| 4840 | TCP | Agent HTTPS（mTLS，控制面调用） |
| 4421 | TCP | NVMe/TCP 数据面，强制 TLS 1.3；固件段与内核段共用 |
| — | 共享卷 UDS | nvmet-host 管理面（mTLS） |
| 3260 | TCP | iSCSI 兼容后端（主分支无存活引导路径） |

### 设备

设备只需要能访问 DHCP、TFTP、HTTPS 与存储节点数据面；不直接访问控制面管理面。

## 数据流

### 管理请求

```text
浏览器 / CLI
  → 443 nginx
      → mTLS 校验（users/nodes 信任包 + CRL）
      → 注入身份头（仅 SUCCESS）
  → unix socket 控制面
      → 认证器链（证书 / token / 会话）
      → 主体类与 RBAC
      → 台账 / 审计 / 调用 Agent
```

### 开通与凭据

```text
控制面
  → 4840 Agent（mTLS）
      → UDS nvmet-host（mTLS）
          → configfs：subsystem / namespace / port / allowed_hosts / dhchap_key
          → 内核 .nvme keyring：TLS PSK
```

推送结果写回台账；失败项由后台对账重试。

### 引导

```text
设备
  → DHCP / TFTP（首跳）
  → HTTPS：脚本、设备上报、挑战、/boot-vars
  → 4421：NVMe/TCP + TLS 1.3，认证与 I/O
  → NBFT / NBCT → initramfs
```

控制面只在 HTTPS 段参与；数据面由设备直连存储节点。

## 状态与派生物

- **控制面声明**：`control_plane/kurrent.yaml`；权威输入。
- **节点声明**：`storager/kurrent.yaml`；权威输入。
- **设备、Worker、盘、凭据、会话、token**：`control_plane/state/*.yml`；运行时台账。
- **审计**：`control_plane/state/audit.jsonl`；追加日志。
- **PKI**：`control_plane/state/pki/`；信任根与组件证书。
- **TOFU 服务器证书**：`control_plane/state/certs/`；引导链证书与指纹。
- **dnsmasq 配置**：`dnsmasq/dnsmasq.conf`；由声明派生，不手工编辑。
- **引导脚本**：`tftp/`；菜单与脚本；固件二进制来自固件仓库。

当前没有数据库。控制面为单节点、纯文件存储；这意味着状态备份、权限与单机故障边界都由部署方承担。

## 故障域

| 故障 | 影响 |
| --- | --- |
| 控制面停止 | 新开通、新引导、配置修改与审计停止；已建立的 NVMe/TCP I/O 继续 |
| nginx 停止 | 管理面与引导链 HTTPS 不可用；数据面不受影响 |
| dnsmasq 停止 | 新设备无法 DHCP/TFTP 引导；已运行系统不受影响 |
| 单个存储节点停止 | 该节点上的盘无法启动或 I/O 中断；其他节点不受影响 |
| 控制面宿主损坏 | 需要从备份恢复；丢失 PKI/证书会导致组件重新 enroll、设备重新信任 |
| 设备损坏 | 换绑到新设备；系统盘与 Worker 身份保留 |

## 扩展点

- **存储后端**：Agent 封装 nvmet / iSCSI 差异，控制面只发 HTTP。
- **传输协议**：无状态语义不依赖具体块协议；更换传输层不改变实体模型。
- **引导行为**：菜单、超时与默认盘由控制面投影，不写入盘内。
- **固件**：独立仓库按补丁构建，可在上游基线上重建。

后续方向（高可用、IPv6、ARM、数据盘、Windows/ESXi）见[路线图](../about/roadmap.md)。

## 深入阅读

- 仓库内完整架构文档：[about/zh/ARCHITECTURE.md](https://github.com/dutyc/kurrent/blob/main/about/zh/ARCHITECTURE.md)
- 三实体与绑定：[设备、Worker 与系统盘](../concepts/entities.md)
- 启动时序：[引导链](../concepts/boot-chain.md)
- 信任边界与威胁模型：[安全模型](./security.md)
- 声明与生效：[声明式配置](../concepts/configuration.md)
