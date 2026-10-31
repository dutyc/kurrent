# API 参考

控制面的全部能力通过 REST API 暴露。WebUI、CLI 与第三方自动化都是同一 API 的客户端，不绕过控制面直接操作 Agent 或数据面。

完整端点、请求字段、响应结构与可执行 `curl` 示例以仓库中的双语文档为准：

- 中文：`api/control-plane-api.zh-CN.md`
- English：`api/control-plane-api.en.md`

## 入口

| 项 | 值 |
| --- | --- |
| Base URL | `https://<host>/api/cp` |
| 外部入口 | nginx 443；80 只做 301 跳转 |
| 内部通信 | nginx 经共享卷 unix socket 转发，控制面不监听管理 TCP 端口 |
| 证书 | TOFU 自签；可用 `--cacert control_plane/state/certs/server.crt` 固定，或 `-k` 跳过校验 |
| OpenAPI | 默认关闭；本地需要时设 `KURRENT_CP_OPENAPI=1` |

## 认证

除 `/healthz` 与引导链端点外，管理端点要求认证。三种凭据按顺序尝试，首个成功即短路；无效凭据返回 401，不再继续尝试。

| 凭据 | 载体 | 说明 |
| --- | --- | --- |
| mTLS 客户端证书 | 443 双向 TLS | 初始化签发的管理员证书；nginx 校验链并注入身份头 |
| 管理 token | `Authorization: Bearer <token>` | `kurrent token create --usage admin` 签发；只存摘要 |
| 会话 Cookie | `__Host-kurrent_session` | 用证书或 token 调 `POST /session` 换取；身份来自服务端会话台账 |

主体类：

| 类 | 来源 | 能力 |
| --- | --- | --- |
| `human` | users CA 签发的人员证书 | 管理与查询 |
| `component` | nodes CA 签发的 agent / nvmet-host 证书 | enroll / renew 与组件端点 |
| `automation` | 管理 token | 按 scope 收窄 |
| `anonymous` | 无凭据 | 仅引导链与健康检查 |

授权规则：

- 读请求（GET）只要求认证；
- 写请求（POST / PUT / PATCH / DELETE）要求 admin 组；
- 跨主体类默认拒绝，返回 403；
- 自动化 token 的 `enroll` scope 只能签发 bootstrap token，不能读台账，也不能换会话；
- Cookie 认证的写请求要求 `Origin` 与 `Host` 匹配；
- 引导链端点匿名，但受绑定关系、设备验签与来源 IP 限速约束。

## 端点分组

| 分组 | 代表端点 | 用途 |
| --- | --- | --- |
| 健康与声明 | `GET /healthz`、`GET /config` | 进程存活、启动派生自检、生效声明与 configHash |
| 引导链 | `GET /boot-assets`、`GET /boot-vars`、`GET /devices/report`、`GET /devices/challenge`、`POST /devices/rotate` | 固件引导变量、设备上报、挑战应答、密钥轮换 |
| 管理身份 | `POST/DELETE /session`、`GET /sessions`、`GET /whoami` | 会话登录、登出、台账与当前身份 |
| 设备 | `GET /devices`、`POST /devices`、`DELETE /devices/{mac}`、`POST/DELETE /devices/{mac}/bind` | 设备池、登记、吊销、绑定与解绑 |
| Worker | `POST /workers`、`GET /workers`、`GET /workers/{id}/status`、`DELETE /workers/{id}` | Worker 台账、状态、批量删除 |
| 系统盘 | `POST /workers/{id}/luns/disk`、`POST /workers/luns/disk/batch`、`DELETE /workers/{id}/luns/disk/{os_tag}`、`PUT /workers/{id}/default-disk` | 建盘、批量建盘、删盘、默认启动盘 |
| 凭据 | `GET/PUT/DELETE /workers/{id}/credential` | NVMe-oF 认证密钥查看、轮换、吊销 |
| Agent | `GET /agents`、`POST /agents`、`POST /agents/probe`、`PUT /agents/{id}`、`DELETE /agents/{id}` | 存储节点台账与探测 |
| LUN 与母盘 | `GET /agents/{id}/luns`、`GET /masters`、`POST /agents/{id}/luns/disk`、`PUT /agents/{id}/masters/{name}/tag` | 母盘标签、LUN 创建与删除 |
| PKI | `POST /pki/tokens`、`GET /pki/tokens`、`GET /pki/users`、`GET /pki/components`、`GET /pki/control`、`GET /enroll/crl` | token、证书台账、CRL 分发 |
| 设置 | `GET/POST/DELETE /settings/registration-window`、`GET/PUT /settings/enforcement` | 注册窗口与验签强制开关 |
| 运维 | `GET /operations` | 操作审计日志 |

