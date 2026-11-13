# 日常运维

本页面向已完成部署并进入系统交付阶段的环境，覆盖声明修改、身份与凭据、设备与节点生命周期、升级回滚、备份与可观测性。首次部署参见[部署](./deployment.md)；跨组件的故障定位见[故障排查](./troubleshooting.md)。

## 配置修改

### 控制面

```bash
vim control_plane/kurrent.yaml
kurrent config validate     # 写盘前全量校验；失败不改动任何文件
kurrent init                # 应用并收敛；健康检查失败自动回滚上一份声明
kurrent config status       # 生效声明、configHash、dnsmasq.conf 一致性、证书漂移
```

`configHash` 是生效声明的规范化 SHA-256。核对运行态是否加载了某份声明时，比较 `config status` 的 `configHash` 与 `config show` 的输出，不依赖文件时间。

常见修改与生效方式：

| 修改 | 生效方式 | 说明 |
| --- | --- | --- |
| `spec.networking` | `kurrent init` | 重新生成 `dnsmasq.conf` 并重启 dnsmasq |
| `spec.auth` / `spec.registration` / `spec.credentials` | `kurrent init` | 下次请求或后台周期任务读取新值 |
| `spec.pki` | `kurrent init` | CRL 重签周期与 token 上限随之变化 |
| `spec.serverCert.san` / `days` | `kurrent init` | 默认只报告漂移，不改变 TOFU 指纹 |
| `spec.serverCert.rotateOnChange: true` | `kurrent init` | 重签证书；已入池设备需要重新信任 |
| `spec.boot` | `kurrent init` | 影响菜单超时与 `/boot-vars` 默认行为 |

`$STATE/control-plane/dnsmasq/dnsmasq.conf` 不得手工编辑：它是 `spec.networking` 的派生物，控制面启动时会重新生成。`$STATE` 指部署记录中的状态根（默认 `~/.local/state/kurrent`），可用 `kurrent deployment show` 查看。

### 存储节点

```bash
vim storager/kurrent.yaml
kurrent config validate --node
kurrent join https://<控制面地址>    # 已有 bootstrap/agent.token 时可省略 --token
```

`kurrent join` 会重新校验声明、按 `diskDir`、节点状态根与信任锚注入 compose 变量并重启 agent（nvmet 后端同时重启 nvmet-host）。修改 `spec.agent.diskDir` 会改变盘文件目录；修改 `spec.agent.advertiseUrl` 会改变控制面访问 Agent 的地址；修改 `spec.agent.backend` 会切换编排目录（nvmet / iscsi）。变更目录前先确认旧目录中的盘文件如何迁移。

节点身份保存在 `<state>/storager/state/agent.id`，组件证书、引导凭据与日志同在 `<state>/storager/` 下，备份需覆盖整个节点状态根。身份丢失后无法从声明恢复：删除 `agent.id`，用 `kurrent token create` 签发新 token，再以新身份重跑 join。控制面信任锚是 `<state>/storager/certs/cp-ca.crt`；服务器证书轮换后更新该文件（或 join 时用 `--cp-ca`）并重启节点组件。

### 服务器证书漂移

`config status` 的 `derived.serverCert.drift` 列出与声明不一致的 SAN 或天数。默认行为是**只报告**，不改变证书与已固定的 TOFU 指纹。需要真正轮换时：

1. 确认所有设备可以接受重新信任（或已计划重新预置）。
2. 设置 `spec.serverCert.rotateOnChange: true`，执行 `kurrent init`。
3. 新证书指纹变化，已入池设备在下一次 HTTPS 握手时拒绝旧证书；需要重新预置 NVRAM 指纹。
4. 完成后可以恢复为 `false`。

## 身份与凭据

### 管理入口

```bash
kurrent token create --usage admin                 # 管理 token（默认 24h，可 --scope/--ttl）
kurrent token list                                 # 未过期 token 台账
kurrent token revoke <token_id>                    # 立即失效

kurrent sessions list                              # 会话台账
kurrent sessions revoke <session_id>               # 逐个下线
kurrent sessions revoke-all                        # 全部下线
```

管理证书轮换：记录当前序列号（`kurrent cert list`）→ 停控制面后删除 `<state>/control-plane/state/pki/users/admin/client.crt` 与 `client.key` → 启动控制面（自动重签并重写 `admin.conf`）→ 用新凭据吊销旧序列号 `kurrent cert revoke admin --serial <旧序列号> --reason superseded`（握手立即生效）→ `kurrent credentials install` 刷新 `~/.kurrent` 快照。`kurrent credentials show` 可查看当前使用的 `admin.conf`。

### 人员证书

```bash
kurrent cert keygen alice --out ./certs            # 本地生成私钥与 CSR
kurrent cert issue alice --csr ./certs/alice.csr   # 需先在 state/users.yml 登记
kurrent cert list
kurrent cert revoke alice [--serial HEX]           # 重签 CRL + reload nginx + 踢会话
```

人员证书由 users CA 签发，吊销在下一次 TLS 握手生效。WebUI「身份与访问」页提供同一组操作的界面入口。

### 组件证书

