# 组件身份

存储节点的 Agent 与 nvmet-host 使用 nodes CA 签发的证书。组件身份与设备身份、人员身份互不替代：组件证书只能调用组件端点，不能访问管理面。

## 首次加入

组件第一次接入时还没有证书，需要一个一次性的引导凭据来换取证书：

1. 管理员签发绑定 `agent_id` 的 bootstrap token。`agent_id` 为 12 位小写十六进制，由控制面在签发时分配；join 把它写入节点状态根下的 `storager/state/agent.id`，CLI 与 WebUI 只消费响应返回的身份，昵称属于控制面台账。
2. 节点用 token 调 `/enroll`，取得组件证书。
3. token 默认一次性，TTL 与次数上限由声明控制。
4. nvmet-host 的凭据由 agent 在 enroll 响应中派生，无需单独签发。

## 运行态

- agent 在证书到期前按阈值 renew（续期）；
- 组件周期拉取 nodes CRL（证书吊销列表），缺失或过期时 fail-closed，即拒绝服务而不放行未校验的连接；
- 控制面在每次出站连接时检查对端序列号；
- 组件被吊销后，下一次握手或应用层校验即拒绝，并删除该节点的 bootstrap token。

## 恢复

被吊销的节点恢复：签发新 bootstrap token，重跑 `kurrent join`。agent 启动自检会从公开 CRL 发现本地证书已失效，用新 token 重新 enroll。

## 相关页面

- 返回[身份与访问](../identity.md)
- 人员证书与三类 CA：[人员与证书](./people.md)
- 节点加入步骤：[部署](../../guide/deployment.md)