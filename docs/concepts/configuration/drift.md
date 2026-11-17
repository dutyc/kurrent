# 漂移与覆盖

声明是唯一输入，运行态是它的投影。但派生物（dnsmasq 配置、证书等）是生成出来的文件，总有可能与声明不一致——有人手工改了文件，或者生成过程中断。`configHash` 用于确认运行态加载了哪份声明；派生物与声明不一致时，默认只报告，不自动改写，把处置权留给管理员。

## configHash

`kurrent config status` 返回生效声明的规范化 SHA-256：

```bash
kurrent config status    # configHash
kurrent config show      # 注入默认值后的生效形态
```

同一份声明两次应用得到相同 configHash；文件时间或人工编辑不参与判断。

## 派生件

| 派生件 | 权威 | 漂移处理 |
| --- | --- | --- |
| `dnsmasq.conf` | `spec.networking` | `config status` 报告是否一致；`init` 重新生成 |
| 服务器证书 | `spec.serverCert` + 首次生成 | 默认只报告 SAN/天数漂移；`rotateOnChange` 才重签 |
| 组件 PKI | `spec.pki` | 启动时幂等补齐；删除 PKI 目录会使既有证书失效，等同于重建信任根 |
| 凭据轮换 | `spec.credentials` | 后台周期任务读取 |
| 注册窗口 | `spec.registration` | 约束窗口上限，不自动开窗 |

重签叶证书不改变设备信任的 boot CA，不需要重新入池；默认只报告漂移，轮换必须显式开启。

## 启动错误

启动期派生件失败（dnsmasq 生成、证书、PKI）会记录到 `startup.errors`：

- `/healthz` 返回 503；
- `kurrent init` 因此判定启动失败并回滚；
- `kurrent config status` 的 `startup` 字段给出错误与告警。

## 运行时覆盖

| 变量 | 覆盖 |
| --- | --- |
| `KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL` | 凭据推送对账周期（秒） |
| `KURRENT_CP_CREDENTIAL_ROTATION_HOURS` | DHHC-1 定期轮换周期（小时） |
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

## 相关页面

- 返回[声明式配置](../configuration.md)
- 校验与生效：[校验与生效](./validation.md)
- 运维细节、回滚与备份：[日常运维](../../guide/operations.md)