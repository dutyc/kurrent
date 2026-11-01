# 声明式配置

Kurrent 的运行参数由两份 YAML 声明描述：控制面声明与节点声明。声明是唯一输入，CLI 负责校验与收敛，容器编排是实现细节。

```text
control_plane/kurrent.yaml   → 控制面、dnsmasq 派生配置、PKI 与策略
storager/kurrent.yaml        → 节点身份、数据目录、后端与 NQN 命名域
```

## 分工

| 层 | 内容 | 位置 |
| --- | --- | --- |
| 声明 | 业务策略、网络、路径、身份、命名域 | `kurrent.yaml` |
| 编排 | 容器名、镜像、挂载目标、端口映射、内部地址 | `docker-compose.yml` |
| 运行时状态 | 注册窗口、会话、设备、Worker、盘、凭据、审计 | `control_plane/state/` |
| 一次性凭据 | bootstrap token、派生 token | `storager/bootstrap/` |

容器内路径、监听地址与内部通讯地址属于编排层，不进入声明；写进声明会被 schema 作为未知字段拒绝。密钥与 token 不进入 YAML。

## 控制面声明

```yaml
apiVersion: kurrent.io/v1
kind: ControlPlaneConfiguration
metadata:
  name: kurrent-cp
spec:
  networking:        # 必填：五项部署事实
    interface: enp3s0
    subnet: 192.168.80.0/24
    dhcpRange: 192.168.80.50,192.168.80.100
    gateway: 192.168.80.2
    dns: 223.5.5.5
  pki: {}            # bootstrap token、组件证书、renew 阈值、CRL 周期
  serverCert: {}     # TOFU 服务器证书 SAN/天数/是否随声明轮换
  boot: {}           # 架构、菜单超时、自动启动超时
  auth: {}           # 会话/证书/token 策略
  registration: {}   # 注册窗口 TTL 与放行额度上限
  credentials: {}    # DHHC-1 轮换与推送对账周期
  agentTimeoutSec: 10
```

除 `spec.networking` 与 `metadata.name` 外，其余块都有默认值。完整键表见[配置参考](../reference/configuration.md)。

### 网络声明

`spec.networking` 是 dnsmasq 配置的唯一权威：

- `interface` 写入 `interface=`；
- `subnet` 推导 netmask；
- `dhcpRange` 写入地址池；
- `gateway` / `dns` 写入 DHCP option 3/6。

控制面启动时按声明生成 `dnsmasq/dnsmasq.conf`。**生成文件不得手工编辑**，否则下次启动时将被覆盖。`kurrent config status` 会报告生成文件是否与声明一致。

## 节点声明

```yaml
apiVersion: kurrent.io/v1
kind: NodeConfiguration
metadata:
  name: 3f2a9c1d8b7e      # 节点身份（agent_id）：12 位小写 hex，由 join 写入
  remark: 生产存储节点      # 描述性名称写这里
spec:
  agent:
    backend: nvmet
    advertiseUrl: https://192.168.1.20:4840
    diskDir: /pool1/disk_dir
    nqnBase: nqn.2026-07.com.kurrent
  controlPlane:
    url: https://192.168.1.10
```

- `metadata.name` 是机器身份，同时是组件证书 CN 与控制面登记键；只接受 12 位小写十六进制。
- `metadata.remark` 是描述性名称的唯一归属，随首次 enroll 登记进控制面台账。
- `diskDir` 必须是宿主绝对路径；它同时同步为 compose 挂载源。
- `nqnBase` 是 Host NQN 与盘 NQN 的命名域；控制面不另行声明 NQN 域。

## 校验

CLI 不在本地维护字段白名单，而是在组件镜像内运行同一份 schema：

```bash
kurrent config validate              # 控制面声明
kurrent config validate --node       # 节点声明
kurrent config show                  # 输出注入默认值后的生效声明
```

语义：

