# 管理 token 与会话

客户端证书适合长期身份，但自动化脚本与浏览器需要更轻的凭据。因此除客户端证书外，管理面还接受 Bearer token 与 Cookie 会话。三种凭据由同一条认证器链处理，产出统一的 `Identity` 供授权判定。

| 凭据 | 载体 | 有效期与存储 |
| --- | --- | --- |
| mTLS 客户端证书 | 443 双向 TLS | 人员证书默认 365 天，上限由声明控制；私钥由用户本地保管 |
| 管理 token | `Authorization: Bearer` | 默认 24 小时，上限按声明；台账只存 sha256 摘要 |
| 会话 | `__Host-kurrent_session` Cookie | 默认 12 小时；Cookie 只含 `{jti, exp, epoch}`，身份以服务端台账为准 |

认证器链按证书 → token → 会话顺序串行尝试，首个成功即止；凭据无效返回 401，不降级。

## 管理 token

```bash
kurrent token create --usage admin [--scope admin,readonly,enroll] [--ttl 24h]
```

- scope 是权限上限：`enroll` 只能签发 bootstrap token，不能签发 admin token，也不能读取管理接口。
- `last_used_at` 节流回写，便于识别长期未用凭据。
- 吊销即删除台账条目，Bearer 校验无缓存。

台账只存 sha256 摘要，不存明文：即使台账泄露，也无法还原出可用的 token。

## 会话

浏览器或客户端用证书/token 换取会话后，后续请求只携带 Cookie：

- 服务端登记 `jti`、用户、组、证书指纹与 scope；
- 每次请求重查用户是否仍存在、组是否变更；
- 删除登记或递增 epoch 可立即失效；
- 写请求要求 `Origin` 与 `Host` 同源。

Cookie 本身不含身份信息，只含一个指向服务端登记的索引。身份以服务端台账为准，因此吊销不需要等 Cookie 过期。

## 相关页面

- 返回[身份与访问](../identity.md)
- 人员证书与三类 CA：[人员与证书](./people.md)
- 组件凭据：[组件身份](./components.md)