完整参数与响应字段见仓库 API 文档。

## 错误语义

| 状态码 | 含义 |
| --- | --- |
| `400` | 请求参数或业务校验失败 |
| `401` | 缺少凭据、凭据无效或 token 已失效 |
| `403` | 主体类或权限不足；跨类访问 |
| `404` | 对象不存在 |
| `409` | 状态冲突：已存在、已绑定、节点排空等 |
| `429` | 触发限速，响应带 `Retry-After` |
| `503` | 审计写入失败、Agent 不可达或启动派生自检失败 |

响应体通常为 `{"detail": "..."}`。每个响应带 `X-Request-Id`；审计记录包含同一 `request_id`。

审计是 fail-closed 的：请求的审计事件写入失败时，请求本身返回 503。审计不记录密钥、token 明文、私钥或 PSK。

限速按来源 IP 的滑动窗口执行：

| 端点 | 默认阈值 |
| --- | --- |
| `/boot-vars` | 240 次/分钟 |
| `/devices/report` | 120 次/分钟 |
| `/devices/challenge` | 120 次/分钟 |
| `/devices/rotate` | 10 次/分钟 |

阈值可由 `KURRENT_CP_RATE_LIMIT_*` 环境变量调整，`<= 0` 关闭。限速用于抑制扫描，不是身份边界。

## 引导链端点

| 端点 | 认证 | 关键约束 |
| --- | --- | --- |
| `/boot-vars` | 匿名 | 绑定校验 + 可选设备验签；未验签不下发密钥；无默认盘返回 reboot |
| `/devices/report` | 匿名 | 注册只在窗口期、额度内且带有效公钥；吊销设备不复活 |
| `/devices/challenge` | 匿名 | 未登记、未认领或已吊销返回 404；nonce 一次性、绑定 MAC |
| `/devices/rotate` | 匿名 | 旧密钥签名 `rotate‖mac‖new_pubkey‖nonce`；nonce 一次性 |

字段级契约见[固件契约](./firmware-contract.md)。

## 状态与配置文件

| 文件 | 含义 |
| --- | --- |
| `config/agents.yml` | Agent 台账与调度角色 |
| `state/devices.yml` | 设备台账；绑定权威 |
| `state/workers.yml` | Worker 台账与系统盘 |
| `state/credentials.yml` | NVMe-oF 密钥库 |
| `state/credential-push.yml` | 凭据推送状态 |
| `state/masters.yml` | 母盘标签 |
| `state/settings.json` | 运行时设置 |
| `state/users.yml` | 管理面用户与组 |
| `state/sessions.yml` | 服务端会话台账 |
| `state/audit.jsonl` | 认证、授权、凭据审计与告警 |
| `state/pki/` | 三套根 CA、签发台账与 CRL |
| `state/operations.jsonl` | 操作审计 |
| `dnsmasq/dhcp-hosts.conf` | MAC → hostname 投影；设备台账才是权威 |

`dhcp-hosts.conf` 原地截断写入以保持 inode；它只是绑定操作的派生投影，不参与启动身份识别。

## 相关页面

- [固件契约](./firmware-contract.md)
- [身份与访问](../concepts/identity.md)
- [声明式配置](../concepts/configuration.md)
- [安全模型](../architecture/security.md)
- [CLI 参考](./cli.md)
