# 配置参考

Kurrent 有两份权威声明：控制面配置与节点配置。二者都使用 `kurrent.io/v1`，由组件内的 Pydantic 模型校验；CLI 调用的校验镜像与运行时加载的是同一份 schema。

- 控制面：`control_plane/kurrent.yaml`，`kind: ControlPlaneConfiguration`
- 节点：`storager/kurrent.yaml`，`kind: NodeConfiguration`

模板由 `kurrent config print init-defaults` 与 `kurrent config print node-defaults` 生成，与仓库中的 `.example` 文件同源。运行时文件不入库。

## 校验语义

| 语义 | 行为 |
| --- | --- |
| 未知字段 | 拒绝；字段拼写错误不会静默忽略 |
| 必填字段 | 缺失即失败 |
| 默认值 | 校验时注入，`config show` 展示生效形态 |
| 跨字段约束 | 网段、TTL 上限、证书天数等在同一模型内校验 |
| 协议常量 | nonce TTL、超时等留在代码，不进入声明 |
| 部署事实 | 容器内路径、监听地址、凭据文件不属于声明 |

校验发生在写盘之前。声明错误会在 `kurrent init` / `kurrent join` 阶段暴露，不会等到容器启动才失败。

## 控制面配置

### 根字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `apiVersion` | string | 是 | 固定 `kurrent.io/v1` |
| `kind` | string | 是 | 固定 `ControlPlaneConfiguration` |
| `metadata.name` | string | 是 | 控制面名称，1–63 字符，小写字母、数字、点或短横线 |
| `spec` | object | 是 | 见下 |

### `spec.networking`（必填）

PXE 部署网络声明，也是 `dnsmasq.conf` 的唯一权威来源。

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `interface` | string | 是 | 1–15 字符，不含空白 | dnsmasq 绑定的网卡 |
| `subnet` | string | 是 | IPv4 CIDR | 服务网段 |
| `dhcpRange` | string | 是 | `start,end`，均在网段内，不含网络/广播地址 | DHCP 地址池 |
| `gateway` | string | 是 | IPv4，位于网段内 | DHCP option 3 |
| `dns` | string | 是 | IPv4 | DHCP option 6 |

### `spec.pki`

| 字段 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `bootstrapTokenTtlMinutes` | `60` | 1–1440 | bootstrap token 默认 TTL |
| `bootstrapTokenMaxTtlMinutes` | `1440` | 1–1440 | 可签发的最大 TTL，且不小于默认值 |
| `bootstrapTokenMaxUses` | `100` | 1–1000 | 单个 token 的最大使用次数 |
| `componentCertDays` | `90` | 1–3650 | 组件证书有效期 |
| `renewThreshold` | `0.2` | (0, 1] | 剩余有效期低于该比例时触发续签 |
| `crlRefreshHours` | `12` | 1–167 | 组件 CRL 重签周期；应小于 CRL 的 `nextUpdate` |

### `spec.serverCert`

TOFU 引导链的 nginx 443 自签服务器证书。

| 字段 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `san` | `IP:127.0.0.1,DNS:localhost` | 至少一项，不可重复 | 逗号分隔的 `IP:` / `DNS:` 条目 |
| `days` | `3650` | 1–36500 | 证书有效期 |
| `rotateOnChange` | `false` | — | 声明变化时是否自动重签；重签会改变 TOFU 指纹 |

SAN 或天数变化默认只报告漂移；`rotateOnChange: true` 才会自动重签。已入池设备需要重新固定服务器指纹。

### `spec.boot`

| 字段 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `defaultArch` | `x86_64` | 字母、数字、下划线 | 默认引导架构 |
| `menuTimeoutMs` | `5000` | ≥ 0 | 菜单自动选择超时（毫秒） |
| `autoBootTimeoutMs` | `1` | ≥ 0 | 无默认盘时重启等待超时（毫秒） |

### `spec.auth`

| 字段 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `sessionTtlHours` | `12` | 1–168 | 管理面会话有效期 |
| `adminCertDays` | `365` | 1–3650 | 管理员证书默认有效期，不超过 `userCertDaysMax` |
| `userCertDaysMax` | `3650` | 1–36500 | 用户证书可签发的最大有效期 |
| `tokenTtlHours` | `24` | 1–8760 | 管理 token 默认 TTL，不超过 `tokenMaxTtlHours` |
| `tokenMaxTtlHours` | `720` | 1–8760 | 管理 token 最大 TTL |

### `spec.registration`

| 字段 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `windowMaxTtlMinutes` | `60` | 1–1440 | 注册窗口单次可开启的最大时长 |
| `windowMaxDevices` | `1000` | 1–100000 | 单个窗口可放行的最大设备数 |

注册窗口本身是运行时状态，只在部署期短开；声明只约束上限。

### `spec.credentials`

| 字段 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `rotationDays` | `90` | 0–3650 | DHHC-1 定期轮换周期；`0` 关闭 |
| `reconcileIntervalSeconds` | `60` | 0–86400 | 凭据推送对账周期；`0` 关闭 |

### `spec.agentTimeoutSec`

| 字段 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `agentTimeoutSec` | `10` | (0, 600] | 控制面调用存储 Agent 的超时（秒） |

### 完整示例

