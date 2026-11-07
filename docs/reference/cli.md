# CLI 参考

`kurrent` 是控制面的命令行客户端与部署工具，单二进制分发，覆盖声明模板、控制面初始化、节点加入、凭据签发与日常查询。命令语法、完整参数与行为以仓库中的 `cli/README.md` 为准；本页给出命令面与常用流程。

## 安装与凭据

```bash
mkdir -p bin
cd cli
go build -o ../bin/kurrent .
sudo install -m 0755 ../bin/kurrent /usr/local/bin/kurrent
kurrent init                   # 首次部署：同时安装管理凭据到 ~/.kurrent
kurrent credentials install    # 后续轮换时刷新快照
```

`kurrent init` 通过一次性交换安装 `~/.kurrent/admin.conf` 与证书副本（目录 0700、文件 0600），使 PATH 安装的二进制脱离仓库也可使用 443 mTLS。凭据轮换后重新执行 `kurrent init` 或 `kurrent credentials install` 刷新。

凭据发现顺序：

1. `--credentials` 指定的文件
2. `KURRENT_CREDENTIALS` 环境变量
3. 部署记录指向的 `<state>/control-plane/state/admin.conf`
4. 从当前目录逐级上溯旧布局 `control_plane/state/admin.conf`
5. 可执行文件旁的 `<exe>/../control_plane/state/admin.conf`
6. 用户级快照 `~/.kurrent/admin.conf`

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
| `init` | — | 首次部署事务：校验、状态根准备、启动、凭据交换、自检；失败回滚或可续 |
| `join` | — | 节点加入：生成或合并声明，收敛启动 Agent |
| `deployment` | `show [-o yaml\|env]` | 只读展示部署记录、仓库根、状态根与手工 compose 变量 |
| `host` | `check` | 只读宿主基线：监听面、Docker 暴露、防火墙、凭据权限、状态根与存储节点内核前置 |
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

- 预检 Docker/Compose v2、WebUI 构建产物、状态根与 `~/.kurrent` 的属主与可写性；
- 构建校验镜像并全量校验声明，失败不写盘；
- 在状态根（默认 `~/.local/state/kurrent`）建立目录并种下 compose 挂载点，写入部署记录 `bootstrapping`；
- 以固定项目名 `kurrent` 启动、重启控制面并等待 `/healthz`；启动失败回滚上一份声明并恢复运行；
- 等待一次性凭据交换，校验后安装到 `~/.kurrent`，以安装副本完成 mTLS `GET /whoami` 自检；
- 交换包消费后重启 dnsmasq 并核验其保持运行，部署记录转 `ready`；
- 幂等：修改声明后重跑即生效；移动仓库后重跑按记录复用状态根并重建容器。

`--config` 可指向其他声明文件，将其应用为控制面配置；`--state-dir` 指定状态根；`--adopt-state` 把仓库内旧状态迁移到状态根（先停旧编排、逐文件校验复制，成功后删除旧副本）。

## 节点加入

```bash
# 控制面：签发绑定节点身份的 bootstrap token，输出完整 join 命令
kurrent token create --agent-id 3f2a9c1d8b7e --cp-url https://192.168.80.1

# 节点：粘贴上一步输出的命令
kurrent join https://192.168.80.1 --token <token> --agent-id 3f2a9c1d8b7e
```

`--agent-id` 是节点身份，只接受 12 位小写十六进制；省略时由控制面在签发 bootstrap token 时分配（显式传入则绑定已有身份，用于恢复），`kurrent token create` 与 WebUI 都只消费响应返回的 `agent_id`。join 首次执行把它写入节点状态根下的 `storager/state/agent.id`，之后重跑须与状态文件一致。昵称属于控制面台账，用 `kurrent agents edit <id> --remark <text>` 设置。控制面入口是 origin 地址（可带反向代理路径）；误贴的 `/api/cp` 后缀会在写声明前自动去除。

`join` 的行为：

- `storager/kurrent.yaml` 缺失时按模板生成，已存在时读入并同步控制面地址；
- 在节点状态根（默认 `~/.local/state/kurrent`）下建立 `storager/{state,bootstrap,pki,logs,certs}`，写入身份 `state/agent.id` 与引导凭据 `bootstrap/agent.token`（0600）；
- 解析控制面信任锚并复制为 `certs/cp-ca.crt`：`--cp-ca` ＞ 同机控制面状态根中的 `control-plane/state/certs/server.crt` ＞ 已有节点副本；
- 以 `diskDir`、节点状态根与信任锚注入 compose 变量，以固定项目名 `kurrent-storage` 收敛启动 Agent；
- nvmet 后端预检内核模块、configfs 与 `tlshd >= 1.5.0`，未就绪时指路 `storager/nvmeof/host-prepare.sh`；
- 等待派生的 `nvmet-host` 凭据写出后重启该组件；
- 幂等，可重跑。

`--dir` 可指向仓库根或 `storager` 目录；`--state-dir` 指定节点状态根；`--cp-ca` 提供分机部署的控制面信任锚；`--adopt-state` 把仓库内旧节点状态迁移到状态根。默认 `diskDir` 位于 checkout 内时 join 会告警（移动仓库会连同盘镜像移动），需要固定位置时在声明中写仓库外绝对路径。

## 宿主检查

```bash
sudo kurrent host check                        # 角色按运行中的 compose 服务推断
sudo kurrent host check --role all             # 同机部署，或容器未启动时显式指定
kurrent host check --role storage              # 内核 nvmet 前置 / configfs / tlshd
sudo kurrent host check --role control -o json --strict
```

