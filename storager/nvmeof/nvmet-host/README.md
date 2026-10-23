# nvmet-host：NVMe-oF 宿主管理服务（容器形态）

存储节点上的 nvmet（内核 configfs）管理服务。2026-08-22 裁定：**内核 nvmet 宿主原生**（数据面不走容器化），
**管理进程容器化**（由 `storager/nvmeof/docker-compose.yml` 托管——独立编排，Agent 服务内嵌，不手动跑 Python）。Agent 通过 HTTP 调用本服务完成子系统/认证管理。
盘文件管理仍归 Agent（与 stgt/LIO 一致）。

数据面**单端口**：NVMe/TCP **TLS 1.3 `4421`**（`addr_tsas=tls1.3`，内核同时把 `treq` 置 required）——
默认全链路加密，iPXE 固件引导段与 GRUB 之后的内核段**共用本口**。历史明文口 `4420` 已撤除，
升级后的宿主由启动流程清理残留（见「传输加密」节）。

## 职责边界

| 层 | 组件 | 职责 |
|---|---|---|
| 内核 | 宿主 `nvmet` / `nvmet-tcp` 模块 | 数据面 target（NVMe/TCP TLS 1.3，单口 4421） |
| 容器 | 本组件（`kurrent-nvmet-host` 容器） | configfs 操作：subsystem/namespace/数据面端口/hosts(`dhchap_key`)；PSK 注入内核全局 `.nvme` keyring；启动清理历史明文口 |
| 存储节点 | Agent（4840） | 盘文件管理（克隆/扫描）、后端调度、凭据推送转调本服务 |
| 控制面 | control-plane（unix socket，经 nginx 443 `/api/cp` 对外） | 凭据库（按 Worker）、/boot-vars 注入（含 TLS PSK/端口）、绑定关系权威 |

## 部署步骤

```bash
# 1. 宿主前置（唯一宿主步骤，需 root；幂等，含重启持久化——模块写 /etc/modules-load.d、
#    configfs 写 /etc/fstab）：
sudo bash ../host-prepare.sh
#    只读诊断（无需 root，部署前排查用）：bash ../host-prepare.sh --check

# 2. 节点侧加入（推荐主路径，仓库根执行）：生成/合并 storager/kurrent.yaml、落 bootstrap
#    凭据、按 backend 起整套编排（Agent + 本组件），详见 cli/README.md 与节点声明示例；
#    join 对 nvmet 后端预检宿主前置（模块 + configfs），未就绪报错并指路步骤 1。
kurrent join <cp-url> --token <bootstrap-token>   # token: kurrent token create 签发

# 3. 仅重启本编排（声明/镜像已就位时；改过组件代码则加 --build）：
cd storager/nvmeof
docker compose --env-file ../.env up -d

# 4. 验证（宿主 loopback 映射；mTLS：须带本组件 client cert 访问）
curl -sk --cert ../../control_plane/state/pki/components/nvmet-<agent_id>/client.crt \
        --key  ../../control_plane/state/pki/components/nvmet-<agent_id>/client.key \
        https://127.0.0.1:4841/healthz
# → {"status":"ok","configfs":true}
```

> **本组件无独立配置**：身份（`metadata.name` = agent_id，证书 CN=`nvmet-<agent_id>`）与 enroll
> 入口（`spec.controlPlane.url`）都读同一份节点声明 `storager/kurrent.yaml`（与 Agent 共享、只读挂载）；
> 一次性引导凭据读 `storager/bootstrap/nvmet-host.token`（由 Agent enroll 上报 `backend=nvmet` 时
> 控制面派生下发，**不手工签发**）。

> 容器无 privileged：configfs bind mount 后容器内 root 写操作是普通文件写 + symlink 创建，无需特权。
> 本服务 host 网络模式直监宿主 4841（局域网不可达不成立——请自行用防火墙限制 4421/4841 暴露面）；
> Agent 经 `https://host.docker.internal:4841`（bridge 网络 extra_hosts host-gateway）访问，mTLS 客户端证书鉴权。

