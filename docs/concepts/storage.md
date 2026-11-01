# 存储与数据面

存储节点承载母盘与系统盘，并把块设备导出给设备。控制面只负责编排与签发凭据，块读写不经过控制面。

| 组件 | 位置 | 职责 |
| --- | --- | --- |
| Storager Agent | 存储节点 `:4840`（mTLS） | 盘文件生命周期、母盘扫描、克隆、删除；向本机后端下发指令 |
| nvmet-host | 共享卷 UDS（mTLS） | 操作内核 configfs：subsystem / namespace / port / allowed_hosts / DHHC-1；注入 TLS PSK |
| 宿主内核 nvmet | 存储节点宿主 | NVMe/TCP target，数据面端口 `4421` |

后端以 nvmet 为主。iSCSI（stgt / lio）代码保留为兼容后端，但主分支没有存活的引导路径，历史实现见 `iscsi-classic` 分支。后端差异封装在 Agent 内部，控制面只发 HTTP。

## 母盘

母盘是系统盘的克隆源，不参与启动。放在存储节点声明 `spec.agent.diskDir` 下，文件名包含 `_tpl_` 标记（大小写不敏感）：

```text
/pool1/disk_dir/_tpl_ubuntu2604.img
```

后台扫描周期缓存母盘清单，包含：

- 文件名、大小与 mtime；
- GPT 扇区布局（`4k` / `512` / `none`）；
- 人工登记的标签（`os`、`os_version`、备注）。

要求：

- **4K 原生 GPT**。nvmet file backend 按底层文件系统块（典型 4096 B）导出，512 字节扇区 GPT 在固件侧会被视为无分区。Agent 拒绝克隆，nvmet-host 拒绝导出，引导会报 `Boot from SAN device ... failed`。
- **reflink 文件系统**。btrfs / xfs 上克隆接近秒级；ext4 不支持 reflink，会退化为全量复制。
- 母盘标签只是展示与选择元数据，不参与身份或数据面运算。

## 系统盘

系统盘由控制面创建，两种来源：

| 类型 | 说明 |
| --- | --- |
| `master` | 从指定母盘克隆；母盘必须在该 Agent 本地存在 |
| `empty` | 创建指定大小的空白盘，用于安装系统 |

创建时控制面生成 `os_tag`、盘 NQN 与 backing 文件名，并写入 Worker 台账：

```text
nqn      = <nqnBase>:<worker_id>.<os>.<os_tag>
filename = <worker_id>.<os>.<os_tag>.img
```

