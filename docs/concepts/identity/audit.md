# 审计与告警

**审计**记录管理面的每一次操作，用于事后回答谁在什么时候做了什么。每个管理请求写一条 `state/audit.jsonl`，包含主体类、主体、issuer、serial、fingerprint、路由、决策、原因与 request_id，不记录 secret 与 token 明文。

写入失败时请求返回 503（fail-closed）：审计未写入，请求不放行。

## 告警事件

八类事件写为 `event=alarm`，供管理员与外部系统关注：

- 引导凭据注入但 TPM 证明未通过；
- 组件或 control 主体进入管理路由；
- 未知 issuer / CRL 命中；
- 同源认证失败风暴；
- bootstrap 身份或 CN 不符；
- 用户/管理员证书签发、CRL 更新、CA 生成；
- `users.yml` 变更；
- 审计写入失败。

## 外投

配置 `KURRENT_CP_ALARM_WEBHOOK` 后，告警事件以 JSON POST 外投；投递在后台队列中完成，不进入请求路径。

## 接下来

- 返回[身份与访问](../identity.md)
- 威胁模型中的审计约束：[安全模型](../../concepts/architecture/security.md)
- 告警排查：[故障排查](../../tasks/troubleshooting.md)