只读本地检查，不访问管理 API，控制面未启动时也可用。公共 TCP 监听、Docker 守护进程 TCP 暴露与 `DOCKER-USER`/ufw 对两种角色都检查，端口白名单按角色区分（control：22/53/67/69/80/443；storage：22/4840/4421）。`control` 另查控制面容器暴露面、凭据权限与状态根（须在仓库外、0700、属主正确，且与部署记录一致）；`storage` 另查 nvmet / nvmet-tcp 模块、configfs 与 `tlshd`；`all` 取并集。容器按 compose 服务标签识别，不依赖固定容器名。状态为 `ok` / `warn` / `fail` / `skip`；退出码 `0` 通过、`1` 有 FAIL、`2` 用法错误，`--strict` 把 SKIP 也计为失败。修复系统状态仍用 `sudo bash storager/nvmeof/host-prepare.sh`。

## 凭据签发

### bootstrap token

```bash
kurrent token create --agent-id 3f2a9c1d8b7e
kurrent token create --agent-id 3f2a9c1d8b7e --uses 20
```

- 默认一次性、60 分钟（TTL 由控制面声明 `spec.pki.bootstrapTokenTtlMinutes` 决定）；
- 绑定 `agent_id`（12 位小写 hex），enroll 只接受同一身份；
- `--uses` 放开使用次数（1..1000，且不超过控制面 `spec.pki.bootstrapTokenMaxUses`，默认 100）；
- 输出带控制面地址与节点 id 的 join 命令。

### 管理 token

```bash
kurrent token create --usage admin
kurrent token create --usage admin --scope readonly --ttl 24h
```

- 供脚本与 WebUI 登录；
- `--scope` 取 `admin` / `readonly` / `enroll`；
- 默认 24 小时；上限由控制面 `spec.auth.tokenMaxTtlHours` 决定（默认 720 小时）；
- 只存 SHA-256 摘要，明文只在签发响应中出现一次。

`token list` 列出未过期条目，`token revoke <token_id>` 删除条目并立即失效。`token_id` 是明文点号前的 6 位前缀（`token list` 或签发输出可查）；粘贴完整 token 会被拒绝，避免 secret 进入服务端访问日志。

## 证书

```bash
kurrent cert keygen alice --out ./certs
kurrent cert issue alice --csr ./certs/alice.csr --days 365
kurrent cert list
kurrent cert revoke alice --serial <hex>
kurrent cert components
kurrent cert revoke-component 3f2a9c1d8b7e
```

私钥只在本地生成，CSR 上送，私钥永不上传。`issue` 要求 CN 已在 `<state>/control-plane/state/users.yml` 登记。吊销会重签 CRL、reload nginx，并使该证书上的会话失效。组件吊销同时删除该节点的 bootstrap token；恢复时重新签发 token 并重跑 `join`。

## NVMe-oF 凭据

nvmet 建盘时控制面自动签发 DHHC-1 密钥。以下命令用于显式轮换与吊销：

```bash
kurrent workers credential show storage-worker-01
kurrent workers credential set storage-worker-01 --generate
kurrent workers credential set storage-worker-01 --secret-file /secure/dhhc1.key
kurrent workers credential revoke storage-worker-01
```

`--generate` 就地生成 32 字节密钥，不打印明文；已有密钥用 `--secret-file`（须 0600）或 `--secret-stdin` 传入，不接受命令行明文 `--secret`。`set` 输出逐 Agent 推送状态；未送达项由对账线程自动重试。吊销后设备下次引导被拒绝。

## 输出与退出码

| 项 | 约定 |
| --- | --- |
| 默认输出 | 表格 |
| `-o json` | 原始 JSON，便于 jq 处理 |
| 空字段 | 以 `—` 占位 |
| 成功/帮助 | 退出码 0 |
| 运行失败 | 退出码 1，错误写入 stderr |
| 用法错误 | 退出码 2，打印该命令用法 |

位置参数与 `--key value`、`--key=value`、`--flag` 可任意混排。所有命令支持 `-h`/`--help`；未知标志、缺值、重复标志与多余位置参数均报用法错误。

## 常见错误

| 现象 | 排查 |
| --- | --- |
| `No management credentials found` | 先跑 `kurrent init`；或 `credentials install` 刷新快照；或显式给 `--server` + `--token` |
| mTLS 证书错误 | 凭据文件与服务端不匹配；重新执行 `kurrent init` 或 `credentials install` |
| `config validate` 失败 | 按报错修正声明；未知字段会被拒绝 |
| `init` 报状态根属主不符 | 按提示执行 `sudo chown -R "$(id -u):$(id -g)" <路径>`，不要以 root 重跑 |
| `init` 报旧布局状态 | 确认迁移后加 `--adopt-state` 重跑 |
| `join` 停在宿主预检 | 先跑 `kurrent host check --role storage` 定位，再按提示执行 `host-prepare.sh` |
| `join` 报缺少控制面信任锚 | 用 `--cp-ca` 提供控制面的 `server.crt`（见部署页分机步骤） |
| token 无效或已过期 | 重新 `token create`；确认 `--agent-id` 与节点状态文件 `<state>/storager/state/agent.id` 一致 |

## 相关页面

- [部署](../guide/deployment.md)
- [日常运维](../guide/operations.md)
- [配置参考](./configuration.md)
- [API 参考](./api.md)