> **宿主前置（TLS 数据面，缺任一即数据面不可用）**：内核带 `CONFIG_NVME_TCP_TLS` /
> `CONFIG_NVME_TARGET_TCP_TLS` / `CONFIG_NVME_KEYRING`，且宿主常驻 **tlshd**（ktls-utils，内核 TLS 握手
> 走它做用户态 upcall；两端都要）。本服务启动时 `ensure_tls_port()` 建 `ports/2`（`4421`，`tsas=tls1.3`），
> 失败则 `tls_ready` 保持 false、`/capabilities.port.ready` 如实报 false——控制面据此**拒绝引导**
> （默认全链路加密，不做明文降级）。
> 注：`storager/nvmeof/host-prepare.sh` 与 `kurrent join` 目前**只预检 nvmet/nvmet-tcp 与 configfs**，
> 尚未预检上述 TLS 条件与 tlshd（部署前请手工确认）。

## 环境变量

本组件几乎不读环境变量——**身份 / 数据面参数 / 引导凭据都不经 env 下发**（见上）。实际读取的只有：

| 变量 | 默认 | 说明 |
|---|---|---|
| `KURRENT_CONFIG_FILE` | `/etc/kurrent/kurrent.yaml` | 节点声明路径覆盖（测试注入用；compose 挂载 `../kurrent.yaml` 只读） |
| `NVMET_CONFIGFS` | `/sys/kernel/config/nvmet` | configfs 根（测试可注入） |
| `NVMET_TLS_PORT_ID` | `2` | 数据面端口 ID（**空字符串 = 关闭数据面**，联调用；控制面因端口未就绪拒绝引导） |
| `NVMET_TLS_TRSVCID` | `4421` | 数据面端口 trsvcid（`tsas=tls1.3`） |
| `NVMET_PLAIN_PORT_ID` | `1` | 历史明文口 ID——**仅启动迁移用**：存在即拆除（先摘挂载再删口），不再创建/挂载 |
| `NVMET_PLAIN_TRSVCID` | `4420` | 历史明文口 trsvcid（仅用于迁移日志措辞） |
| `NVMET_KEYS_FILE` | `/proc/keys` | 定位内核 `.nvme` keyring serial 用（测试注入） |

监听地址/端口为模块常量（`0.0.0.0:4841`，host 网络即宿主栈），compose 不注入。

以下变量**此前**以 env 下发，现已收敛（`.env` 里若残留，`kurrent join` 会主动清掉）：

| 旧变量 | 现来源 |
|---|---|
| `KURRENT_AGENT_ID` | 声明 `metadata.name`（bootstrap token 绑定的节点身份，join 写入） |
| `KURRENT_COMPONENT` | 代码常量（`nvmet-host`，pki_client 调用点；CSR CN 前缀 `nvmet`） |
| `KURRENT_PKI_DIR` | compose 挂载（宿主 `${KURRENT_NVMET_PKI_HOST}` → `/etc/kurrent/pki`） |
| `KURRENT_CP_ENROLL_URL` | 声明 `spec.controlPlane.url` |
| `KURRENT_CP_CA` | compose 挂载（控制面 `server.crt` → `/etc/kurrent/cp-ca.crt:ro`） |
| `KURRENT_BOOTSTRAP_TOKEN(_NVMET)` | `storager/bootstrap/nvmet-host.token`（Agent enroll 派生，只读挂载） |

> 例外：`KURRENT_DISK_DIR` 与 `KURRENT_NVMET_PKI_HOST` **仍是 `.env` 插值键**——它们是 compose
> 挂载源（不经组件代码读取），值由 `kurrent join` 从声明同步写入。