| 规则 | 行为 |
| --- | --- |
| `extra="forbid"` | 未知字段、拼写错误、旧键直接拒绝 |
| 默认值注入 | 缺失的策略块使用 schema 默认值 |
| 跨字段校验 | 网段/网关/地址池一致性、token TTL 上限、证书天数上限等 |
| 身份/路径/NQN | 节点名、绝对路径、NQN 命名域在声明期校验 |
| 版本锁 | `apiVersion` 与 `kind` 必须匹配 |

校验在写盘前完成。失败时不会修改任何文件，也不会启动容器。

## 生效

### 控制面

```bash
kurrent init
```

流程：

1. 构建校验镜像（内部步骤）；
2. 在镜像内按运行时 schema 校验声明；
3. 应用声明到 `control_plane/kurrent.yaml`；
4. 启动/重建容器，重启控制面；
5. 等待 `/healthz`；启动期派生件失败会返回 503；
6. 确认 dnsmasq 保持运行。

任何一步失败：

- 校验失败：不写盘；
- 启动失败：恢复上一份声明，重启控制面与 dnsmasq，返回非零退出码。

### 存储节点

```bash
kurrent join <cp-url> [--token <token>]
```

流程：

1. 读取或生成节点声明；
2. 合并默认值，并用 agent 镜像内的同一份 schema 校验；
3. 写入声明、引导凭据与 `.env` 插值键；
4. 启动 agent，确认其保持运行；
5. nvmet 后端预检宿主前置（内核模块、configfs、`tlshd >= 1.5.0`），并等待派生凭据后重启 nvmet-host。

声明校验失败不会写任何文件。

## 运行态与漂移

### configHash

`kurrent config status` 返回生效声明的规范化 SHA-256。它用于确认运行态加载了哪份声明：

```bash
kurrent config status    # configHash
kurrent config show      # 注入默认值后的生效形态
```

同一份声明两次应用得到相同 configHash；文件时间或人工编辑不参与判断。

### 派生件

| 派生件 | 权威 | 漂移处理 |
| --- | --- | --- |
| `dnsmasq.conf` | `spec.networking` | `config status` 报告是否一致；`init` 重新生成 |
| 服务器证书 | `spec.serverCert` + 首次生成 | 默认只报告 SAN/天数漂移；`rotateOnChange` 才重签 |
| 组件 PKI | `spec.pki` | 启动时幂等补齐；删除 PKI 目录会使既有证书失效，等同于重建信任根 |
| 凭据轮换 | `spec.credentials` | 后台周期任务读取 |
| 注册窗口 | `spec.registration` | 约束窗口上限，不自动开窗 |

服务器证书重签会改变 TOFU 指纹，已入池设备需要重新信任。因此默认只报告漂移，轮换必须显式开启。

### 启动错误

启动期派生件失败（dnsmasq 生成、证书、PKI）会记录到 `startup.errors`：

- `/healthz` 返回 503；
- `kurrent init` 因此判定启动失败并回滚；
- `kurrent config status` 的 `startup` 字段给出错误与告警。

## 运行时覆盖

以下环境变量可临时覆盖声明中的后台参数：

| 变量 | 覆盖 |
| --- | --- |
| `KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL` | 凭据推送对账周期（秒） |
| `KURRENT_CP_CREDENTIAL_ROTATION_DAYS` | DHHC-1 定期轮换天数 |
| `KURRENT_CP_CRL_REFRESH_INTERVAL` | CRL 重签周期（秒） |

声明是缺省来源；环境变量用于测试与排障。生产环境应把稳定值写进声明，避免运行态与声明分离。

## 变更流程

```bash
vim control_plane/kurrent.yaml
kurrent config validate
kurrent init
kurrent config status
```

节点：

```bash
vim storager/kurrent.yaml
kurrent config validate --node
kurrent join <cp-url>
```

声明修改的运维细节、回滚与备份见[日常运维](../guide/operations.md)。

## 相关页面

- 完整键表与默认值：[配置参考](../reference/configuration.md)
- 节点身份与三实体：[设备、Worker 与系统盘](./entities.md)
- 服务端证书与 TOFU：[引导链](./boot-chain.md)
- 配置生效的实现边界：[架构总览](../architecture/index.md)
