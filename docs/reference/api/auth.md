---
title: "认证与授权"
---

# 认证与授权

控制面的认证链、主体类、授权规则与审计告警如下。管理面只经 443 入口；引导链端点与公开 CRL 不需要认证。

## 鉴权（认证器链）

管理面访问统一经 nginx 443 入口（唯一管理面入口）；控制面管理面只监听 unix socket，不暴露 TCP。

除 `GET /healthz`、公开 CRL（`/crl/nodes.crl`）与 iPXE 引导链端点（`/boot-vars`、`/devices/report`、`/devices/challenge`）外，管理面接口要求认证。三种凭据**串行短试**，首个成功即止；凭据无效即 `401`，不降级：

| 凭据 | 载体 | 说明 |
|---|---|---|
| mTLS 客户端证书 | 443 双向 TLS | `kurrent init` 签发并安装的 admin 证书（CN=admin、无 O）；CLI 按 `--credentials` ＞ `KURRENT_CREDENTIALS` ＞ 部署记录指向的 `<state>/control-plane/state/admin.conf` ＞ 旧布局发现 ＞ `~/.kurrent/admin.conf` 的顺序取用（`kurrent init` 通过一次性交换安装该快照，并在安装后用 mTLS 自检）。nginx 校验证书链（users CA + nodes CA 信任包）后，仅在 `SUCCESS` 时注入 DN / issuer / serial / fingerprint / class，控制面按 CN 查 `state/users.yml` 得组；其余反代 location 的身份头默认清空 |
| 管理 token | `Authorization: Bearer <token>` | `kurrent token create --usage admin` 签发（台账只存 sha256 摘要）；scope 为 `admin` / `readonly` / `enroll`，TTL 默认 24 小时、上限 30 天；供脚本调用与 WebUI 登录 |
| 会话 Cookie | `__Host-kurrent_session` | 以证书或管理 token 经 `POST /session` 换取；Cookie 只携带 `{jti, exp, epoch}`，身份与组以服务端 `state/sessions.yml` 为准（12 小时 TTL，剩余不足一半才滑动续期）；删登记即下一请求失效 |

**主体类（class）**：

- `human`：users CA 签发的客户端证书。
- `component`：nodes CA 签发的 agent / nvmet-host 证书。
- `automation`：Bearer 管理 token。
- `anonymous`：无凭据。

nginx 按证书 issuer 判定 human / component 并透传 `X-Client-Class`。控制面路由声明各自接受的主体类，**默认拒绝**；跨类请求返回 `403`（节点证书不能访问管理面，人类证书不能调用 `/enroll/renew`）。

授权（RBAC 最小版）：

- 读请求（GET）只需**已认证**。
- 写请求（POST/PUT/PATCH/DELETE）需 admin 组，否则 `403`。
- 唯一例外是 scope 含 `enroll` 的自动化 token：仅放行 `POST /pki/tokens` 签发 bootstrap token，且不得签发 admin token。读接口同样 `403`（scope 语义是「仅签发」，不能借读接口枚举设备/Worker/凭据与会话），也不能用该 token 换浏览器会话。
- Cookie 认证的写请求还要求 `Origin` 与 `Host` 同源。

端点主体类：引导链端点（`/boot-vars`、`/devices/report`、`/devices/challenge`）为 `anonymous`；`POST /enroll` 接受 `anonymous`（Bearer 引导 token）与 `automation`；`POST /enroll/renew` 仅接受 `component`。

**审计**：每请求写一条 `state/audit.jsonl`（`event` / `principal_class` / `principal` / `issuer` / `serial` / `fingerprint_sha256` / `route` / `decision` / `reason` / `request_id` / `client_ip`），响应头返回 `X-Request-Id`；不记录 secret、token 明文、私钥与 PSK。审计写入失败时请求返回 `503`（fail-closed）。

告警以 `event=alarm` 事件写入同一文件，覆盖：

- 引导凭据 `injected && !verified`；
- 引导端点限流触发；
- component/control 进入管理路由；
- 未知 issuer / CRL 命中（CRL 由 nginx 在握手层直接拒绝，应用侧告警覆盖到达应用层的未知 issuer 证据）；
- 同源认证失败风暴；
- bootstrap 身份/CN 不符；
- 用户/管理员证书签发 / CRL 更新 / CRL 重签失败 / CA 生成；
- `users.yml` 变更以及审计写入失败。

**告警外投（可选）**：设置 `KURRENT_CP_ALARM_WEBHOOK=<url>` 后，每条 `event=alarm` 审计事件会以 JSON POST 到该地址（字段与审计事件相同，另含 `alarm` / `severity` / `detail`；不含任何密钥或明文）。

投递走后台线程与有界队列（200 条），失败重试 2 次（间隔 1s / 4s，单次超时 5s）；队列满时丢弃并记 ERROR 日志。外投不进入请求路径，不影响 `503` fail-closed 语义。未设置该变量时不投递。

开发模式：`KURRENT_CP_INSECURE=1` 时放行全部请求（身份 insecure/admin），仅限本地开发；生产严禁。不存在“未配置凭据即放行”的空放行路径。

示例：

```bash
# 本机（控制面服务器）：CLI 自动发现 admin.conf，经 443 mTLS 认证
kurrent agents list

# 脚本：管理 token（kurrent token create --usage admin）
export BASE_URL=https://<host>/api/cp
export TOKEN=<admin-token>
```

带鉴权的 curl 写法：

```bash
curl -s "$BASE_URL/workers" \
  -H "Authorization: Bearer $TOKEN"
```

---
