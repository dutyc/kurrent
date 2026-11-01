# 日常运维

本页面向已完成部署并进入系统交付阶段的环境，覆盖声明修改、身份与凭据、设备与节点生命周期、升级回滚、备份与可观测性。首次部署参见[部署](./deployment.md)。

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

`dnsmasq/dnsmasq.conf` 不得手工编辑：它是 `spec.networking` 的派生物，控制面启动时会重新生成。

### 存储节点

```bash
vim storager/kurrent.yaml
kurrent config validate --node
kurrent join https://<控制面地址>    # 已有 bootstrap/agent.token 时可省略 --token
```

`kurrent join` 会重新校验声明、同步 `.env` 并重启 agent。修改 `spec.agent.diskDir` 会改变盘文件目录；修改 `spec.agent.advertiseUrl` 会改变控制面访问 Agent 的地址；修改 `spec.agent.backend` 会切换编排目录（nvmet / iscsi）。变更目录前先确认旧目录中的盘文件如何迁移。

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

管理证书：`kurrent credentials show` 查看当前使用的 `admin.conf`；轮换后重新执行 `kurrent credentials install` 刷新用户级快照。

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

nvmet 后端建盘时自动签发，上述命令用于显式轮换与吊销。推送失败由后台对账重试；`spec.credentials.reconcileIntervalSeconds` 控制周期，`rotationDays` 控制定期轮换。

## 设备与 Worker 生命周期

- **注册新设备**：WebUI → Devices → 开启注册窗口；仅窗口期内、带有效公钥的设备入池。
- **绑定**：Devices / Workers 页；设备与 Worker 一对一；已绑定设备需显式换绑。
- **换绑（冷迁移）**：Devices 页 `force` 换绑；新绑定落盘后清旧绑定；系统盘留在原 Worker。
- **解绑**：Devices 页；设备回池，Worker 保留系统盘。
- **排空存储节点**：Agents 页停用；不再接收新盘，存量盘继续服务。
- **删除 Worker**：Workers 页；联动解绑设备、清理凭据条目与推送台账；盘文件按选项处理。
- **清理吊销设备**：Devices 页 / `DELETE /devices/revoked`；删除 `revoked` 条目，不作用于在用设备。

换绑前确认目标 Worker 的系统盘已有可用凭据，并确保原设备已关机或从数据面隔离。当前控制面不会在换绑时校验目标凭据是否就绪，也不会自动围栏原设备；Kurrent 不提供活迁移与数据面 fencing，不能把同一系统盘同时挂到两台设备。

## 升级与回滚

1. 备份声明、`state/`、`pki/` 与 `certs/`（见下节）。
2. 更新代码后先升级控制面：重建镜像、`kurrent init`，确认 `/healthz` 与 `config status`。
3. 再重建存储节点组件（`kurrent join` 或 `docker compose up -d`），确认 `agents list` 为 `ok`。
4. 最后更新 WebUI 静态资源与固件（如有变更）。
5. 执行部署探针：`bash tests/deploy/probe_edge.sh`。

控制面服务端迁移是启动时执行的幂等操作，但升级前仍应备份。回滚使用上一版本的代码重建镜像并重跑 `kurrent init`；声明文件与状态目录保留。若新版本已写入旧版本不认识的字段，回滚前先恢复备份。

## 备份与恢复

需要备份：

| 路径 | 内容 | 权限 |
| --- | --- | --- |
| `control_plane/kurrent.yaml` | 控制面声明 | 644 |
| `storager/kurrent.yaml` | 节点声明 | 644 |
| `control_plane/state/` | 台账、凭据、审计、会话、证书台账 | 目录 700，文件 600 |
| `control_plane/state/pki/` | 三类 CA、组件证书、admin 凭据 | 目录 700，私钥 600 |
| `control_plane/state/certs/` | TOFU 服务器证书与指纹 | 目录 700，私钥 600 |
| `storager/bootstrap/` | 节点引导凭据 | 目录 700，文件 600 |

`state/` 含私钥与凭据明文，备份必须加密并限制访问。恢复流程：

1. 停止控制面与存储节点组件。
2. 恢复声明与状态目录，保持原有权限。
3. `kurrent init`，确认 `configHash` 与备份时一致。
4. 存储节点按需重跑 `kurrent join`，确认 `agents list`。
5. 若丢失 `pki/` 或 `certs/`，所有组件需要重新 enroll，设备需要重新信任服务器证书；这不是无损恢复。

## 可观测性

- **控制面健康**：`GET /healthz`（经 443）；启动期派生件失败时返回 503。
- **声明状态**：`kurrent config status`；生效声明、configHash、dnsmasq 一致性、证书漂移、启动错误。
- **Agent 健康**：`kurrent agents list` / WebUI Agents；控制面到 Agent 的连通性。
- **引导链状态**：WebUI「引导链」/ `GET /boot-assets`；固件能力、脚本链与菜单完整性。
- **操作审计**：WebUI Operations / `kurrent ops list`；注册、绑定、建盘、凭据等操作轨迹。
- **请求审计**：`control_plane/state/audit.jsonl`；主体、issuer、serial、路由、决策与告警。
- **告警外投**：`KURRENT_CP_ALARM_WEBHOOK`；`event=alarm` 事件 POST 到外部地址。

审计写入失败时请求返回 503（fail-closed）。告警外投走后台队列，不进入请求路径。

## 安全基线

```bash
bash control_plane/host-baseline.sh
```

- 控制面宿主只暴露 22、53/67/69、80/443；存储节点另有 4840/4421。
- nvmet-host 管理面走共享卷 UDS，不产生 TCP 监听。
- 控制面容器不挂载 `docker.sock`。
- CA 私钥、`users.yml`、`sessions.yml`、`admin.conf`、`audit.jsonl` 应为 600。
- Docker 发布端口不受 ufw 过滤；需要入站限制时在 `DOCKER-USER` 链收口。
- PXE 网段按[部署](./deployment.md)页所述的边界隔离，并启用 DHCP snooping、动态 ARP 检测与端口安全。

## 故障排查

跨组件的现象定位、证据采集与恢复路径见[故障排查](./troubleshooting.md)。