```yaml
apiVersion: kurrent.io/v1
kind: ControlPlaneConfiguration
metadata:
  name: kurrent-cp
spec:
  networking:
    interface: enp3s0
    subnet: 192.168.80.0/24
    dhcpRange: 192.168.80.50,192.168.80.100
    gateway: 192.168.80.2
    dns: 223.5.5.5
  pki:
    bootstrapTokenTtlMinutes: 60
    bootstrapTokenMaxTtlMinutes: 1440
    bootstrapTokenMaxUses: 100
    componentCertDays: 90
    renewThreshold: 0.2
    crlRefreshHours: 12
  serverCert:
    san: "IP:127.0.0.1,DNS:localhost"
    days: 3650
    rotateOnChange: false
  boot:
    defaultArch: x86_64
    menuTimeoutMs: 5000
    autoBootTimeoutMs: 1
  auth:
    sessionTtlHours: 12
    adminCertDays: 365
    userCertDaysMax: 3650
    tokenTtlHours: 24
    tokenMaxTtlHours: 720
  registration:
    windowMaxTtlMinutes: 60
    windowMaxDevices: 1000
  credentials:
    rotationDays: 90
    reconcileIntervalSeconds: 60
  agentTimeoutSec: 10
```

## 节点配置

### 根字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `apiVersion` | string | 是 | 固定 `kurrent.io/v1` |
| `kind` | string | 是 | 固定 `NodeConfiguration` |
| `metadata.name` | string | 是 | 节点身份（agent_id）：12 位小写十六进制，与盘 `os_tag` 同构 |
| `metadata.remark` | string | 否 | 人类备注（描述性名称的唯一归属），默认空 |
| `spec` | object | 是 | 见下 |

`metadata.name` 同时是控制面登记键与组件证书 CN，由 bootstrap token 绑定。`kurrent join` 写入后不应手工改写；描述性名称写 `metadata.remark`，不得占用身份字段。

### `spec.agent`

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `backend` | string | 否 | `nvmet` / `stgt` / `lio`，默认 `nvmet` | 数据面后端 |
| `advertiseUrl` | string | 否 | http(s) URL，可空 | 控制面可达的 Agent 地址，用于登记与续签 |
| `diskDir` | string | 是 | 宿主绝对路径 | 盘镜像目录；也是 compose 挂载源 |
| `nqnBase` | string | 是 | `nqn.<yyyy-mm>.<reverse-domain>` | Host NQN 与盘 NQN 的命名域 |

### `spec.controlPlane`

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `url` | string | 是 | http(s) URL | 组件 enroll / renew 的 mTLS 连接目标 |

### 完整示例

```yaml
apiVersion: kurrent.io/v1
kind: NodeConfiguration
metadata:
  name: 3f2a9c1d8b7e
  remark: 生产存储
spec:
  agent:
    backend: nvmet
    advertiseUrl: https://192.168.80.10:4840
    diskDir: /storager_img
    nqnBase: nqn.2026-07.com.kurrent
  controlPlane:
    url: https://192.168.80.1
```

## 应用与核对

### 控制面

```bash
kurrent config print init-defaults > control_plane/kurrent.yaml
$EDITOR control_plane/kurrent.yaml
kurrent config validate
kurrent config show          # 注入默认值后的生效形态
kurrent init                 # 校验、应用、启动、等待健康检查
kurrent config status        # configHash、派生文件与证书漂移
```

`init` 是幂等的：修改声明后重跑即生效。校验失败不写盘；启动失败会回滚上一份声明并恢复运行，退出码非零。

### 节点

```bash
kurrent config print node-defaults > storager/kurrent.yaml
$EDITOR storager/kurrent.yaml
kurrent config validate --node
kurrent join https://<control-plane> --token <token> --agent-id <agent_id>
```

`join` 在声明缺失时按模板生成，已存在时读入并同步控制面地址。节点声明错误在写盘前失败，不会等到容器启动才暴露。

## configHash 与漂移

控制面的 `configHash` 是对注入默认值后的生效声明做规范化 JSON（排序键、紧凑分隔符）再取 SHA-256。它用于确认运行态加载的是哪一份声明，不包含运行时设置。

派生与漂移：

| 对象 | 来源 | 漂移处理 |
| --- | --- | --- |
| `dnsmasq.conf` | `spec.networking` | 启动时重新生成；`config status` 报告差异 |
| 服务器证书 | `spec.serverCert` | 默认只报告；`rotateOnChange` 开启后重签 |
| 组件 PKI | `spec.pki` | 启动时校验；缺失或损坏导致健康检查失败 |

注册窗口、验签强制开关等运行时设置存放于 `state/settings.json`，不进入声明，也不参与 `configHash`。

## 环境变量

| 变量 | 作用 | 范围 |
| --- | --- | --- |
| `KURRENT_CONFIG_FILE` | 覆盖进程读取的声明路径 | 控制面与节点；测试注入用 |
| `KURRENT_CP_INSECURE` | 开发模式放行所有请求 | 仅本地开发，生产禁用 |
| `KURRENT_CP_OPENAPI` | 开启 OpenAPI 文档端点 | 默认关闭 |
| `KURRENT_CP_ALARM_WEBHOOK` | 告警外投 URL | 可选 |
| `KURRENT_CP_CREDENTIAL_ROTATION_DAYS` | 临时覆盖轮换周期 | 运维与测试 |
| `KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL` | 临时覆盖对账周期 | 运维与测试 |
| `KURRENT_CP_CRL_REFRESH_INTERVAL` | 临时覆盖 CRL 重签周期 | 运维与测试 |

声明是策略的缺省来源；后台任务的两个环境变量只作临时覆盖。容器内路径与监听地址由 compose 固化，不属于声明。

## 相关页面

- [声明式配置](../concepts/configuration.md)
- [日常运维](../guide/operations.md)
- [CLI 参考](./cli.md)
- [API 参考](./api.md)
- [术语表](./glossary.md)
