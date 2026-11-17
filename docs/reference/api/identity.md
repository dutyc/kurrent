---
title: "身份与 PKI"
---

# 身份与 PKI

管理面身份由三分根 CA 签发。一键加入、token、会话与证书吊销端点如下。

## 一键加入：bootstrap token 签发（绑定节点身份）+ enroll 自动登记

### 说明

控制面签发**绑定节点身份的 bootstrap token**，格式为 `<6位>.<16位>`。存储只保留 sha256 摘要，TTL 默认 **60 分钟**、上限 24 小时。签发时可指定 `agent_id`（只接受 12 位小写十六进制），省略则由控制面分配新身份；enroll 只接受同一身份。

token **默认一次性**：使用额度在签发前原子占用，签发失败退回。批量引导用 `max_uses`（`--uses N`，1..1000，且不超过 `spec.pki.bootstrapTokenMaxUses`，默认 100）显式放开次数，不使用无限复用。

每次签发都是新 token，明文不可恢复、不可再查。token **支持吊销**（`DELETE /pki/tokens/{token_id}`，删除即失效），未吊销的 token 到期自然失效。

节点身份是机器身份：只接受 12 位小写十六进制，由控制面在签发 bootstrap token 时分配。请求省略 `agent_id` 时生成新身份，显式指定则绑定已有身份用于恢复。实际身份写入响应与输出的 join 命令；`kurrent token create` 与 WebUI 只是调用该端点并消费返回的 `agent_id`，不自行生成。join 把它写入节点状态根下的 `storager/state/agent.id`，不属于节点声明。昵称归控制面台账。

`nvmet-host` 组件凭据**不在此签发**。agent enroll 上报 `backend=nvmet` 时，控制面派生绑定 agent_id 的凭据，随 enroll/renew 响应下发；agent 落盘节点状态根下的 `storager/bootstrap/nvmet-host.token` 供其引导。签发由能力上报驱动，签发 token 时不预知后端。

### POST /pki/tokens

请求体按用途分两种。

节点引导 token（默认 usage）：

```json
{"usage": "bootstrap", "agent_id": "<已有节点 id，可省略>", "component": "agent", "max_uses": 1}
```

- `agent_id` 省略时由控制面分配新身份
- `component` 取 `agent` | `nvmet-host`
- `max_uses` 1..1000，默认 1，且不超过 `spec.pki.bootstrapTokenMaxUses`

管理 token（认证器链 Bearer 用）：

```json
{"usage": "admin", "scopes": ["readonly"], "ttl_hours": 24}
```

- `scopes` 取 `admin` | `readonly` | `enroll`，省略 = `admin`
- `ttl_hours` 1 小时至 `spec.auth.tokenMaxTtlHours`（默认 720），默认 24

scope 是权限上限：`readonly` 只读；`enroll` 只能签发 bootstrap token，其余读写接口与换会话一律 `403`。`readonly` / `enroll` token 不得签发 admin token。

#### curl

```bash
# 节点引导 token（绑定节点身份）
curl -s -X POST "$BASE_URL/pki/tokens" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "3f2a9c1d8b7e"}'

# 管理 token（供脚本调用与 WebUI 登录；等同 admin 证书身份）
curl -s -X POST "$BASE_URL/pki/tokens" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"usage": "admin"}'

# 只读管理 token（24 小时）
curl -s -X POST "$BASE_URL/pki/tokens" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"usage": "admin", "scopes": ["readonly"], "ttl_hours": 24}'
```

#### 成功返回（201）

```json
{
  "token": "a1b2c3.0123456789abcdef",
  "expires_at": "2026-09-22T12:00:00Z",
  "usage": ["enroll"],
  "groups": [],
  "scopes": [],
  "uses": 0,
  "max_uses": 1,
  "agent_id": "3f2a9c1d8b7e",
  "component": "agent"
}
```

`token` 明文仅本次返回可见，存储只留 sha256 摘要。

- `usage=["enroll"]`：节点引导 token，绑定 `agent_id`、默认一次性、60 分钟
- `usage=["admin"]` + `scopes=["admin"]`：管理 token，认证器链 Bearer 用（`kurrent token create --usage admin` 同款）

#### 常见错误

