# 身份与访问

Kurrent 的身份体系分两条正交的链：

- **管理面身份**：人、自动化脚本与组件如何证明自己，决定谁能调用管理接口。
- **设备身份**：引导链上的机器如何证明自己，决定谁可以取得某次引导的凭据。

两条链使用不同的信任根，不能互相替代。设备持有有效密钥不代表可以访问管理面；管理员持有证书也不代表可以冒充设备。

## 设备身份

### 信任根

设备身份是 TPM 2.0 的证明密钥（AK）。固件在首次启动时于本机 TPM 内创建受限签名密钥（ECC P-256 或 RSA-2048），私钥不可导出；后续启动复用同一 AK，已存在的 AK 不会被重建。控制面只记录 AK 公钥（SPKI DER）、AK name 与密钥类型。

固件提供三项能力：

| 能力 | 作用 |
| --- | --- |
| `akgen` | 在 TPM 内创建或复用 AK（受限签名，ECC P-256 或 RSA-2048）；已存在时不覆盖 |
| `akpub` | 导出 AK 公钥（SPKI DER）、TPM2B_NAME 与算法类型，供注册上报 |
| `quote <nonce>` | 读取 PCR 0/2/4/7（SHA-256）并在同一次 TPM 调用内生成 quote；`extraData` 为 nonce 字符串的 ASCII 字节；输出 TPMS_ATTEST 原始字节（不含 TPM2B 长度前缀）与 AK 签名 |

### 登记

设备向 `/devices/report` 上报指纹、AK 公钥/name/type 与 Secure Boot 状态。控制面按 MAC 登记，但**登记不产生权限**：

- 只有注册窗口开启、放行额度未用尽、携带有效 AK 且 `sb=1` 的未知设备才入池；
- 已入池但未绑定的设备没有引导凭据；
- 窗口关闭后不再接受新设备；
- 无 TPM 或 Secure Boot 未开启的设备不入池。

### 挑战-证明

`/devices/challenge` 下发一次性 nonce，设备用 TPM AK 对 PCR 0/2/4/7 生成 quote，`extraData` 等于 nonce 字符串的 ASCII 字节。`/boot-vars` 依次校验 quote 结构、AK 签名、nonce 绑定与一次性消费，再比对 PCR 摘要与平台基线。

平台基线在首次证明通过时记录（TOFU）；之后摘要变化按 `pcr_mismatch` 拒绝，并保留待批摘要，管理员核对后通过 `POST /devices/{mac}/approve-baseline` 批准。nonce 一次性、绑定 MAC，相关端点按来源 IP 限速。

AK 不可在线轮换：设备恢复或更换主板走删除登记并在注册窗口内重新入池的管理员流程。

### 设备身份的边界

- 证明的是“请求方持有该设备的 AK，并能对指定 PCR 生成有效 quote”，不证明启动后的系统未被控制；运行期被控仍可在轮换窗口内取得当前 PSK。
- TOFU 只固定服务器证书，不保护首跳的 DHCP/TFTP。
- PCR 基线同样是 TOFU：首次记录时若设备已在攻击者控制下，基线会被污染；首次登记应核对装机环境。
- 固件行为（PCR 选择、AK 创建）需逐型号验证；AMD fTPM 与独立 TPM 的 PCR 行为不同。

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

1. 管理员签发绑定 `agent_id` 的 bootstrap token（`agent_id` 为 12 位小写十六进制，由控制面在签发时分配，join 写入节点状态根下的 `storager/state/agent.id`；CLI 与 WebUI 只消费响应返回的身份；昵称属于控制面台账）；
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

- 引导凭据注入但 TPM 证明未通过；
- 组件或 control 主体进入管理路由；
- 未知 issuer / CRL 命中；
- 同源认证失败风暴；
- bootstrap 身份或 CN 不符；
- 用户/管理员证书签发、CRL 更新、CA 生成；
- `users.yml` 变更；
- 审计写入失败。

配置 `KURRENT_CP_ALARM_WEBHOOK` 后，告警事件以 JSON POST 外投；投递在后台队列中完成，不进入请求路径。

## 凭据归属

- **设备身份**：TPM 内 AK 私钥（不可导出）；台账记录 AK 公钥与 PCR 基线；恢复走删登记后重新入池。
- **人员证书**：用户本地私钥 + users CA；CSR 重签 / 序列号吊销。
- **管理 token**：控制面台账（摘要）；TTL / 吊销。
- **会话**：控制面会话台账；登出 / 逐个吊销 / 全部下线。
- **组件证书**：Agent / nvmet-host 本地；renew / nodes CRL。
- **DHHC-1**：控制面密钥库按 Worker；轮换 / 吊销 / 对账。
- **TLS PSK**：派生值；随 DHHC-1 原子变化。

## 相关页面

- 引导链如何使用设备身份：[引导链](./boot-chain.md)
- 固件侧 AK、quote 与 TOFU：[固件契约](../reference/firmware-contract.md)
- 凭据与数据面：[存储与数据面](./storage.md)
- 威胁模型与残余风险：[安全模型](../architecture/security.md)
- 日常操作命令：[日常运维](../guide/operations.md)
