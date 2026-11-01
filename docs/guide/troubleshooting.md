# 故障排查

本页按现象组织：先判断失败发生在哪一段边界，再进入对应组件检查。各任务页保留局部失败表；本页负责跨组件定位、证据采集与恢复路径。

## 先定位失败边界

- **浏览器或 CLI 无法登录**：管理面 443 / nginx / 控制面；`curl -k https://<控制面IP>/healthz`；nginx 日志。
- **能登录但 Agent 非 `ok`**：控制面 → Agent 4840；`kurrent agents list`；节点 `spec.agent.advertiseUrl`。
- **设备没有 DHCP 应答**：dnsmasq / PXE 网段；设备网段、`interface=`、防火墙 67/69。
- **有 DHCP 但无 TFTP 下载**：TFTP 根 / 69；TFTP 根文件与权限。
- **iPXE 卡在 HTTPS**：443 / 证书 / TOFU；服务器证书、NVRAM 指纹。
- **设备不入池**：注册窗口 / 公钥 / 吊销；Devices 页窗口状态；设备上报。
- **菜单为空或循环重启**：绑定 / 系统盘 / 默认盘；Worker 详情；`/boot-vars` 投影。
- **菜单后数据面连接失败**：4421 / tlshd / 凭据；`host-prepare.sh --check`；凭据推送状态。
- **系统启动后认证失败**：凭据轮换 / 对账；`kurrent workers credential show`。

## 控制面未就绪

```bash
docker logs kurrent-control-plane
kurrent config status
curl -k https://<控制面IP>/healthz
```

- `/healthz` 返回 503：启动期派生件失败，`config status` 会列出启动错误；修复声明后重跑 `kurrent init`。
- `init` 非零退出并报告回滚：新声明未通过启动检查；回滚只恢复声明文件，证书等派生物以 `config status` 为准。
- `configHash` 与 `config show` 不一致：运行态没有加载当前声明，重新执行 `kurrent init`。

## Agent 非 ok

```bash
kurrent agents list
docker logs storager-agent
docker logs kurrent-nvmet-host
```

| 检查 | 说明 |
| --- | --- |
| 控制面能否访问 `spec.agent.advertiseUrl` | 默认 `https://<节点IP>:4840`；检查防火墙与地址声明 |
| Agent 是否反复重启 | 常见原因是控制面 443 不可达，或 `bootstrap/agent.token` 缺失 |
| 证书是否被吊销 | Agent 启动自检失败会阻止服务；用新 bootstrap token 重新 `join` |

恢复被吊销节点：

```bash
kurrent token create --agent-id <agent_id>
# 在存储节点执行
kurrent join https://<控制面地址> --token <token> --agent-id <agent_id>
```

旧证书无需手工删除；agent 启动自检会重新 enroll。

## 设备不入池

1. 确认 Devices 页的注册窗口处于开启状态，且未超过 TTL 与放行额度。
2. 确认设备上报携带有效公钥：首次启动应执行 `keygen` 与 `pubkey`。
3. 确认设备未被吊销；`revoked` 条目不会因重新上报自动恢复。
4. 检查控制面审计与日志中是否有 `/devices/report` 记录。

窗口关闭后上报只更新指纹，不再入池。没有窗口外的注册通道。

## 引导被拒绝

| 检查 | 入口 |
| --- | --- |
| 设备是否已绑定 Worker | Devices 页；绑定权威是设备台账 |
| Worker 是否有系统盘与默认盘 | Worker 详情；无默认盘时控制面返回 `menu-default=reboot` |
| 验签强制是否开启 | 设置页；开启后只接受通过挑战-应答的已绑定设备 |
| `/boot-vars` 是否返回完整连接信息 | `GET /boot-vars`，核对 `disk-nqn`、`storager-ip`、端口与 PSK |
| 设备 NVRAM 指纹是否与控制面证书一致 | 证书轮换后需重新预置 `server-fingerprint` |

## 数据面连接失败

```bash
bash storager/nvmeof/host-prepare.sh --check
docker logs kurrent-nvmet-host
ss -tlnp | grep 4421
```

- 宿主内核需要 `CONFIG_NVME_TCP_TLS`、`CONFIG_NVME_TARGET_TCP_TLS`、`CONFIG_NVME_KEYRING`，且 `tlshd >= 1.5.0` 常驻。
- `/capabilities` 的 `port.ready` 应为 `true`；`4421` 未监听时查 nvmet-host 建口日志。
- 引导被拒绝且没有明文回退：检查凭据是否已推送到持有该盘的 Agent。

## 凭据未送达

```bash
kurrent workers credential show worker-01
```

输出包含密钥哈希前缀与逐 Agent 推送状态。推送失败由控制面周期对账重试；检查 `spec.credentials.reconcileIntervalSeconds`、Agent 连通性与节点是否持有该盘。

明文只应出现在控制面与 Agent 的 0600 状态文件、存储节点 configfs 与内核 `.nvme` keyring。需要进一步定位时使用哈希前缀与推送状态，不打印密钥。

## 证书被吊销

```bash
kurrent cert components
kurrent cert list
```

- 人员证书：`kurrent cert revoke <name> [--serial HEX]`，下一次 TLS 握手生效。
- 组件证书：`kurrent cert revoke-component <agent_id>`，写 nodes CRL 并删除该节点 bootstrap token。
- 恢复节点：签发新 token 后重新 `join`，见「Agent 非 ok」。

## 固件身份与 TOFU

- **`keygen: device key already exists`**：密钥已存在；正常幂等路径；确需重置时清 NVRAM。
- **`pubkey: no device key` / `sign: no device key`**：未生成密钥或 NVRAM 被清；先执行 `keygen`。
- **HTTPS 下载被拒**：服务器证书与已固定指纹不一致；确认证书是否轮换；轮换后清除 `server-fingerprint`。
- **访问第二个 HTTPS 主机被拒**：TOFU 为全局单锚；统一 HTTPS 入口与证书，其余流量走 TFTP/HTTP。
- **TLS 握手失败**：服务端 TLS 高于 1.2 或套件不兼容；服务端限制到 TLS 1.2。
- **`${pubkey}` / `${sig}` 为空**：命令失败或设置未写入；检查命令输出，两者仅当前启动内有效。
- **裸 `clear device-key` 后密钥仍在**：未带 `nvo/` 前缀；使用 `clear nvo/device-key` 或 `nvclear`。

## 收集证据

| 证据 | 位置 |
| --- | --- |
| 控制面启动与派生件 | `docker logs kurrent-control-plane`；`kurrent config status` |
| 管理面请求决策 | `control_plane/state/audit.jsonl` |
| 操作轨迹 | WebUI Operations / `kurrent ops list` |
| Agent 与数据面 | `docker logs storager-agent`、`docker logs kurrent-nvmet-host` |
| 引导链状态 | WebUI「引导链」/ `GET /boot-assets` |
| 部署探针 | `bash tests/deploy/probe_edge.sh` |

审计写入失败时请求返回 503（fail-closed）。提交问题时附上时间窗口、设备 MAC 与 Worker id，不要附带私钥、token、PSK 或 `admin.conf`。

## 相关页面

- [部署](../guide/deployment.md)
- [日常运维](./operations.md)
- [引导链](../concepts/boot-chain.md)
- [存储与数据面](../concepts/storage.md)
- [固件构建与部署](../firmware/build-deploy.md)