| HTTP 状态码 | 常见原因 |
|---:|---|
| `400` | `agent_id` 字符集非法 / 额度或 TTL 越界 / scope 非法 |
| `401` | 缺少 Token 或 Token 错误 |

### GET /pki/tokens

列出**未过期** token 台账条目（可视化管理）：bootstrap 引导 token、管理 token、nvmet-host 派生 token 同表。

不返回明文（本就不可恢复）与 `token_secret_hash`。`created_at` 对旧条目为空串——该字段自签发时注入，历史条目没有。`scopes` / `uses` / `max_uses` / `last_used_at` 供审计与容量观察，`max_uses=0` 表示不限（旧条目与管理 token）。读请求只需已认证（RBAC 最小版）。

```bash
curl -s "$BASE_URL/pki/tokens" -H "Authorization: Bearer $TOKEN"
```

```json
[
  {
    "token_id": "a1b2c3",
    "usage": ["admin"],
    "groups": ["admin"],
    "scopes": ["admin"],
    "component": "",
    "agent_id": "",
    "name": "admin",
    "created_at": "2026-09-27T09:00:00+00:00",
    "expires_at": "2026-10-04T09:00:00+00:00",
    "last_used_at": "2026-09-27T10:12:00+00:00",
    "uses": 0,
    "max_uses": 0
  }
]
```

### DELETE /pki/tokens/{token_id}

吊销 token：删除台账条目，**删除即失效**。Bearer 校验每请求重读台账（无缓存），吊销后立即 401，且无法恢复。

「谁在何时吊销了哪个 token」由操作审计（`pki.token.revoke`）留痕。写请求需 admin 组。

```bash
curl -s -X DELETE "$BASE_URL/pki/tokens/a1b2c3" \
  -H "Authorization: Bearer $TOKEN"
```

成功返回 `{"revoked": "a1b2c3"}`；token 不存在返回 `404`。WebUI「凭据」页与 `kurrent token list|revoke` 为同款调用。

### enroll 自动登记（POST /enroll，nginx 入口 /api/cp/enroll）

agent 组件引导（`POST /enroll`，Bearer = 绑定该节点身份的 bootstrap token）时，若 `agents.yml` 无该 id，**自动创建条目**：role_disk=True、role_cd=False、enabled=True、tags=`("auto", "storage")`。旧 agent 不上报能力时仅 `("auto",)`。**后端类型不进 tags**，它由 `/boot-vars` 实时查 `/capabilities` 判定。

请求体 `base_url` 字段（agent 侧 `spec.agent.advertiseUrl`，控制面可达地址）写入台账。昵称不随引导写入，注册后由管理员在 WebUI 或通过「更新 Agent」设置。

响应同时下发两张证书（client / serving，nodes CA 签发，subject 只含 CN）与**两个信任锚**：`ca_nodes_crt`（校验同伴组件）与 `ca_control_crt`（校验控制面客户端）。

规则：

- token 校验：格式 / 登记存在 / 组件匹配 / 绑定匹配（`agent_id` 与 token 登记一致）/ 未过期；通过后**原子占用一次额度**，证书签发失败退回，默认一次性（`max_uses` 可提高）
- `/enroll/renew` 凭据 = 组件现有 client cert（nginx 该路径强制 mTLS，issuer 必须是 nodes CA → class=component；人类证书 `403`），校验 CN == `<前缀>-<agent_id>` 后重签
- 响应附带 `renew_threshold`（声明 `spec.pki.renewThreshold`，默认 0.2）：组件落盘为 `policy.json`，按证书剩余生命周期低于该比例时 renew；旧组件忽略该字段（保留内置默认）
- `nvmet-host` 组件**不自动登记**（与 agent 共享 agent_id，须 agent 组件先行在册；否则 400，容器重启重试；额度不消耗）
- `base_url` 非空时必须为 `http://` / `https://` 开头，否则 400（不自动登记）
- 已登记 agent 再次 enroll/renew 时**按节点声明刷新** `base_url` / `role.cd`（声明权威：节点文件说了算，节点搬家后 renew 即对齐，无需重装）；控制面属性 `enabled` / `storager_ip` / `tags` / `remark` **不碰**，节点未上报的字段保留现有值
- agent 上报 `backend=nvmet` → 响应附加 `nvmet_token`（派生 nvmet-host 凭据）；stgt/lio 或旧 agent 无此字段
- 响应附带 `nodes_crl` / `control_crl`（PEM）与 `next_update`：组件落盘后用于入站/出站的吊销校验；CRL 生成失败时省略该组字段（尽力而为），组件启动后经 `/enroll/crl` 补齐

