# 架构总览

Kurrent 把物理设备、存储节点与控制面组织成一套可启动、可迁移的计算系统。设备不承载持久状态：上电后经网络完成身份校验，取得系统盘与启动参数，以 NVMe-oF 作为根设备启动；系统盘由存储节点提供，块读写发生在设备与存储节点之间。

本页说明三个角色的职责、主要数据流、信任边界、状态归属与故障域。

![架构](/architecture.svg)

## 控制面与数据面

系统沿一条清晰的线切分：控制面经 HTTPS 处理身份、调度与配置；数据面承载 NVMe-oF 块读写，永远不经过控制面。Worker 的盘流量在设备与 Storager 宿主之间直连（TCP `4421`，TLS 1.3），控制面故障只会影响开通流程，不会中断进行中的 I/O。管理操作全部收敛到 nginx 的 `443`，数据面不依赖控制面进程存活。

## 三个角色

### Controller

Controller 是集群大脑。控制面 HTTP 服务只监听容器内的 unix socket，由 nginx 以 `:443` 作为唯一对外入口，提供 WebUI、管理面 API、引导链裸路径与 iPXE 菜单；`:80` 仅做 301 升级。dnsmasq 以 host 网络提供 DHCP、TFTP 与引导文件分发。状态以纯文件（YAML / JSONL）持久化在仓库外状态根（`control-plane/state/`、`control-plane/config/`、`control-plane/dnsmasq/`），当前没有数据库；控制面声明是权威，`dnsmasq.conf` 等派生物在启动时按它渲染。WebUI 与 CLI 都只是 REST API 的客户端，状态位置与仓库解耦。

### Storager

Storager 是块存储侧。每节点运行两个组件：API Agent（`:4840`，mTLS）负责盘文件生命周期、母盘扫描与 reflink 克隆，并经 HTTP 驱动本机后端；nvmet-host 只经共享卷 UDS 暴露 mTLS 管理面，把指令翻译为 configfs 操作，并把 TLS 预共享密钥注入内核全局 `.nvme` keyring。数据面端口为 `4421`，强制 TLS 1.3，固件段与内核段共用。后端以 nvmet 为首选；iSCSI 后端代码保留，但主分支已无存活的引导路径，后端差异封装在 Agent 内部。

### Devices

Devices 是无状态计算侧。物理设备不承载持久状态：PXE 引导时上报指纹（MAC / UUID / SMBIOS / CPU / 内存 / 网卡，共 11 字段）并入设备池；绑定到 Worker 并具备系统盘后，以 NVMe-oF 为根设备启动。块读写直走数据面，不经过控制面。

## 三实体模型

角色层对应三个实体对象：设备、Worker 与系统盘。盘标识以 NQN 为权威，IQN 由 NQN 派生；绑定关系的权威在设备侧，Worker 侧只是投影。解绑或换绑时设备回池，系统盘保留在 Worker 上；一台 Worker 可挂多块系统盘，默认启动系统可在线切换。`readiness` 由「有绑定设备」与「有系统盘」共同决定。

| 实体 | 身份 | 说明 |
| --- | --- | --- |
| 设备（Device） | MAC（唯一） | 物理机器，台账条目；绑定的权威侧 |
| Worker | `worker_id`（即 hostname） | 计算身份；设备绑定后成为该 Worker 的引导节点 |
| 系统盘（System Disk） | NQN | 存储卷（母盘克隆或空白盘）；每 Worker 可挂多块 |

## 数据流

管理请求在 `443` 终止，开通由控制面主动推送到存储节点；两条路径都不经过数据面端口。

### 管理请求

管理请求先到 nginx，由它校验证书链与 CRL，仅在 `SUCCESS` 时注入身份头，再经 unix socket 进入控制面；控制面依次执行认证器链、主体类与 RBAC 判定，读写台账并记录审计，需要时调用 Agent。身份头只在 nginx 注入时可信，其他代理位置默认清空。

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

开通与凭据由控制面经 `4840` mTLS 调 Agent，再由 Agent 经 UDS 调 nvmet-host；后者写 configfs 并把结果返回，TLS PSK 写入内核 keyring。建盘时同步签发绑定 Worker 的 DHHC-1 凭据；推送失败写回台账并由后台对账重试。

```text
控制面
  → 4840 Agent（mTLS）
      → UDS nvmet-host（mTLS）
          → configfs：subsystem / namespace / port / allowed_hosts / dhchap_key
          → 内核 .nvme keyring：TLS PSK
```

## 引导链路

引导从 DHCP 与 TFTP 的首跳开始：设备取得 iPXE 与指针，再经 HTTPS 取回配置。设备首次生成并持久化在 NVRAM 的 ECDSA P-256 私钥用于挑战-应答，未知 MAC 仅在注册窗口开启且带有效公钥时入池。`/boot-vars` 依次校验绑定关系、设备签名与密钥库条目，全部通过才投影 root-path、NQN 与凭据；设备随后以 DH-HMAC-CHAP 连接、以 TLS 1.3 加密传输。固件在 sanboot 时安装 NBCT 凭据表，initramfs 消费 NBFT 与 NBCT 重连并挂载根文件系统——盘内不写死任何机器身份。

```text
设备
  → DHCP / TFTP（首跳）
  → HTTPS：脚本、设备上报、挑战、/boot-vars
  → 4421：NVMe/TCP + TLS 1.3，认证与 I/O
  → NBFT / NBCT → initramfs
```

