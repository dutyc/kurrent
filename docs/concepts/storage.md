# 存储与数据面

**系统盘**集中存放在**存储节点**上，设备每次启动通过网络连接并挂载。存储节点承载**母盘**与系统盘，并把块设备导出给设备；控制面只负责编排与签发凭据，块读写不经过控制面。系统盘位置固定，设备因此可以替换。

存储节点上有三个组件，各管一层：

| 组件 | 位置 | 职责 |
| --- | --- | --- |
| Storager Agent | 存储节点 `:4840`（mTLS） | 盘文件生命周期、母盘扫描、克隆、删除；向本机后端下发指令 |
| nvmet-host | 共享卷 UDS（mTLS） | 操作内核 configfs：subsystem / namespace / port / allowed_hosts / DHHC-1；注入 TLS PSK |
| 宿主内核 nvmet | 存储节点宿主 | NVMe/TCP target，数据面端口 `4421` |

mTLS 即双向 TLS：通信双方都出示证书，互相验证。UDS 是 unix domain socket，只在本机进程间通信，不经过网络。configfs 是内核提供的配置接口，nvmet-host 通过它告诉内核要导出哪块盘、允许谁来连。

![存储路径：从母盘克隆系统盘，经 nvmet-host 导出到设备数据面](/diagrams/zh/storage-data-plane.svg)

Agent 与 nvmet-host 分开：两者权限需求不同，Agent 管理盘文件，nvmet-host 操作内核导出配置。后端以 nvmet 为主。iSCSI（stgt / lio）代码保留为兼容后端，但主分支没有存活的引导路径，历史实现见 `iscsi-classic` 分支。后端差异封装在 Agent 内部，控制面只发 HTTP。

## 母盘

母盘是系统盘的克隆源，不参与启动。它放在存储节点声明 `spec.agent.diskDir` 下，文件名包含 `_tpl_` 标记。

母盘必须是 4K 原生 GPT，且所在文件系统支持 reflink（一种文件系统级的写时复制克隆，克隆瞬间完成、不复制数据）；否则克隆会失败或退化为全量复制。准备与放置见[母盘](./storage/master-images.md)。

## 凭据与 TLS PSK

DHHC-1 认证密钥按 Worker 保存，建盘时签发。TLS PSK（预共享密钥）由认证密钥派生，不单独存储。轮换或吊销认证密钥会同时轮换或吊销 PSK，两把密钥不会出现一边新一边旧的失配。

推送结果写入台账，失败项由后台对账重试。派生、推送和轮换见[凭据与 TLS PSK](./storage/credentials.md)。

## 数据面

设备与存储节点之间只有一条数据面路径：固件段与内核段共用 `4421`。

- DH-HMAC-CHAP 认证谁能连。
- TLS 1.3 保护传输内容。
- 没有明文回退端口。

端口未就绪或密钥缺失时，`/boot-vars` 拒绝下发。端口与就绪条件见[数据面与就绪](./storage/data-plane.md)。

## 接下来

- 系统盘对象与默认盘：[系统盘](./entities/system-disk.md)
- 启动时的凭据投影：[引导链](./boot-chain.md)
- 节点部署与宿主前置：[部署](../setup/deployment.md)
- 凭据运维：[日常运维](../tasks/operations.md)