### 一键加入命令（节点侧）

声明式工作流（配置即声明、CLI 即工具）：控制面签发 token 时输出**带控制面地址与节点 id 的 join 命令**，存储节点粘贴执行即获得地址与身份。

节点声明 `storager/kurrent.yaml` 缺失时由 join 按模板自动生成（地址同步进 `spec.controlPlane.url`），已存在则读入合并。身份写入节点状态根下的 `storager/state/agent.id`：首次必须提供 `--agent-id`，重跑须与状态文件一致。随后收敛启动 agent，幂等可重跑。仓库根以外执行用 `--dir` 指向仓库根或 `storager` 目录；分机部署用 `--cp-ca` 提供控制面信任锚。

```bash
# 控制面（签发绑定节点身份的 bootstrap token；--cp-url = 存储节点可达的控制面 HTTPS 入口，
# 缺省按 --server 推导；省略 --agent-id 时由控制面分配 12 位小写 hex 新身份，
# 显式指定则绑定已有身份用于恢复）：
kurrent token create --cp-url https://<cp-host>

# 存储节点（粘贴上一条输出的 join 命令；节点 id 由控制面分配并已包含在命令中；业务键可用 kurrent config print node-defaults 预置模板编辑）：
kurrent join https://<cp-host> --token a1b2c3.0123456789abcdef --agent-id <agent_id-from-response>
```

### GET /enroll/crl（组件 CRL 分发）

组件（agent / nvmet-host）用现有 client cert 取吊销表。入口 `/api/cp/enroll/crl` 强制 mTLS 且只放行 `class=component`（人类证书 `403`，无证书 `401`）：

```json
{
  "nodes_crl": "-----BEGIN X509 CRL-----\n...",
  "control_crl": "-----BEGIN X509 CRL-----\n...",
  "next_update": "2026-11-23T09:00:00+00:00"
}
```

- `nodes_crl` 由 nodes CA 签发：组件校验**同伴**（agent ↔ nvmet-host）
- `control_crl` 由 control CA 签发：组件校验**控制面客户端**（4840 入站）
- 控制面出站（agent_client）与 agent → nvmet-host 出站同样加载 nodes CRL，并在每次建连时校验新鲜度。CRL 缺失或已过 `nextUpdate` 时**拒绝连接**（fail-closed，非降级放行）
- 组件默认每 60 秒刷新一次（`KURRENT_COMPONENT_CRL_REFRESH_SECONDS`，`<=0` 关闭）。拉取失败保留上一份快照，过期后按上面的规则拒绝
- 吊销生效时间 = 控制面写 CRL 后的下一次组件刷新（默认 ≤60 秒），无需重启组件

公开只读副本（启动自检）：`GET /crl/nodes.crl` 由 nginx 直接返回 `state/pki/nginx/nodes.crl`，无认证、仅 `GET`/`HEAD`、`Cache-Control: no-store`。

被吊销的证书无法通过 `/enroll/crl` 的 mTLS（握手即被拒），所以组件启动时需要一个不要求客户端证书的入口：本地 `client.crt` / `serving.crt` 的序列号命中该表即进入重新引导。

组件用本地 `ca-nodes.crt` 校验签名、issuer 与 `nextUpdate` 后才采信。拉取或校验失败不改变原有链校验结果（自检是补充，不是替代）。`KURRENT_PKI_SELF_CHECK=0` 可关闭启动自检。

## 管理面会话与证书吊销

### 会话（服务端登记）

`POST /session` 用当前凭据（admin 证书 / 管理 token）换会话 Cookie。Cookie 只携带 `{jti, exp, epoch}`，身份与组以 `state/sessions.yml` 为准。