组件通道（443 enroll、4840 入站、控制面出站、agent → nvmet-host UDS）在 TLS 链校验之外做应用层吊销校验。

```bash
kurrent cert components                            # agent / nvmet-host 证书
kurrent cert revoke-component <agent_id>           # 写 nodes CRL + 删除该节点 bootstrap token
```

- 控制面生成两张组件 CRL：`state/pki/nginx/nodes.crl`（nodes CA，校验同伴组件）与 `control.crl`（control CA，校验控制面客户端）；nginx 另用 `clients.crl` 管管理面握手。
- 组件用自身 client cert 从 `/api/cp/enroll/crl` 取表，默认每 60 秒刷新（`KURRENT_COMPONENT_CRL_REFRESH_SECONDS`）；控制面出站与 agent → nvmet-host 在每次建连时加载 `nodes.crl` 并检查 `nextUpdate`。
- **fail-closed**：CRL 缺失或过期时组件/出站连接直接拒绝，不降级放行；`nodes.crl` / `control.crl` 的 `nextUpdate` 为 30 天，容忍控制面长时间不可达。
- 吊销入口：`DELETE /api/cp/pki/components/{agent_id}`（nodes 域）与 `DELETE /api/cp/pki/control/{cn}`（control 域），均可加 `serial` 精确到一张。
- **升级顺序**：先升级并重启控制面（生成 CRL、开放分发端点），再重建组件容器；旧组件不认识 `/enroll/crl` 时会保留原有链校验行为，不会因此拒绝启动。
- 生效时间 = 控制面写入 CRL 后的下一次组件刷新（≤60 秒），无需重启组件。
- **启动自检**：被吊销的证书无法完成 `/enroll/crl` 的 mTLS，组件改为在启动时从公开只读入口 `GET /crl/nodes.crl`（无认证、仅 GET/HEAD）取表，用本地 nodes CA 校验签名后比对 client / serving 序列号，命中即用引导凭据重新 enroll（`KURRENT_PKI_SELF_CHECK=0` 可关闭）。
- **管理入口**：WebUI「身份与访问」页按 users / nodes / control 三个域展示到期与状态，可吊销人员与节点证书；节点证书异常时同一行提供「恢复」。

吊销后的节点恢复：

1. 控制面：WebUI「身份与访问」→「存储节点」→「恢复」，或
   `kurrent token create --agent-id <id> --cp-url https://<cp-host>`。WebUI 预填控制面
   地址，签发后显示一次性 token 与 join 命令（明文仅本次可见）。
2. 节点：执行输出的 `kurrent join <cp-url> --token <token> --agent-id <id>`。
3. 启动自检：agent 发现本地 client / serving 序列号命中 nodes CRL，用新 token 重新
   enroll 并覆盖本地证书；随后 join 等待派生凭据写出并自动重启 nvmet-host。

验证：

```bash
# 含组件 CRL 分发、公开 nodes CRL、control CA 台账与人类证书 403 断言
bash tests/deploy/probe_edge.sh
docker logs storager-agent 2>&1 | grep 'crl:'
docker exec kurrent-nvmet-host python /app/healthcheck.py; echo $?
```

`docker logs` 应出现 initial fetch ok（revoked 计数与 next_update），healthcheck 返回 0。

### NVMe-oF 凭据

```bash
kurrent workers credential show worker-01          # 密钥哈希前缀 + 逐 Agent 推送状态
kurrent workers credential set worker-01 --generate  # 轮换（不打印明文）
kurrent workers credential revoke worker-01        # 吊销：下次引导被拒绝
```

nvmet 后端建盘时自动签发，上述命令用于显式轮换与吊销。推送失败由后台对账重试；`spec.credentials.reconcileIntervalSeconds` 控制周期，`rotationHours` 控制定期轮换（默认 24 小时，吊销是主要处置手段）。

## 设备与 Worker 生命周期

- **注册新设备**：WebUI → Devices → 开启注册窗口；仅窗口期内、带有效 TPM AK 的设备入池。
- **绑定**：Devices / Workers 页；设备与 Worker 一对一；已绑定设备需显式换绑。
- **换绑（冷迁移）**：Devices 页 `force` 换绑；新绑定落盘后清旧绑定；系统盘留在原 Worker。
- **解绑**：Devices 页；设备回池，Worker 保留系统盘。
- **排空存储节点**：Agents 页停用；不再接收新盘，存量盘继续服务。
- **删除 Worker**：Workers 页；联动解绑设备、清理凭据条目与推送台账；盘文件按选项处理。
- **清理吊销设备**：Devices 页 / `DELETE /devices/revoked`；删除 `revoked` 条目，不作用于在用设备。

换绑前确认目标 Worker 的系统盘已有可用凭据，并确保原设备已关机或从数据面隔离。当前控制面不会在换绑时校验目标凭据是否就绪，也不会自动围栏原设备；Kurrent 不提供活迁移与数据面 fencing，不能把同一系统盘同时挂到两台设备。

## 升级与回滚

