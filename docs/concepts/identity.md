# 身份与访问

Kurrent 的身份体系分两条正交的链：

- **管理面身份**：人、自动化脚本与组件如何证明自己，决定谁能调用管理接口。
- **设备身份**：引导链上的机器如何证明自己，决定谁可以取得某次引导的凭据。

两条链使用不同的信任根，不能互相替代。设备持有有效密钥不代表可以访问管理面；管理员持有证书也不代表可以冒充设备。

## 设备身份

### 密钥

设备首次启动时由固件生成 ECDSA P-256 私钥，写入 EFI NVRAM，私钥不出机器。后续启动复用该密钥，拒绝覆盖。

固件提供三个命令：

| 命令 | 作用 |
| --- | --- |
| `keygen` | 生成设备密钥；已存在时拒绝覆盖 |
| `pubkey` | 导出公钥（未压缩点，hex） |
| `sign <data>` | 对数据做 SHA-256 + ECDSA 签名，输出 base64 DER |

### 登记

设备向 `/devices/report` 上报指纹与公钥。控制面按 MAC 登记，但**登记不产生权限**：

- 只有注册窗口开启且带有效公钥的未知设备才入池；
- 已入池但未绑定的设备没有引导凭据；
- 窗口关闭后不再接受新设备。

### 挑战-应答

`/devices/challenge` 下发一次性 nonce，设备用私钥签名 `nonce‖mac`。验签结果在 `/boot-vars` 准入时使用。nonce 一次性、绑定 MAC，相关端点按来源 IP 限速。

设备密钥支持轮换：旧密钥签名 `rotate‖mac‖new_pubkey‖nonce`，成功后旧密钥失效。

### 设备身份的边界

- 挑战-应答证明“请求方持有该设备私钥”，不证明“运行该私钥的代码可信”。
- TOFU 只固定服务器证书，不保护首跳的 DHCP/TFTP。
- 设备私钥在 NVRAM 中，启动后的系统若可读取固件变量，也可以使用它。当前主线未提供 TPM 或 Secure Boot 度量来关闭这一路径。

这些边界在[安全模型](../architecture/security.md)中展开。

## 管理面身份

### 唯一入口

管理面只经 HTTPS `443` 进入。控制面服务本身不监听 TCP，只监听容器内 unix socket；nginx 是唯一对端。`X-Client-Cert-*` 与 `X-Client-Class` 头只信 nginx 注入，其他反代位置默认清空。

### 三类 CA

| CA | 签发对象 | 用途 |
| --- | --- | --- |
| users | 人类用户与 admin | 管理面 mTLS；nginx 判定为 `human` |
| nodes | agent / nvmet-host | 组件 enroll / renew、组件间 mTLS；判定为 `component` |
| control | 控制面自身客户端 | 控制面出站到组件；不进 443 |

nginx 只信任 users + nodes 的信任包，按证书 issuer 判定主体类。控制面每个路由声明接受的主体类，**默认拒绝**：

- 节点证书不能访问管理面；
- 人类证书不能调用组件续期端点；
- control 域证书不进入 443。

### 三类凭据

| 凭据 | 载体 | 有效期与存储 |
| --- | --- | --- |
| mTLS 客户端证书 | 443 双向 TLS | 人员证书默认 365 天，上限由声明控制；私钥由用户本地保管 |
| 管理 token | `Authorization: Bearer` | 默认 24 小时，上限按声明；台账只存 sha256 摘要 |
| 会话 | `__Host-kurrent_session` Cookie | 默认 12 小时；Cookie 只含 `{jti, exp, epoch}`，身份以服务端台账为准 |

认证器链按证书 → token → 会话顺序串行尝试，首个成功即止；凭据无效返回 401，不降级。

### 人员证书

管理员或用户先在本机生成私钥与 CSR：

```bash
kurrent cert keygen alice --out ./certs
kurrent cert issue alice --csr ./certs/alice.csr
```

