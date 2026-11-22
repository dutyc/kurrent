# 校验与生效

声明在生效前必须通过**校验**：拼错的字段、越界的取值与互相矛盾的网络参数，都在写盘之前被拦下。CLI 在组件镜像内运行同一份 schema，不在本地维护字段白名单，因此校验标准与运行时完全一致。校验在写盘前完成，失败时不修改任何文件，也不启动容器。

```bash
kurrent config validate              # 控制面声明
kurrent config validate --node       # 节点声明
kurrent config show                  # 输出注入默认值后的生效声明
```

## 校验规则

| 规则 | 行为 |
| --- | --- |
| `extra="forbid"` | 未知字段、拼写错误、旧键直接拒绝 |
| 默认值注入 | 缺失的策略块使用 schema 默认值 |
| 跨字段校验 | 网段/网关/地址池一致性、token TTL 上限、证书天数上限等 |
| 路径/NQN | 绝对路径、NQN 命名域、地址在声明期校验；节点身份由 join 校验状态文件 |
| 版本锁 | `apiVersion` 与 `kind` 必须匹配 |

## 控制面生效

```bash
kurrent init
```

`kurrent init` 把声明收敛为运行态，依次执行：

1. 构建校验镜像（内部步骤）；
2. 在镜像内按运行时 schema 校验声明；
3. 应用声明到 `control_plane/kurrent.yaml`；
4. 启动/重建容器，重启控制面；
5. 等待 `/healthz`；启动期派生件失败会返回 503；
6. 确认 dnsmasq 保持运行。

任何一步失败都有明确的退路：校验失败不写盘；启动失败会恢复上一份声明，重启控制面与 dnsmasq，并返回非零退出码。

## 存储节点生效

```bash
kurrent join <cp-url> [--token <token>]
```

`kurrent join` 把节点接入控制面，依次执行：

1. 读取或生成节点声明；
2. 合并默认值，并用 agent 镜像内的同一份 schema 校验；
3. 在节点状态根下写入 `storager/state/agent.id` 与引导凭据（首次执行需要 `--agent-id`）；
4. 以 `diskDir`、节点状态根与信任锚注入 compose 变量并启动 agent，确认其保持运行；
5. nvmet 后端预检宿主前置（内核模块、configfs、`tlshd >= 1.5.0`），并等待派生凭据后重启 nvmet-host。

声明校验失败不会写任何文件。

## 接下来

- 返回[声明式配置](../configuration.md)
- 控制面声明：[控制面声明](./control-plane.md)
- 节点声明：[节点声明](./node.md)
- 运行态与漂移：[漂移与覆盖](./drift.md)