## API（全部 mTLS 鉴权，Agent 是唯一调用方）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/healthz` | 探活（TLS 层要求客户端证书，链到 nodes CA） |
| `GET` | `/capabilities` | 后端能力（backend=nvmet、cd=false、`port` = 数据面端口含 `port_id`/`trsvcid`/`tsas`/`ready`；数据面关闭时为 `null`） |
| `POST` | `/port` | 幂等确保数据面端口（NVMe/TCP TLS 1.3，无参数） |
| `GET` | `/subsystems` | 子系统清单（含 namespaces/hosts） |
| `POST` | `/subsystems` | `{nqn, backing}` 创建子系统 + namespace/1（严格模式 allow_any_host=0）+ 挂数据面端口（端口未就绪时暂不挂） |
| `DELETE` | `/subsystems/{nqn}` | 删除（自动摘数据面端口挂载） |
| `PUT` | `/subsystems/{nqn}/hosts` | `{hostnqn, secret}` 登记/更新 host 认证（DHHC-1 明文写入全局 `hosts/<hostnqn>/dhchap_key`，**写 key 即启用认证**——无独立 control 属性——再把该 host symlink 进子系统 `allowed_hosts/` 完成准入） |
| `DELETE` | `/subsystems/{nqn}/hosts/{hostnqn}` | 移除 host 认证 |
| `PUT` | `/subsystems/{nqn}/tls` | `{hostnqn, psk}`（64 hex = 32 字节）注入 TLS-PSK 到内核 `.nvme` keyring，identity `NVMe0R01 <hostnqn> <subsysnqn>`；数据面关闭时 409（无可用端口） |
| `DELETE` | `/subsystems/{nqn}/tls/{hostnqn}` | 摘除该 host 的 PSK（无则 `deleted:false`，幂等） |

## 认证模型（按 Worker 跟盘，2026-08-22 裁定）

- 子系统 = 盘，**NQN 为权威**：`<base_nqn>:<worker_id>.<os>.<os_tag>`（如
  `nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89`；`os_tag` 为盘级 12 位随机 hex），
  IQN 由 NQN 派生而非相反；`attr_allow_any_host=0`（严格）
- 连接认证 = DH-HMAC-CHAP（target 认证 host）：客户端 Host NQN（worker 维度派生 `nqn.2026-07.com.kurrent:host.<worker_id>`）
  须在全局 `hosts/<hostnqn>/` 有对应条目：`dhchap_key` = 该 worker 的 DHHC-1 密钥
  （`DHHC-1:01:<base64>` 明文，无换行）——**写 key 即启用认证，内核无独立 control 属性**
- `allowed_hosts/` 是**准入白名单**：严格模式下必须把全局 `hosts/<hostnqn>` symlink 进
  `subsystems/<nqn>/allowed_hosts/`，该 host 才被准许连接（内核要求 target 是 `nvmet_host_type`，
  否则 EINVAL）；反之 `attr_allow_any_host=1` 时内核**拒绝** link 显式 host——所以准入始终走严格模式
- hosts 矩阵随绑定关系同步：控制面在 **nvmet 建盘即自动签发密钥**、凭据轮换/吊销、设备换绑时推送 Agent，
  Agent 转调本服务
- 无 UUID 回退：Host NQN 恒为 worker 派生（`nqn.2026-07.com.kurrent:host.<worker_id>`），每 worker 单条目
- 同一 worker 挂多盘时多子系统共用一个 hostnqn：吊销逐个摘链，全局条目在最后一个引用摘除后回收
  （内核 configfs 对仍被 allowed_hosts 引用的目录 rmdir 返回 EBUSY，按引用扫描处理）

## 传输加密（NVMe/TCP TLS 1.3，单端口）

设计（源码级机制、实测证据与验收判据）见本地蓝图层 `blueprint/nvme-tcp-tls-in-kernel.md` /
`blueprint/nvme-tcp-tls-in-firmware.md`（含主仓改动清单 `blueprint/nvme-tcp-tls-kurrent-changes.md`；
`blueprint/` 不入版本库）。

- **单端口全链路加密**：数据面只有 `4421` 一个口（`tsas=tls1.3`，写 tsas 时内核自动置 `treq=required`）；
  iPXE 固件段（`sanboot` 直连，固件自带 TLS 1.3 PSK 客户端）与 GRUB 之后的内核段（`nvme connect --tls`）
  **共用本口**。历史明文口 `4420` 不再创建：启动时 `remove_legacy_plain_port()` 拆除残留（先摘
  `ports/1/subsystems/*` 挂载再 `rmdir`），使升级后的宿主收敛到单口。