控制面只接收 CSR，不接触私钥。签发前要求该 CN 已在 `state/users.yml` 登记；组也来自该文件。

吊销可以精确到一张证书（序列号）或该用户的全部证书；重签 CRL 后 nginx reload，下一次 TLS 握手即拒绝。吊销同时删除该用户的相关会话。

### 管理 token

```bash
kurrent token create --usage admin [--scope admin,readonly,enroll] [--ttl 24h]
```

- scope 是权限上限：`enroll` 只能签发 bootstrap token，不能签发 admin token，也不能读取管理接口。
- `last_used_at` 节流回写，便于识别长期未用凭据。
- 吊销即删除台账条目，Bearer 校验无缓存。

### 会话

浏览器或客户端用证书/token 换取会话后，后续请求只携带 Cookie：

- 服务端登记 `jti`、用户、组、证书指纹与 scope；
- 每次请求重查用户是否仍存在、组是否变更；
- 删除登记或递增 epoch 可立即失效；
- 写请求要求 `Origin` 与 `Host` 同源。

### 授权

- 读请求只需已认证。
- 写请求需要 `admin` 组。
- `enroll` scope 只放行签发 bootstrap token。
- 会话写请求额外做同源校验。

## 组件身份

存储节点的 Agent 与 nvmet-host 使用 nodes CA 签发的证书。首次加入时：

1. 管理员签发绑定 `agent_id` 的 bootstrap token（`agent_id` 为 12 位小写十六进制；描述性名称写节点声明 `metadata.remark`）；
2. 节点用 token 调 `/enroll`，取得组件证书；
3. token 默认一次性，TTL 与次数上限由声明控制；
4. nvmet-host 的凭据由 agent 在 enroll 响应中派生，无需单独签发。

进入运行态后：

- agent 在证书到期前按阈值 renew；
- 组件周期拉取 nodes CRL，缺失或过期时 fail-closed；
- 控制面在每次出站连接时检查对端序列号；
- 组件被吊销后，下一次握手或应用层校验即拒绝，并删除该节点的 bootstrap token。

被吊销的节点恢复：签发新 bootstrap token，重跑 `kurrent join`。agent 启动自检会从公开 CRL 发现本地证书已失效，用新 token 重新 enroll。

## 审计与告警

每个管理请求写一条 `state/audit.jsonl`，包含主体类、主体、issuer、serial、fingerprint、路由、决策、原因与 request_id，不记录 secret 与 token 明文。

写入失败时请求返回 503（fail-closed）。八类事件写为 `event=alarm`：

- 引导凭据注入但未验签；
- 组件或 control 主体进入管理路由；
- 未知 issuer / CRL 命中；
- 同源认证失败风暴；
- bootstrap 身份或 CN 不符；
- 用户/管理员证书签发、CRL 更新、CA 生成；
- `users.yml` 变更；
- 审计写入失败。

配置 `KURRENT_CP_ALARM_WEBHOOK` 后，告警事件以 JSON POST 外投；投递在后台队列中完成，不进入请求路径。

## 凭据归属

- **设备私钥**：设备 NVRAM；`POST /devices/rotate`。
- **人员证书**：用户本地私钥 + users CA；CSR 重签 / 序列号吊销。
- **管理 token**：控制面台账（摘要）；TTL / 吊销。
- **会话**：控制面会话台账；登出 / 逐个吊销 / 全部下线。
- **组件证书**：Agent / nvmet-host 本地；renew / nodes CRL。
- **DHHC-1**：控制面密钥库按 Worker；轮换 / 吊销 / 对账。
- **TLS PSK**：派生值；随 DHHC-1 原子变化。

## 相关页面

- 引导链如何使用设备身份：[引导链](./boot-chain.md)
- 固件侧密钥、签名与 TOFU：[设备身份与信任](../firmware/index.md)
- 凭据与数据面：[存储与数据面](./storage.md)
- 威胁模型与残余风险：[安全模型](../architecture/security.md)
- 日常操作命令：[日常运维](../guide/operations.md)