- 同一 Worker 的同一 `(os, os_version)` 至多一块盘；不同版本可并存。
- 第一块盘自动成为默认盘；后续盘不自动改写默认盘。
- 放置由控制面决定；候选须持有母盘、启用、数据面就绪，按现有盘数最少均摊。详见[设备、Worker 与系统盘](./entities.md#放置)。

删除盘时可以选择是否删除 backing 文件。只删 target 时，盘文件仍可重新挂载；删除默认盘会自动提升剩余盘。

## 数据面

设备与存储节点之间只有一条数据面路径：

```text
设备固件 / 内核 ── NVMe/TCP :4421（TLS 1.3）──► nvmet target
```

- **单端口**。固件段和内核段共用 `4421`；没有第二个明文端口可退回。
- **默认加密**。TLS 1.3 由固件完成首段握手；内核与 initramfs 在同一端口以 `--tls` 重连。
- **认证与加密分层**。DH-HMAC-CHAP 回答“谁能连”，TLS 1.3 保护内容。
- **Host NQN**。每个 Worker 有独立 Host NQN，target 侧按 `allowed_hosts` 登记；严格模式认证不会接受 Unknown Host。
- **凭据跟盘/跟 Worker**。DHHC-1 认证密钥按 Worker 保存，多块盘共用一把；轮换/吊销对同一 Worker 的全部盘生效。

### TLS PSK

TLS 预共享密钥不是独立存储字段，而是认证密钥的纯函数：

```text
PSK = HMAC-SHA256(raw_dhhc1_key, "kurrent-nvme-tls-psk-v1")
```

因此轮换或吊销认证密钥会原子地轮换或吊销 PSK。存储节点侧由 nvmet-host 注入内核全局 `.nvme` keyring；设备侧随 `/boot-vars` 下发 `nbft-tls-psk` 与 `nbft-tls-port`。

`/boot-vars` 只有在以下条件同时满足时才下发 PSK：

- Worker 已绑定；
- 密钥库有条目；
- 后端为 nvmet；
- 存储节点数据面端口就绪。

任一条件不满足时不投影 PSK；nvmet 后端缺少密钥或端口未就绪时，整个引导变量不下发，控制面拒绝引导并记审计。

## 凭据生命周期

### 签发

nvmet 后端建盘时，控制面检查该 Worker 是否已有认证密钥；没有则生成 DHHC-1 并推送。已有密钥不因新建盘而轮换。

### 推送与对账

控制面把密钥推送到持有该 Worker 盘的所有 Agent；Agent 转调 nvmet-host，写入 configfs 的 `dhchap_key`、`allowed_hosts` 与内核 keyring。

推送结果写入状态台账：

- 已送达：target 侧已应用；
- 部分生效：例如节点 TLS 未就绪；
- 未送达：Agent 不可达或应用失败，由后台对账周期重试。

对账周期由 `spec.credentials.reconcileIntervalSeconds` 控制。轮换后的失败不会造成“引导变量使用新钥、configfs 仍是旧钥”的失配，因为未送达项会被持续重放。

### 轮换与吊销

```bash
kurrent workers credential set worker-01 --generate
kurrent workers credential revoke worker-01
```

- 轮换就地生成新密钥，不打印明文；成功后旧密钥与旧 PSK 同时失效。
- 吊销后下一次 `/boot-vars` 拒绝下发，设备无法连接。
- 密钥在 target 侧的清除有代际号保护，避免并发推送把旧密钥写回。
- 删除盘、删除子系统或删除 Worker 时，关联的 target、key 与推送台账一并清理；孤儿条目有后台清理。

### 静态明文边界

密钥明文只出现在链路必要范围：

- 控制面 `state/credentials.yml`；
- Agent 缓存（含派生 PSK）；
- 存储节点 configfs `dhchap_key`；
- 存储节点内核 `.nvme` keyring。

这些文件/接口不引入应用层加密，由部署方负责磁盘加密、宿主 root 边界与备份加密。接口与日志不打印密钥。

## 数据面就绪

存储节点启动时创建 `4421` 监听并上报能力。控制面以 Agent 上报的实时能力为准，而不是声明值：

- 端口未就绪时，`/boot-vars` 拒绝下发凭据；
- 内核缺少 NVMe/TCP TLS 配置、`tlshd < 1.5.0` 或服务未运行时，nvmet-host 上报端口未就绪；
- 数据面失败不会回落明文，也不会偷偷改用另一个端口。

宿主前置与诊断见[部署](../guide/deployment.md)。

## 常见失败

| 现象 | 方向 |
| --- | --- |
| 建盘返回无可用候选 | 母盘不存在于候选节点；节点被排空；数据面未就绪 |
| 克隆失败 | 母盘为 512 字节扇区 GPT；文件系统不支持 reflink；目标已存在 |
| 引导报 `Boot from SAN device ... failed` | 盘 GPT 布局与导出块大小不一致；检查 `/masters` 的 `gpt` |
| 认证失败 | Host NQN 不匹配；密钥未送达；target 未登记 allowed host |
| TLS 握手失败 | PSK 未进入 keyring；`tlshd` 版本或服务状态；内核 TLS 配置 |
| 轮换后仍能旧钥连接 | 推送未完成或节点未应用；查看凭据推送状态与 Agent 日志 |

## 相关页面

- 对象与放置：[设备、Worker 与系统盘](./entities.md)
- 启动时的凭据投影：[引导链](./boot-chain.md)
- 节点部署与宿主前置：[部署](../guide/deployment.md)
- 凭据运维：[日常运维](../guide/operations.md)
