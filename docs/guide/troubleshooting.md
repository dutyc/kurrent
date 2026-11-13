# 故障排查

本页按现象组织：先判断失败发生在哪一段边界，再进入对应组件检查。各任务页保留局部失败表；本页负责跨组件定位、证据采集与恢复路径。

## 先定位失败边界

- **浏览器或 CLI 无法登录**：管理面 443 / nginx / 控制面；`curl -k https://<控制面IP>/healthz`；nginx 日志。
- **能登录但 Agent 非 `ok`**：控制面 → Agent 4840；`kurrent agents list`；节点 `spec.agent.advertiseUrl`。
- **设备没有 DHCP 应答**：dnsmasq / PXE 网段；设备网段、`interface=`、防火墙 67/69。
- **有 DHCP 但无 TFTP 下载**：TFTP 根 / 69；TFTP 根文件与权限。
- **iPXE 卡在 4443 HTTPS**：boot CA / 4443 证书 / 端口；固件构建的 boot CA、叶证书 SAN 与端口。
- **设备不入池**：注册窗口 / TPM AK / Secure Boot / 吊销；Devices 页窗口状态与证明列；设备上报。
- **菜单为空或循环重启**：绑定 / 系统盘 / 默认盘；Worker 详情；`/boot-vars` 投影。
- **菜单后数据面连接失败**：4421 / tlshd / 凭据；`kurrent host check --role storage`；凭据推送状态。
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
- `init` 报状态根或 `~/.kurrent` 属主不符：按提示执行 `sudo chown -R "$(id -u):$(id -g)" <路径>`，不要以 root 重跑。
- `init` 报仓库内存在旧状态：确认迁移后加 `--adopt-state` 重跑；`bootstrapping` 记录与交换包保留时，修复后重跑从凭据交换继续。
- `init` 报状态根不存在而部署记录仍在：从备份恢复状态根，或删除 `~/.kurrent/deployment.yaml` 后重新部署；`kurrent deployment show` 可确认记录内容。

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
2. 确认设备上报携带有效 TPM AK：首次启动应执行 `akgen` 与 `akpub`，且 Secure Boot 已开启。
3. 确认设备未被吊销；`revoked` 条目不会因重新上报自动恢复。
4. 检查控制面审计与日志中是否有 `/devices/report` 记录。

窗口关闭后上报只更新指纹，不再入池。没有窗口外的注册通道。

## 引导被拒绝

| 检查 | 入口 |
| --- | --- |
| 设备是否已绑定 Worker | Devices 页；绑定权威是设备台账 |
| Worker 是否有系统盘与默认盘 | Worker 详情；无默认盘时控制面返回 `menu-default=reboot` |
| TPM 证明强制是否被关闭 | Devices 页注册窗口面板；默认开启，关闭后未通过证明的已绑定设备也可取得菜单变量（凭据仍不注入） |
| `/boot-vars` 是否返回完整连接信息 | `GET /boot-vars`，核对 `disk-nqn`、`storager-ip`、端口与 PSK |
| 设备 NVRAM 指纹是否与控制面证书一致 | 证书轮换后需重新预置 `server-fingerprint` |

## 数据面连接失败

```bash
kurrent host check --role storage
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

## 设备身份、TPM 证明与引导信任

设备身份是 TPM 2.0 AK，证明随 `/boot-vars` 提交。控制面把拒绝原因写入 `boot_vars.credential` 审计与设备记录的 `attestation.last_reason`，Devices 页的证明列与展开行显示状态和原因码。

| 原因码 | 含义 | 处置 |
| --- | --- | --- |
| `no_tpm` | 上报未携带有效 AK（无 TPM 或固件未接入 TCG2） | 确认目标机有 TPM 2.0 且固件已开启；Secure Boot 状态见下一行 |
| `sb_disabled` | 注册期上报 `sb=0`，Secure Boot 未开启 | 在固件设置中开启 Secure Boot，预置站点信任库后重新入池 |
| `window_closed` | 未知 MAC 在窗口关闭或额度用尽时上报 | 按需开启注册窗口后重新上报 |
| `ak_unregistered` | 设备尚无 AK 身份 | 在注册窗口内带 AK 上报完成认领 |
| `missing_mac` | `/boot-vars` 未带 MAC，主机名也无法定位设备 | 引导脚本传 `mac=${net0/mac}` |
| `nonce_invalid` | nonce 缺失、已使用、过期或与 MAC 不符 | 重新请求 `/devices/challenge` 后重试，不要复用旧 nonce |
| `quote_invalid` | quote 结构、extraData 或签名不符 | 确认固件 quote 对 PCR 0/2/4/7 做 SHA-256，且 extraData 原样等于 nonce |
| `pcr_mismatch` | PCR 摘要与平台基线不一致（固件或启动配置变更） | 核对变更后在设备详情点击「批准新基线」，设备下次启动重新证明 |
| `baseline_approved` | 基线刚被批准，等待下次证明 | 重启设备完成一次证明 |

- **引导 HTTPS 被拒**：服务器证书无法链到固件内嵌的 boot CA；核对固件构建用的 `BOOT_CA_CERT` 与控制面 `state/pki/ca/boot/ca.crt`，以及 4443 叶证书 SAN 是否覆盖设备访问的地址。
- **访问第二个 HTTPS 主机被拒**：boot CA 为全局单锚；统一引导 HTTPS 入口与证书，其余流量走 TFTP/HTTP。
- **TLS 握手失败**：服务端 TLS 高于 1.2 或套件不兼容；服务端限制到 TLS 1.2。
- **`${quote}` / `${quote-sig}` 为空**：TPM 命令失败或设备没有 TPM；检查固件日志，无 TPM 设备不入池。
- **`device.claim rejected reason=ak_mismatch`**：已登记的 AK 与本次上报不一致；AK 不可在线更换，删除登记后在注册窗口内重新入池。

## 收集证据

| 证据 | 位置 |
| --- | --- |
| 控制面启动与派生件 | `docker logs kurrent-control-plane`；`kurrent config status` |
| 管理面请求决策 | 状态根下 `control-plane/state/audit.jsonl`（`kurrent deployment show` 查看状态根） |
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
