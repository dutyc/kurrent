# 数据面与就绪

设备与存储节点之间只有一条数据面路径，控制面不参与块读写。凭据发出去之前，必须先确认存储节点具备接受这条连接的能力——否则设备拿到变量也无法连接。因此就绪状态以存储节点上报的实时能力为准，声明值不作依据：端口没准备好，控制面就不下发凭据。

## 单端口与分层

- **单端口**。固件段和内核段共用 `4421`；没有第二个明文端口可退回。
- **默认加密**。TLS 1.3 由固件完成首段握手；内核与 initramfs 在同一端口以 `--tls` 重连。
- **认证与加密分层**。DH-HMAC-CHAP 回答「谁能连」，TLS 1.3 保护内容。
- **Host NQN**。每个 Worker 有独立 Host NQN，target 侧按 `allowed_hosts` 登记；严格模式认证不会接受 Unknown Host。
- **凭据按 Worker 归属**。DHHC-1 认证密钥按 Worker 保存，同一 Worker 的多块盘共用一把。

## TLS PSK 的下发条件

`/boot-vars` 只有在以下条件同时满足时才下发 PSK：

- Worker 已绑定；
- 密钥库有条目；
- 后端为 nvmet；
- 存储节点数据面端口就绪。

任一条件不满足时不投影 PSK；nvmet 后端缺少密钥或端口未就绪时，整个引导变量不下发，控制面拒绝引导并记审计。PSK 的派生方式见[凭据与 TLS PSK](./credentials.md)。

## 就绪判定

存储节点启动时创建 `4421` 监听并上报能力。控制面以 Agent 上报的实时能力为准：

- 端口未就绪时，`/boot-vars` 拒绝下发凭据；
- 内核缺少 NVMe/TCP TLS 配置、`tlshd < 1.5.0` 或服务未运行时，nvmet-host 上报端口未就绪；
- 数据面失败不会回落明文，也不会改用另一个端口。

## 常见失败

| 现象 | 方向 |
| --- | --- |
| 引导报 `Boot from SAN device ... failed` | 盘 GPT 布局与导出块大小不一致；检查 `/masters` 的 `gpt` |
| 认证失败 | Host NQN 不匹配；密钥未送达；target 未登记 allowed host |
| TLS 握手失败 | PSK 未进入 keyring；`tlshd` 版本或服务状态；内核 TLS 配置 |
| 轮换后仍能旧钥连接 | 推送未完成或节点未应用；查看凭据推送状态与 Agent 日志 |

## 相关页面

- 返回[存储与数据面](../storage.md)
- 准入与变量投影：[数据面](../boot-chain/data-plane.md)
- 宿主前置与诊断：[部署](../../guide/deployment.md)