- **PSK 是认证密钥的纯函数**：`HMAC-SHA256(raw_key, b"kurrent-nvme-tls-psk-v1")` → 64 hex，由控制面
  `workers._derive_tls_psk` 现算——不新增存储字段，轮换/吊销认证密钥即原子地轮换/吊销 PSK。
- **目标端无需 configfs 密钥配置**：nvmet 建 port 时即取内核全局 `.nvme` keyring（`port->keyring`），
  只需把 `psk` key 放进去。identity = `NVMe0R01 <hostnqn> <subsysnqn>`（ver 0 不带 digest，内核按前缀匹配）。
- **两个实测坑（已固化在 `NvmePskKeyring`）**：`psk` key 的 possessor 权限不含 Write ⇒ `add_key` 不会就地
  更新同名 key，**必须先摘旧 key 再写**（一条 identity 只留一条）；淘汰**必须限定在 `.nvme` 内**
  （用 `KEYCTL_READ` 读 keyring 挂载列表定位），照 `/proc/keys` 全局同名条目找会 UNLINK 得
  `ENOENT` 且旧 key 越积越多。
- **容器前置**：`security_opt` 需 `seccomp=unconfined`（Docker 默认 seccomp 拒 `add_key`/`keyctl` → EPERM）
  与 `systempaths=unconfined`（`/proc/keys` 被 MaskedPaths 屏蔽成 `/dev/null`）——缺任一即静默失败。
- **验收状态（2026-10-06）**：目标端供给（1.3a）、控制面派生→推送→目标端（1.3b）、
  内核段冷启动（1.3c，`:4421` pcap 零明文、`TlsTxSw`/`TlsRxSw` 非零）与固件段 TLS 1.3 PSK
  客户端（段 2.1/2.2，串口两次握手 + 注册 SAN、`:4421` 客户端流首字节全 `0x16`）均已端到端实测通过；
  段 2.3 端口收口（本 README 描述的单口形态）代码/测试就位。

## 盘文件要求（4K 原生 GPT）

nvmet file backend 的逻辑块大小跟随底层 FS 块（典型 4096 B，对 file backing 不做 512 模拟）：
**母盘 GPT 必须按 4096 字节扇区书写**（GPT 头位于 LBA1，即文件 offset 4096）。

512 字节扇区布局的母盘在 4K 导出下被发起端按 4K 解释，EDK2 找不到 ESP
（生产实测 sanboot 报 `EFI_NOT_FOUND`）。Agent 建盘时校验母盘布局：512 直接拒绝并给出重建指引；
`GET /masters` 的 `gpt` 字段（`4k` / `512` / `none`）供前端标注。

制盘要点：在 4K 原生设备（或 `losetup --sector-size 4096` 的 loop 设备）上建分区表——
`sgdisk` / `parted` 跟随设备逻辑扇区大小；512 布局的存量母盘需在 4K 视图下重建分区表后再投入使用。

## 安全边界

- 本服务监听宿主网络栈 4841：mTLS（uvicorn `ssl_cert_reqs=2` + `ssl_ca_certs=ca-nodes.crt`）强制客户端证书且校验链；
  身份边界 = nodes CA 签发范围（签发受控于绑定 agent_id 的派生 token + CSR CN 校验 + 组件登记）
- 容器 `security_opt`：`apparmor:unconfined`（Ubuntu docker-default profile 拒 configfs 写入）、
  `seccomp=unconfined` + `systempaths=unconfined`（写内核 `.nvme` keyring 所需，见「传输加密」节）——
  三条都是**为完成既定职责**而放行的窄面豁免，生产建议改自定义 profile 只放行 configfs 与 `add_key`/`keyctl`
- configfs 操作需要 root（容器以 root 运行，bind mount 直写宿主内核配置面）；盘文件不经本服务
- 数据面端口 `4421`（NVMe/TCP TLS 1.3）为宿主监听，暴露面自行用防火墙约束
- 审计：Agent 侧 operations 记录凭据推送结果（含 `tls: bool`，不记密钥本体）；本服务不落盘任何状态
  （configfs 与内核 keyring 即真相）
