# CLI 参考

`kurrent` 是控制面的命令行客户端与部署工具，单二进制分发，覆盖声明模板、控制面初始化、节点加入、凭据签发与日常查询。命令语法、完整参数与行为以仓库中的 `cli/README.md` 为准；本页给出命令面与常用流程。

## 安装与凭据

```bash
cd cli
go build -o kurrent .
sudo install -m 0755 kurrent /usr/local/bin/kurrent
kurrent credentials install    # 把管理凭据快照到 ~/.kurrent
```

`credentials install` 复制 `control_plane/state/admin.conf` 与证书副本到 `~/.kurrent`（目录 0700、文件 0600），使 PATH 安装的二进制脱离仓库也可使用 443 mTLS。凭据轮换后需重新执行 `install` 刷新。

凭据发现顺序：

1. `--credentials` 指定的文件
2. `KURRENT_CREDENTIALS` 环境变量
3. 从当前目录逐级上溯 `control_plane/state/admin.conf`
4. 可执行文件旁的 `<exe>/../control_plane/state/admin.conf`
5. 用户级快照 `~/.kurrent/admin.conf`

没有管理凭据时必须显式给出 `--server` 与 `--token`，走 Bearer 通道。

## 命令面

```text
kurrent [--credentials PATH] [--server URL] [--token T] <group> <verb> [args]
```

| 命令组 | 子命令 | 用途 |
| --- | --- | --- |
| `config` | `print init-defaults` / `print node-defaults` | 输出控制面或节点声明模板 |
| | `validate [--node] [--config PATH]` | 在组件镜像内按运行时 schema 校验 |
| | `show [--node] [--config PATH]` | 输出注入默认值后的生效声明 |
| | `status` | 运行态声明、configHash、dnsmasq 与证书漂移 |
| `init` | — | 控制面初始化：校验、应用、启动、健康检查，失败回滚 |
| `join` | — | 节点加入：生成或合并声明，收敛启动 Agent |
| `token` | `create` / `list` / `revoke` | 签发、列出、吊销 bootstrap 与管理 token |
| `sessions` | `list` / `revoke` / `revoke-all` | 管理面会话台账与逐个/全部吊销 |
| `cert` | `keygen` / `issue` / `list` / `revoke` | 用户证书：本地生成 CSR、签发、列出、吊销 |
| | `components` / `revoke-component` | 存储节点组件证书：列出与吊销 |
| `credentials` | `install` / `show` / `remove` | 管理凭据用户级快照 |
| `agents` | `list` / `get` / `add` / `edit` / `remove` / `probe` | 存储 Agent 台账与能力探测 |
| `workers` | `list` / `get` | Worker 台账（只读） |
| | `credential show` / `set` / `revoke` | NVMe-oF 认证密钥的查看、轮换、吊销 |
| `ops` | `list` | 操作审计日志 |
| `version` | — | 输出版本号 |

## 控制面初始化

```bash
kurrent config print init-defaults > control_plane/kurrent.yaml
$EDITOR control_plane/kurrent.yaml
kurrent config validate
kurrent init
kurrent config status
```

`init` 的行为：

- 构建校验镜像并全量校验声明；
- 校验失败不写盘，退出码非零；
- 应用声明、重启控制面、等待 `/healthz`；
- 启动失败回滚上一份声明并恢复运行；
- 重启 dnsmasq 并核验其保持运行；
- 幂等，修改声明后重跑即生效。

`--config` 可指向其他声明文件，将其应用为控制面配置。

## 节点加入

```bash
# 控制面：签发绑定节点身份的 bootstrap token，输出完整 join 命令
kurrent token create --agent-id storage-nvmet-01 --cp-url https://192.168.80.1

# 节点：粘贴上一步输出的命令
kurrent join https://192.168.80.1 --token <token> --agent-id storage-nvmet-01
```

`join` 的行为：

- `storager/kurrent.yaml` 缺失时按模板生成，已存在时读入并同步控制面地址；
- 写入 `bootstrap/agent.token`（0600）与 `.env` 插值键；
- 使用 compose 收敛启动 Agent；
- nvmet 后端预检内核模块、configfs 与 `tlshd >= 1.5.0`，未就绪时指路 `storager/nvmeof/host-prepare.sh`；
- 等待派生的 `nvmet-host` 凭据写出后重启该组件；
- 幂等，可重跑。

仓库根以外执行需 `--dir` 指定 `storager` 目录。

## 凭据签发

### bootstrap token

```bash
kurrent token create --agent-id storage-nvmet-01
kurrent token create --agent-id storage-nvmet-01 --uses 20 --ttl 2h
```

- 默认一次性、60 分钟；
- 绑定 `agent_id`，enroll 只接受同一身份；
- `--uses` 放开使用次数（上限 100）；
- 输出带控制面地址与节点 id 的 join 命令。

### 管理 token

```bash
kurrent token create --usage admin
kurrent token create --usage admin --scope readonly --ttl 24h
```

- 供脚本与 WebUI 登录；
- `--scope` 取 `admin` / `readonly` / `enroll`；
- 默认 24 小时，上限 720 小时；
- 只存 SHA-256 摘要，明文只在签发响应中出现一次。

`token list` 列出未过期条目，`token revoke <token_id>` 删除条目并立即失效。

## 证书

```bash
kurrent cert keygen alice --out ./certs
kurrent cert issue alice --csr ./certs/alice.csr --days 365
kurrent cert list
kurrent cert revoke alice --serial <hex>
kurrent cert components
kurrent cert revoke-component storage-nvmet-01
```

私钥只在本地生成，CSR 上送，私钥永不上传。`issue` 要求 CN 已在 `control_plane/state/users.yml` 登记。吊销会重签 CRL、reload nginx，并使该证书上的会话失效。组件吊销同时删除该节点的 bootstrap token；恢复时重新签发 token 并重跑 `join`。

## NVMe-oF 凭据

nvmet 建盘时控制面自动签发 DHHC-1 密钥。以下命令用于显式轮换与吊销：

```bash
kurrent workers credential show storage-worker-01
kurrent workers credential set storage-worker-01 --generate
kurrent workers credential revoke storage-worker-01
```

`--generate` 就地生成 32 字节密钥，不打印明文。`set` 输出逐 Agent 推送状态；未送达项由对账线程自动重试。吊销后设备下次引导被拒绝。

## 输出与退出码

| 项 | 约定 |
| --- | --- |
| 默认输出 | 表格 |
| `-o json` | 原始 JSON，便于 jq 处理 |
| 空字段 | 以 `-` 占位 |
| 成功 | 退出码 0 |
| 失败 | 非零退出码，错误写入 stderr |

位置参数与 `--key value`、`--key=value`、`--flag` 可任意混排。

## 常见错误

| 现象 | 排查 |
| --- | --- |
| `no management credentials` | 在仓库内执行、运行 `credentials install`，或显式给 `--server` + `--token` |
| mTLS 证书错误 | 凭据文件与服务端不匹配；重新执行 `credentials install` |
| `config validate` 失败 | 按报错修正声明；未知字段会被拒绝 |
| `join` 停在宿主预检 | 按提示执行 `host-prepare.sh`，确认内核模块、configfs 与 `tlshd` |
| token 无效或已过期 | 重新 `token create`；确认 `--agent-id` 与节点声明一致 |

## 相关页面

- [快速开始](../guide/quickstart.md)
- [部署指南](../guide/deployment.md)
- [日常运维](../guide/operations.md)
- [配置参考](./configuration.md)
- [API 参考](./api.md)