默认 TTL 12 小时，剩余不足一半才滑动续期。Cookie 写请求要求 `Origin` 与 `Host` 同源。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/sessions` | 列出未过期会话（管理员）；`session_id` = `sha256(jti)`，不是可用凭据 |
| `DELETE` | `/sessions/{session_id}` | 逐个吊销：删登记，下一请求 `401`；不存在 `404` |
| `DELETE` | `/sessions` | 全部下线：清空登记并递增 `epoch`，所有既有 Cookie 失效 |

`session_id` 含 64 位 hex，原样传给 DELETE（`kurrent sessions list` 输出完整值）。用户从 `state/users.yml` 删除时，其会话下一次请求即 `401` 并自动清理。

### 证书签发、列表与吊销（users CA + CRL）

`POST /pki/users/{cn}` 按 CSR 为 `state/users.yml` 中**已登记**的用户签发证书（管理员）。
私钥由申请者本地生成、**永不上送**；控制面只签 CSR。

- CN 必须先在 `state/users.yml` 中登记（证书只承载 CN，组来自该文件），未知用户 `400`
- `csr` 必填（PEM）：其 CN 必须等于路径里的 `cn`，且除 CN 外不得携带 subject 属性（自报 O/OU 一律拒绝）；缺失/非法 `400`
- `days` 可选：默认 365，上限 3650
- 响应只含 `certificate`（PEM），不含私钥；证书文件留在 `state/pki/users/<cn>/issued/<serial>.crt` 供审计；签发写告警事件 `user_cert_issued`
- 例外：`kurrent init` 自举的 admin 证书仍由控制面本地生成（本机便捷路径）

```bash
# 申请者本地：生成 key + CSR（私钥保持 0600，不上送）
kurrent cert keygen alice --out ./alice
# 管理员签发（CLI 读取 CSR 文件后提交）
kurrent cert issue alice --csr ./alice/alice.csr --out ./alice --days 365
```

请求体形如 `{"csr": "<PEM>", "days": 365}`；响应为证书元数据 + `certificate`（PEM）。

`GET /pki/users` 列出 users CA 签发的证书（含已吊销 / 已过期）：

```json
[
  {
    "cn": "admin",
    "groups": ["admin"],
    "serial": "6f1c0d2e",
    "fingerprint": "<sha256 hex>",
    "created_at": "2026-10-23T09:00:00+00:00",
    "expires_at": "2027-10-23T09:00:00+00:00",
    "revoked_at": "",
    "reason": "",
    "expired": false
  }
]
```

`groups` 来自 `state/users.yml`（证书只承载 CN；已从目录移除的用户返回空数组），供管理面展示权限组。实际授权仍在每次请求时按 `users.yml` 重新解析。

`DELETE /pki/users/{cn}?reason=key_compromise` 吊销某 CN 的证书。加 `&serial=<hex>` 只吊销指定序列号（同一个人可能同时持有新旧两张）：

- `reason` 取 `unspecified`（默认）| `key_compromise` | `superseded` | `cessation_of_operation`
- 标记 `state/pki/users/issued.yml`，重签 `state/pki/nginx/users.crl` 与 `clients.crl`（users + nodes 拼接），再 reload nginx。被吊销证书**下一次 TLS 握手**即失败
- 省略 `serial` = 吊销该用户全部未吊销证书并删除其全部会话；给出 `serial` 时只踢绑定该证书指纹的会话（返回 `sessions` 数量）
- 不能吊销当前请求正在使用的那张证书（按 `serial` 精确判断，`400`）；对自身 CN 省略 `serial` 同样拒绝。同名主体的其他序列号可显式按 `serial` 吊销（轮换后作废旧证书）
- 无未吊销证书返回 `404`；CRL 已写但 reload 失败返回 `500`（吊销尚未生效）
- 成功后写告警事件 `crl_updated`

CRL 由控制面每 12 小时重签一次并请求 nginx 重新加载（`CRL_REFRESH_HOURS`；测试/部署可用 `KURRENT_CP_CRL_REFRESH_INTERVAL` 覆盖秒数，`<=0` 关闭）。这保证 7 天的 `nextUpdate` 不会在长跑中过期——过期 CRL 会让 nginx 拒绝**全部**客户端证书。

控制面不接触容器运行时：重载请求写入共享卷（`/run/kurrent/nginx.reload`），nginx 容器内的 watcher 执行 `nginx -s reload` 后回写 `nginx.ack`，控制面据此确认（超时按 500 处理）。

```bash
curl -s -X DELETE "$BASE_URL/pki/users/alice?reason=key_compromise" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{ "revoked": "alice", "serials": ["6f1c0d2e"], "sessions": 1, "reason": "key_compromise" }
```

CLI 对应 `kurrent cert keygen <cn> [--out DIR]`、`kurrent cert issue <cn> --csr FILE`、`kurrent cert list [-o json]` 与 `kurrent cert revoke <cn> [--serial HEX] [--reason ...]`；WebUI「凭据」页展示 token 权限 / 最近使用与会话台账。

### 组件证书（nodes 域，agent / nvmet-host）

`GET /pki/components` 列出 nodes CA 签发的组件证书（`client` 与 `serving` 各一条）：

```json
[
  {
    "cn": "agent-23456789abcd",
    "agent_id": "23456789abcd",
    "kind": "client",
    "serial": "3f9c1b7e",
    "fingerprint": "9c1f...",
    "created_at": "2026-10-23T09:00:00+00:00",
    "expires_at": "2027-01-21T09:00:00+00:00",
    "revoked_at": "",
    "reason": "",
    "expired": false
  }
]
```

`DELETE /pki/components/{agent_id}?reason=key_compromise`（可加 `&serial=<hex>`）吊销
该节点的组件证书：

- 标记 `state/pki/components/issued.yml`，重签 `clients.crl` 的 nodes 部分，再 reload nginx。被吊销证书**下一次 mTLS 握手**即失败（控制面与同伴组件两侧都拒绝）
- 同时删除该节点的 bootstrap token（agent 与 nvmet-host 两路），断掉「吊销后重新 enroll 一张新证书」的路径。`/enroll/renew` 对已吊销序列号额外返回 `403`（CRL reload 窗口内的应用侧回退）
- 登记只在签发（enroll / renew）时写入：P4 之前签发、未登记的旧证书不可吊销，下一次 renew 入库后即可管理
- 无未吊销证书返回 `404`；`reason` / `serial` 非法 `400`；CRL 已写但 reload 失败 `500`

吊销后的恢复（不删除本地证书）：

1. 签发新的节点绑定 bootstrap token（WebUI「证书」→「存储节点」→「恢复」，或 `kurrent token create --agent-id <id>`）。吊销时旧 token 已被删除，不能复用
2. 在节点上执行 `kurrent join <cp-url> --token <token> --agent-id <id>`
3. agent 启动时自检本地 client / serving 序列号，命中吊销表即用新 token 重新 enroll 并覆盖本地证书（无手工删除步骤）。join 等待 agent 写出派生的 nvmet-host 凭据后自动重启 nvmet-host

```bash
curl -s -X DELETE "$BASE_URL/pki/components/node-a?reason=key_compromise" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{ "revoked": "node-a", "serials": ["3f9c1b7e", "7a2d4c11"], "kinds": ["client", "serving"], "reason": "key_compromise" }
```

CLI 对应 `kurrent cert components [-o json]` 与 `kurrent cert revoke-component <agent_id> [--serial HEX] [--reason ...]`。

### control 域证书（控制面客户端）

控制面调用 Agent（4840）时出示的客户端证书由 control CA 签发。`GET /pki/control` 列出该域证书（控制面启动时自举的一张会自动登记）：

```json
[
  {
    "cn": "control-plane",
    "serial": "1de29ec7",
    "fingerprint": "3a71...",
    "created_at": "2026-10-24T09:00:00+00:00",
    "expires_at": "2027-01-22T09:00:00+00:00",
    "revoked_at": "",
    "reason": "",
    "expired": false
  }
]
```

`DELETE /pki/control/{cn}?reason=key_compromise`（可加 `&serial=<hex>`）吊销：

- 标记 `state/pki/control/issued.yml`，重签 `state/pki/nginx/control.crl`
- 组件在下一次刷新（默认 ≤60 秒）后拒绝该序列号。CRL 的 `nextUpdate` 为 30 天，容忍控制面长时间不可达
- 不能吊销当前请求自身所用的证书（`400`）；无未吊销证书返回 `404`
- control 域证书不参与 nginx 管理面握手（`ca-bundle.pem` 只含 users + nodes），吊销只影响控制面 → Agent 的组件通道

```bash
curl -s -X DELETE "$BASE_URL/pki/control/control-plane?reason=key_compromise" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{ "revoked": "control-plane", "serials": ["1de29ec7"], "reason": "key_compromise" }
```

---
---
