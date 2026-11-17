---
title: "基础与状态"
---

# 基础与状态

调用 API 所需的基础信息、运行环境、状态文件与健康检查端点如下。

## Base URL

管理面入口（唯一）：`https://<host>/api/cp`（nginx 443；80 仅 301 升级）。引导链端点另经 4443 提供，见下文。

控制面管理面不监听任何 TCP 端口：nginx 经共享卷中的 unix socket（`/run/kurrent/cp.sock`）
转发，容器健康检查走同一 socket。本机调试请经 443 入口，或进入控制面容器访问该 socket。

> 入口使用自签服务器证书（状态根下 `control-plane/state/certs/server.crt`，控制面启动时自动生成；
> 状态根默认 `~/.local/state/kurrent`，`kurrent deployment show` 可查），curl 需加 `-k`，
> 或用 `--cacert <state>/control-plane/state/certs/server.crt` 固定信任该证书本体。未签名调试固件以
> TOFU 固定该证书指纹；签名固件走 4443，只信任构建时嵌入的 boot CA。

如果你通过别的端口或域名暴露，请替换为实际地址。

## 环境变量

控制面不解析 `.env` 文件，全部通过容器环境变量（`os.getenv(...)`）读取。仓库根的
`docker-compose.yml` 注入 `TZ`、`KURRENT_HTTPS_PORT` 与 `KURRENT_BOOT_HTTPS_PORT`；按需追加的开关（例如开发模式
`KURRENT_CP_INSECURE=1`，仅限本地、生产严禁）直接加到该服务的 `environment:` 下，
或用 `docker compose --env-file` 传入。

## 状态与配置文件

控制面的状态与配置分工如下（容器内路径；宿主对应状态根下的 `control-plane/{state,config,dnsmasq,bootstrap}/`，状态根默认 `~/.local/state/kurrent`，`KURRENT_STATE_DIR` 可覆盖）：

| 文件 | 含义 |
|---|---|
| `config/agents.yml` | Agent 节点清单与调度角色 |
| `state/devices.yml` | 设备台账（设备池 / 绑定 / 指纹 / TPM AK 与 PCR 基线）——**绑定关系唯一权威**（`bound_worker_id`） |
| `state/workers.yml` | Worker 台账（计算身份 / 系统盘 / 默认启动） |
| `state/credentials.yml` | NVMe-oF 认证密钥库（DHHC-1，按 Worker 跟盘） |
| `state/credential-push.yml` | 凭据推送状态台账（逐 Agent 送达结果，对账与投影用，见[系统盘](./disks.md)） |
| `state/masters.yml` | 母盘标签登记台账（os / os_version 备注，见[Agent 与存储](./agents.md)） |
| `state/settings.json` | 运行时设置（注册窗口 / 证明强制开关） |
| `state/users.yml` | 管理面用户组映射（CN → groups；首次启动自举 admin 条目，之后随证书 CN 查组） |
| `state/sessions.yml` | 服务端会话登记（key = `sha256(jti)`；删条目即失效，`epoch` 用于全部下线） |
| `state/audit.jsonl` | 认证/授权/凭据审计与告警（见[认证与授权](./auth.md)；与 `operations.jsonl` 分离） |
| `state/pki/ca/{users,nodes,control}/` | 三分根 CA（users=人类、nodes=节点组件、control=控制面客户端） |
| `state/pki/users/issued.yml` | users 域证书登记（按序列号；含吊销时间与原因） |
| `state/pki/components/issued.yml` | nodes 域组件证书登记（agent / nvmet-host，签发时写入；含吊销时间与原因） |
| `state/pki/control/issued.yml` | control 域证书登记（控制面客户端，签发时写入；含吊销时间与原因） |
| `state/pki/nginx/ca-bundle.pem` | nginx 信任包（仅 users + nodes 公钥；不含私钥） |
| `state/pki/nginx/clients.crl` | nginx 客户端证书 CRL（users + nodes 两张表拼接） |
| `state/pki/nginx/nodes.crl` / `control.crl` | 组件面 CRL（分别由 nodes / control CA 签发；经 `/enroll/crl` 分发给组件，`nextUpdate` 30 天） |
| `state/operations.jsonl` | 控制面操作轨迹（含 actor） |
| `dnsmasq/dhcp-hosts.conf` | `MAC -> hostname` 绑定（**派生视图**，权威在设备台账） |
| `bootstrap/` | 首次部署的一次性管理凭据交换目录（`request` → `ready` → `consumed`；消费后删除） |

说明：

- 部署记录 `~/.kurrent/deployment.yaml`（0600）固定项目名、仓库根与状态根，不在容器内；`kurrent deployment show` 只读展示；
- `workers.yml` **不记录 MAC**——MAC 与绑定关系统一由设备台账持有，Worker 侧只是投影；
- compose 把整个 `dnsmasq/` 目录挂进控制面容器（控制面还要往该目录写 `dnsmasq.conf`）；
- 写 `dhcp-hosts.conf` 采用**截断写**（保持 inode），不用临时文件做 atomic replace：该文件以文件级 bind mount 进 dnsmasq 容器，换掉 inode 后容器读到的仍是旧文件；
- `dnsmasq/dhcp-hosts.conf` 一行一个绑定，格式固定为：

```text
00:0c:29:b9:8b:2d,worker-01
```

该文件只服务 DHCP 的主机名/地址分配，不参与设备↔Worker 的归属判定：归属始终以设备台账为准，控制面在绑定/换绑时写入该投影（缺失时补写），dnsmasq 不会反向改写台账。投影缺失或陈旧只表示派生物不一致，不改变绑定权威。

---

## GET /healthz

### 说明

健康检查接口，不改状态，不需要鉴权。启动期派生件（`dnsmasq.conf`、服务器证书、组件 PKI）失败时返回 `503 {"status":"degraded"}`——`kurrent init` 据此回滚声明；错误详情只在已认证的 `GET /config` 返回。

### curl

```bash
curl -s "$BASE_URL/healthz"
```

### 成功返回

```json
{"status":"ok"}
```

---

## GET /config（声明配置状态）

返回运行进程加载的生效声明、规范化 `configHash` 与派生件一致性（需管理面认证：`human` 或 `automation`）。用于回答「现在运行的是哪份声明、派生物是否与之一致」；不返回任何私钥/密钥。

| 字段 | 说明 |
|---|---|
| `apiVersion` / `kind` / `metadata.name` | 声明身份 |
| `spec` | 生效配置（camelCase alias，默认值已注入；与 `kurrent config show` 输出一致） |
| `configHash` | 生效配置的规范化 SHA-256（排序键紧凑 JSON；与 `kurrent config validate` 输出的哈希可比对） |
| `derived.dnsmasqConf` | `path` 与 `matchesDeclaration`（磁盘上的 dnsmasq.conf 是否等于当前声明渲染结果） |
| `derived.serverCert` | `present` / `fingerprint` / `san` / `days` / `notAfter` / `drift`（`san` / `days` / `expired` / `missing`） / `rotateOnChange`；漂移默认只报告，`rotateOnChange=true` 才重签 |
| `startup.errors` / `startup.warnings` | 启动期派生失败/告警（`errors` 非空时 `/healthz` 返回 503） |

```bash
kurrent config status     # CLI 封装（443 mTLS / Bearer）
curl -s --cert "$CERT" --key "$KEY" "$BASE_URL/config"
```

---
