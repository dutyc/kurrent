# 节点声明

**节点声明**（`NodeConfiguration`）定义存储节点的数据目录、后端、对外地址与 NQN 命名域。节点身份不在声明里：它由控制面在签发 bootstrap token 时分配，join 写入节点状态根。声明描述节点如何运行，身份描述节点是谁，两者分开存放。

```yaml
apiVersion: kurrent.io/v1
kind: NodeConfiguration
spec:
  agent:
    backend: nvmet
    advertiseUrl: https://192.168.1.20:4840
    diskDir: /pool1/disk_dir
    nqnBase: nqn.2026-07.com.kurrent
  controlPlane:
    url: https://192.168.1.10
```

- `agent_id`（12 位小写十六进制）由控制面在签发 bootstrap token 时分配，CLI 与 WebUI 只消费返回的身份。join 把它写入 `<state>/storager/state/agent.id`。它是组件证书 CN 与控制面登记键；昵称归控制面台账。
- `diskDir` 必须是宿主绝对路径；join 在启动容器时把它注入为 compose 挂载源，不落盘第二份配置。
- `nqnBase` 是 Host NQN 与盘 NQN 的命名域；控制面不另行声明 NQN 域。
- `advertiseUrl` 是控制面调用 Agent 的地址，必须从控制面可达。

## 接下来

- 返回[声明式配置](../configuration.md)
- 控制面侧声明：[控制面声明](./control-plane.md)
- 节点加入流程：[校验与生效](./validation.md)
- 盘与后端：[存储与数据面](../storage.md)