控制面只在 HTTPS 段参与；数据面由设备直连存储节点。引导链的逐步细节见[引导链](../concepts/boot-chain.md)。

## 安全边界

管理面认证由三种凭据串行短试：mTLS 客户端证书、管理 token 与服务端会话，统一产出 `Identity` 供主体类与 RBAC 判定。凭据域拆成 users、nodes、control 三个独立根；nginx 只信任 users 与 nodes 的公钥包，按 issuer 判定主体类，控制面每个路由声明接受的主体类并默认拒绝。审计对每个管理请求写一条记录，写盘失败时请求返回 503（fail-closed）。

设备身份信任根建立在 NVRAM 私钥、一次性 nonce 的挑战-应答、TOFU 单锚与「绑定即认证」之上；指纹上报不鉴权，只喂设备池。认证与加密是彼此独立的层：DH-HMAC-CHAP 回答谁能连，TLS 1.3 保护内容，PSK 是认证密钥的纯函数，无明文回退。完整的威胁模型见[安全模型](./security.md)。

## 状态与派生物

控制面声明与节点声明是仅有的输入；前者定义网络、PKI、注册窗口与引导策略，后者定义数据目录、后端与 NQN 命名域。运行时状态位于 `<state>`，默认 `~/.local/state/kurrent`，由 `~/.kurrent/deployment.yaml` 固定；台账、会话、token、审计与 PKI 都在这里，与仓库解耦。

信任材料同样位于状态根：三分根 CA（users、nodes、control）签发人员、组件与控制面证书；TOFU 服务器证书与 CRL 供 nginx 与引导链使用；节点侧的身份、引导凭据、组件 PKI 与日志位于 `<state>/storager`。首次部署的管理凭据经 `<state>/control-plane/bootstrap` 一次性交换交给部署用户，交换包消费后删除，权威 `admin.conf` 始终留在控制面状态目录。

派生物从权威单向生成：dnsmasq 配置由 `spec.networking` 渲染，引导菜单与默认盘由控制面投影，WebUI 与引导脚本来自仓库构建。系统当前没有数据库，控制面为单节点纯文件存储，因此状态备份、文件权限与单机故障边界都由部署方承担；状态外置后，移动 checkout 不再影响台账与引导身份。

## 端口

端口按归属划分，便于确定暴露面：控制面宿主对外提供 443（80 仅做 301 升级）以及引导所需的 DNS、DHCP 与 TFTP 端口，管理面本身没有 TCP 监听；存储节点公开 4840 与 4421，其中 4840 只接受控制面 mTLS，4421 强制 TLS 1.3；设备只需访问 DHCP、TFTP、HTTPS 与存储节点数据面。

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

## 故障域

故障影响由进程归属决定：控制面、nginx 或 dnsmasq 停止只影响新开通、管理面与引导，不中断已建立的数据面 I/O；单个存储节点停止只影响该节点上的盘；控制面宿主损坏需要从备份恢复，PKI 与服务器证书丢失会导致组件重新 enroll、设备重新建立信任；设备损坏通过换绑恢复，Worker 身份与系统盘保留。

| 故障 | 影响 |
| --- | --- |
| 控制面停止 | 新开通、新引导、配置修改与审计停止；已建立的 NVMe/TCP I/O 继续 |
| nginx 停止 | 管理面与引导链 HTTPS 不可用；数据面不受影响 |
| dnsmasq 停止 | 新设备无法 DHCP/TFTP 引导；已运行系统不受影响 |
| 单个存储节点停止 | 该节点上的盘无法启动或 I/O 中断；其他节点不受影响 |
| 控制面宿主损坏 | 需要从备份恢复；丢失 PKI/证书会导致组件重新 enroll、设备重新信任 |
| 设备损坏 | 换绑到新设备；系统盘与 Worker 身份保留 |

## 协议演进

数据面已从 iSCSI 迁移到 NVMe-oF（NVMe/TCP）。固件主分支包含完整补丁集（`0001`–`0010`），整机冷启动已在多个发行版通过验收。语义——无状态计算、盘机解耦、从 MAC 到引导的身份链——不依赖具体协议：Storager 后端已由 Agent 抽象，更换传输层不触碰架构。iSCSI 在主分支已无存活的引导路径，历史实现保留在 `iscsi-classic` 分支。

## 扩展点

存储后端差异封装在 Agent 内部，控制面只发 HTTP；引导菜单、超时与默认盘由控制面投影，不写入盘内；固件由独立仓库按补丁构建，可在上游基线上重建。后续方向（高可用、IPv6、ARM、数据盘）见[路线图](../about/roadmap.md)。

本文描述的是当前阶段的架构：它是现有约束下的较优解，不是最终形态。Kurrent 的最终目的是实现无状态云原生——计算、存储与身份从单机解耦，系统可以整体调度、迁移与重建；当前的组件划分、协议与部署方式都是朝这个方向的一次实现，会随约束变化继续调整。没有最优，只有更优。

## 深入阅读

- 三实体与绑定：[设备、Worker 与系统盘](../concepts/entities.md)
- 启动时序：[引导链](../concepts/boot-chain.md)
- 信任边界与威胁模型：[安全模型](./security.md)
- 声明与生效：[声明式配置](../concepts/configuration.md)
