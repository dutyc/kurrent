# 凭据与 TLS PSK

数据面的凭据按 Worker 归属：同一个 Worker 的多块盘共用一把 DHHC-1 认证密钥，轮换或吊销对全部盘同时生效。这样凭据跟着身份走，与「系统归属于 Worker」的模型一致。控制面只保存与推送密钥，块读写不经过它。

## 签发

nvmet 后端建盘时，控制面检查该 Worker 是否已有认证密钥；没有则生成 DHHC-1 并推送。已有密钥不因新建盘而轮换。

## 推送与对账

控制面把密钥推送到持有该 Worker 盘的所有 Agent；Agent 转调 nvmet-host，写入 configfs 的 `dhchap_key`、`allowed_hosts` 与内核 keyring。

推送可能失败——节点暂时不可达、TLS 尚未就绪——因此推送结果写入状态台账：

- 已送达：target 侧已应用；
- 部分生效：例如节点 TLS 未就绪；
- 未送达：Agent 不可达或应用失败，由后台对账周期重试。

对账周期由 `spec.credentials.reconcileIntervalSeconds` 控制。轮换后的失败不会造成「引导变量使用新钥、configfs 仍是旧钥」的失配，因为未送达项会被持续重放。

## 轮换与吊销

```bash
kurrent workers credential set worker-01 --generate
kurrent workers credential revoke worker-01
```

- 轮换就地生成新密钥，不打印明文；成功后旧密钥与旧 PSK 同时失效。
- 吊销后下一次 `/boot-vars` 拒绝下发，设备无法连接。
- 密钥在 target 侧的清除有代际号保护，避免并发推送把旧密钥写回。
- 删除盘、删除子系统或删除 Worker 时，关联的 target、key 与推送台账一并清理；孤儿条目有后台清理。

## TLS PSK

TLS 预共享密钥由认证密钥派生，不单独存储：

```text
PSK = HMAC-SHA256(raw_dhhc1_key, "kurrent-nvme-tls-psk-v1")
```

派生而非独立存储，保证了轮换或吊销认证密钥会原子地轮换或吊销 PSK，两把密钥永远同步。存储节点侧由 nvmet-host 注入内核全局 `.nvme` keyring；设备侧随 `/boot-vars` 下发 `nbft-tls-psk` 与 `nbft-tls-port`。

## 静态明文边界

密钥明文只出现在链路必要范围：

- 控制面 `state/credentials.yml`；
- Agent 缓存（含派生 PSK）；
- 存储节点 configfs `dhchap_key`；
- 存储节点内核 `.nvme` keyring。

这些位置保存的是密钥明文，磁盘加密、宿主 root 边界与备份加密由部署方负责。接口与日志不打印密钥。

## 相关页面

- 返回[存储与数据面](../storage.md)
- 凭据如何进入启动变量：[数据面](../boot-chain/data-plane.md)
- 端口就绪与握手失败：[数据面与就绪](./data-plane.md)
- 轮换与吊销操作：[日常运维](../../guide/operations.md)