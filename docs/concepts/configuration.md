# 声明式配置

一套系统部署时要决定的参数很多：网段、地址池、证书有效期、凭据轮换周期。如果这些参数散落在环境变量、容器配置与命令行参数里，运行态就会逐渐偏离「人看到的那份配置」，排障只能靠猜。Kurrent 的做法是把所有人能选择的参数收进两份 YAML 声明：**控制面声明**与**节点声明**。声明是唯一输入，CLI 负责校验与收敛，容器编排只是实现细节。

```text
control_plane/kurrent.yaml      → 控制面、dnsmasq 派生配置、PKI 与策略
storager/kurrent.yaml           → 数据目录、后端与 NQN 命名域
<state>/storager/state/agent.id → 节点身份（join 写入，容器只读）
```

节点身份不在声明里。它由控制面在签发引导 token 时分配，单独存为节点状态文件。这样划分是因为两者的性质不同：声明是人能选择的参数，随仓库移动；身份是系统分配的事实，属于这台机器，不随仓库移动。

`<state>` 是仓库外的状态根（默认 `~/.local/state/kurrent`），由部署记录 `~/.kurrent/deployment.yaml` 固定。声明随仓库移动，运行时状态不随仓库移动。

## 一份声明长什么样

控制面声明的最小例子：

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
```

除 `spec.networking` 与 `metadata.name` 外，其余块都有默认值。密钥与 token 不进入 YAML。

## 分工

| 层 | 内容 | 位置 |
| --- | --- | --- |
| 声明 | 业务策略、网络、路径、命名域等人能选择的参数 | `kurrent.yaml` |
| 节点状态 | 机器身份（agent_id） | `<state>/storager/state/agent.id` |
| 编排 | 容器名、镜像、挂载目标、端口映射、内部地址 | `docker-compose.yml` |
| 运行时状态 | 注册窗口、会话、设备、Worker、盘、凭据、审计 | `<state>/control-plane/state/` |
| 一次性凭据 | bootstrap token、派生 token | `<state>/storager/bootstrap/` |
| 首次部署交接 | 管理凭据交换（`request` → `ready` → `consumed`） | `<state>/control-plane/bootstrap/` |

容器内路径、监听地址与内部通讯地址属于编排层，不进入声明；写进声明会被 schema 作为未知字段拒绝。这条边界保证声明里只有业务决策，换一套编排方式不需要改声明。

## 控制面声明

控制面声明定义网络、PKI、服务器证书、引导策略、认证策略、注册窗口与凭据轮换。各块的含义见[控制面声明](./configuration/control-plane.md)，完整键表见[配置参考](../reference/configuration.md)。

## 节点声明

节点声明定义数据目录、后端、对外地址与 NQN 命名域。结构与字段见[节点声明](./configuration/node.md)。

## 校验与生效

CLI 在组件镜像内运行同一份 schema，不在本地维护字段白名单。这样校验标准与运行时完全一致，不会出现「CLI 放行、运行时拒绝」的分裂。校验在写盘前完成，失败时不修改任何文件、也不启动容器。

`kurrent init` 与 `kurrent join` 分别收敛控制面与存储节点。收敛过程见[校验与生效](./configuration/validation.md)。

## 漂移与覆盖

`configHash` 用于确认运行态加载了哪份声明。dnsmasq、服务器证书、PKI 等派生物从声明单向生成，漂移默认只报告。

少量环境变量可临时覆盖后台参数，用于测试与排障。检测与覆盖方式见[漂移与覆盖](./configuration/drift.md)。

## 相关页面

- 完整键表与默认值：[配置参考](../reference/configuration.md)
- 节点身份与三实体：[设备、Worker 与系统盘](./entities.md)
- 服务端证书与引导信任：[引导链](./boot-chain.md)
- 配置生效的实现边界：[架构总览](../architecture/index.md)