1. 备份声明、状态根（`$STATE`）与 `~/.kurrent/`（见下节）。
2. 更新代码后先升级控制面：重建镜像、`kurrent init`，确认 `/healthz` 与 `config status`。
3. 再重建存储节点组件（`kurrent join`），确认 `agents list` 为 `ok`。
4. 最后更新 WebUI 静态资源：`(cd webui/app && npm ci && npm run build)`（`dist` 不入库）；固件有变更时同步更新。
5. 执行部署探针：`bash tests/deploy/probe_edge.sh`。

控制面服务端迁移是启动时执行的幂等操作，但升级前仍应备份。回滚使用上一版本的代码重建镜像并重跑 `kurrent init`；声明文件与状态根保留。若新版本已写入旧版本不认识的字段，回滚前先恢复备份。

### 仓库移动

状态根与仓库解耦，移动或改名 checkout 不需要迁移状态：

1. 停止在新旧路径上都可能触发编排的进程；确认没有正在执行的 `kurrent init` / `kurrent join`。
2. 移动或重命名仓库目录。
3. 在新位置执行 `kurrent init --dir <新仓库根>`；命令按部署记录复用状态根与固定项目名，以 `--force-recreate` 重建容器并更新记录中的 `repoRoot`。
4. 执行 `kurrent config status` 与 `kurrent host check`，确认服务与挂载源正常。

存储节点按同样步骤用 `kurrent join <cp-url> --dir <新仓库根>` 收敛。不要手工编辑 `~/.kurrent/deployment.yaml`：仓库根、状态根与项目名三者必须一致，写错会让下一次 init 认错部署。

## 备份与恢复

需要备份：

| 路径 | 内容 | 权限 |
| --- | --- | --- |
| `control_plane/kurrent.yaml` | 控制面声明（随仓库） | 644 |
| `storager/kurrent.yaml` | 节点声明（随仓库） | 644 |
| `$STATE/control-plane/state/` | 台账、凭据、审计、会话、证书台账、PKI 与 TOFU 证书 | 目录 700，文件 600 |
| `$STATE/control-plane/config/agents.yml` | Agent 台账 | 644 |
| `$STATE/control-plane/dnsmasq/` | 渲染配置与租约（可再生） | 644 |
| `$STATE/storager/` | 节点身份、引导凭据、组件 PKI、日志与信任锚 | 目录 700，私钥 600 |
| `~/.kurrent/deployment.yaml` | 部署记录（仓库根、状态根、项目名） | 600 |
| `~/.kurrent/admin.conf` 与 `pki/` | 管理凭据快照（可由 `kurrent init` 重建） | 600 |

状态根含私钥与凭据明文，备份必须加密并限制访问。`control-plane/dnsmasq/` 与 `~/.kurrent/` 中的凭据可以再生产，完整备份以 `control-plane/state/` 与节点状态根为主。恢复流程：

1. 停止控制面与存储节点组件。
2. 恢复声明与状态根到部署记录中的原路径，保持原有权限；部署记录一并恢复。
3. `kurrent init`，确认 `configHash` 与备份时一致。
4. 存储节点按需重跑 `kurrent join`，确认 `agents list`。
5. 若丢失 `state/` 下的 PKI 或 `certs/`，所有组件需要重新 enroll，设备需要重新信任服务器证书；这不是无损恢复。

## 可观测性

- **控制面健康**：`GET /healthz`（经 443）；启动期派生件失败时返回 503。
- **声明状态**：`kurrent config status`；生效声明、configHash、dnsmasq 一致性、证书漂移、启动错误。
- **Agent 健康**：`kurrent agents list` / WebUI Agents；控制面到 Agent 的连通性。
- **引导链状态**：WebUI「引导链」/ `GET /boot-assets`；固件能力、脚本链与菜单完整性。
- **操作审计**：WebUI Operations / `kurrent ops list`；注册、绑定、建盘、凭据等操作轨迹。
- **请求审计**：`$STATE/control-plane/state/audit.jsonl`；主体、issuer、serial、路由、决策与告警。
- **告警外投**：`KURRENT_CP_ALARM_WEBHOOK`；`event=alarm` 事件 POST 到外部地址。

审计写入失败时请求返回 503（fail-closed）。告警外投走后台队列，不进入请求路径。

## 安全基线

```bash
sudo kurrent host check --role all    # 同机部署；分机时分别用 control / storage
```

- 控制面宿主只暴露 22、53/67/69、80/443；存储节点另有 4840/4421。
- 输出为 `ok` / `warn` / `fail` / `skip`；exit 0 通过、1 有 FAIL、2 用法错误。非 root 时防火墙类检查为 `skip`，`--strict` 将 `skip` 也计为失败。
- nvmet-host 管理面走共享卷 UDS，不产生 TCP 监听。
- 控制面容器不挂载 `docker.sock`。
- CA 私钥、`users.yml`、`sessions.yml`、`admin.conf`、`audit.jsonl` 应为 600。
- Docker 发布端口不受 ufw 过滤；需要入站限制时在 `DOCKER-USER` 链收口。
- PXE 网段按[部署](./deployment.md)页所述的边界隔离，并启用 DHCP snooping、动态 ARP 检测与端口安全。
