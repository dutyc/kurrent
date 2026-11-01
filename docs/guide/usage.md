# 首次交付

> 本页把一台已接入 PXE 网段的设备交付为可网络启动的系统：确认母盘 → 入池 → 绑定 Worker → 创建系统盘 → 设置默认盘 → 冷启动验收 → 管理凭据。前提是[部署](./deployment.md)已完成，控制面与存储节点 `health=ok`。

## 交付路径

按顺序完成七步，完成标志分别是：

1. 确认母盘：`diskDir` 中有 4K 原生 GPT 母盘，`GET /masters` 可见。
2. 设备入池：Devices 页显示 `pooled`，并完成密钥认领。
3. 绑定 Worker：设备与 Worker 一对一绑定。
4. 创建系统盘：母盘克隆完成，`os_tag` 已生成；nvmet 后端同时签发凭据。
5. 设置默认盘：Worker 的默认启动盘指向目标系统。
6. 冷启动验收：设备经 GRUB、内核、initramfs 到 rootfs，出现登录提示符。
7. 管理凭据：轮换/吊销可用，推送状态正常。

## WebUI 与 API

以下步骤均可通过 WebUI 完成，也可用控制面 REST API 自动化。**CLI 目前只提供 `workers list/get` 与凭据操作**，Worker 创建、设备绑定、建盘与默认盘请走 API 或 WebUI。

在控制面服务器、仓库根目录准备 API 调用（admin 证书即 `kurrent init` 自举的管理凭据）：

```bash
BASE_URL=https://192.168.1.10/api/cp
CERT=control_plane/state/pki/users/admin/client.crt
KEY=control_plane/state/pki/users/admin/client.key
CA=control_plane/state/certs/server.crt
api() {
  curl -sS --cert "$CERT" --key "$KEY" --cacert "$CA" \
    "$BASE_URL$1" "${@:2}"
}
```

> 这里假定 `spec.serverCert.san` 已包含控制面管理地址；否则按[部署](./deployment.md)补 SAN 并重新 `kurrent init`。临时排障可去掉 `--cacert` 并加 `-k`，不要把它当作长期配置。

## 1. 确认母盘

母盘不是通过 API 上传的：把盘文件放进存储节点的 `diskDir`，文件名包含 `_tpl_` 标记（如 `_tpl_ubuntu2404.img`），后台扫描后出现在母盘列表。母盘必须是 4K 原生 GPT；制作方法见[系统盘定制](./system-disk.md)。

```bash
api /masters | jq \
  '.agents[] | {agent, storager_ip, masters: [.masters[] | {name, gpt}]}'
```

逐项确认：

- `gpt` 为 `4k`。512 字节扇区 GPT 在 nvmet 的 4096 B 导出上会被发起端视为无分区，表现为引导错误 `0x7f22208e`。
- 存储节点为 `enabled=true` 且数据面就绪；放置策略只会选持有该母盘的节点。
- 母盘内不得写入机器身份、target/NQN、认证密钥或控制面地址。

`os` / `os_version` 是建盘时写入的展示与分组元数据，同一 Worker 的同一 `(os, os_version)` 至多一块盘。

## 2. 设备入池

如果设备在部署验收时已经入池，可直接进入下一步；批量新增设备时，先开启注册窗口再让设备网络启动。窗口必须在 `/devices/report` 之前开启。

```bash
# 开启窗口：30 分钟，最多放行 20 台新设备
api /settings/registration-window -X POST \
  -H 'Content-Type: application/json' \
  -d '{"ttl_minutes": 30, "max_devices": 20}'

# 查看窗口与已放行数量
api /settings/registration-window | jq
```

窗口已开启时，`POST` 返回 409；先查询状态，必要时关闭后重开。

设备网络启动后：DHCP → TFTP 拉取 iPXE 与 `boot.ipxe` 指针 → iPXE 经 HTTPS 取回 `boot.ipxe.cfg` → 首次 `keygen` 生成 ECDSA P-256 私钥（留在 EFI NVRAM）→ `/devices/report` 上报指纹与公钥。

```bash
# 确认设备出现在池中且已完成密钥认领
api /devices | jq
```

设备全部认领后关闭窗口，再开启验签强制：

```bash
api /settings/registration-window -X DELETE

# 仅在全部设备完成密钥认领后开启
api /settings/enforcement -X PUT \
  -H 'Content-Type: application/json' -d '{"enabled": true}'
```

> 注册窗口关闭后上报只更新指纹、不再入池；没有窗口外的注册通道。验签强制开启后，无密钥、无签名或验签失败的已绑定设备也会被拒绝引导。被吊销的设备不会因重新上报自动恢复。

## 3. 绑定 Worker

设备与 Worker 严格**一对一**。WebUI 有两种路径：

- **Devices 页**：选择设备 → 绑定到已有 Worker；已绑定设备默认拒绝，需显式 `force` 换绑（原子：新绑定落盘后再清旧绑定）。
- **Workers 页**：新建 Worker 时填入 MAC，创建即完成绑定；不填 MAC 则得到一台纯空转 Worker，之后再绑设备。

API 等价操作：

```bash
# 新建 Worker 并同时绑定设备（设备必须已在池中）
api /workers -X POST \
  -H 'Content-Type: application/json' \
  -d '{
    "worker_id": "worker-01",
    "hostname": "worker-01",
    "mac": "00:0c:29:b9:8b:2d"
  }'

# 或把已入池设备绑定到已有 Worker
api '/devices/00:0c:29:b9:8b:2d/bind?worker_id=worker-01&force=false' \
  -X POST
```

绑定权威是设备台账的 `bound_worker_id`。dnsmasq 的主机名只是绑定操作的派生投影，更新可能滞后，**不参与引导身份判定**。解绑后设备回池，Worker 的系统盘保留；换绑用 `force=true`，先确认原设备已关机或从数据面隔离。

## 4. 创建系统盘

在 **Workers** 页选中 Worker → 创建系统盘：

- 选择母盘与 `os` / `os_version`；同一 Worker 的同一 `(os, os_version)` 至多一块。
- 放置由控制面决定：候选节点须持有该母盘、健康且未排空；现有系统盘最少的节点优先；可在计划中逐行 pin 手动覆盖。
- 批量模式：勾选多个 Worker → 批量创建系统盘 → 先预览计划（`dry_run`，与执行同一套逻辑）→ 确认执行。

API 单盘创建：

```bash
api /workers/worker-01/luns/disk -X POST \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "master",
    "os": "ubuntu",
    "os_version": "24.04",
    "name": "_tpl_ubuntu2404.img"
  }'
```

- 省略 `disk_agent` 时由放置策略选节点；传 `disk_agent` 可把盘固定到某台存储节点。
- 响应包含 12 位 hex 的 `os_tag` 与完整盘 NQN。nvmet 后端在建盘时**自动签发 DHHC-1 认证密钥**并推送到持有该盘的 Agent。
- Worker 的第一块系统盘会自动设为默认盘；第二块及以后不会自动改写默认盘。

批量创建使用 `POST /workers/luns/disk/batch`，`dry_run=true` 只返回计划。完整字段与返回结构见 [API 参考](../reference/api.md)。

```bash
# 查看 Worker 的盘与 os_tag
api /workers/worker-01 | jq \
  '.disks[] | {os, os_version, os_tag, agent}'
```

## 5. 设置默认启动盘

单盘 Worker 在建盘时已自动设置默认盘，可跳过；多盘并存时指定要启动的那一块：

```bash
api /workers/worker-01/default-disk -X PUT \
  -H 'Content-Type: application/json' \
  -d '{"disk": "<os_tag>"}'
```

WebUI 路径：Worker 详情 → 系统盘 → **设为默认**。默认盘决定：

- iPXE 菜单超时后自动选中哪一项；
- `/boot-vars` 投影哪块盘的连接信息（完整 `disk-nqn`、`storager-ip`、TLS PSK 与端口）。

未设置默认盘时，控制面返回 `menu-default=reboot`（1 ms 超时循环重启），避免静默进入错误的系统。删除默认盘后，若仍有剩余系统盘，控制面自动提升其中一块为默认盘。

## 6. 首次系统启动与验收

重启设备并保持网络启动。预期链路：

```text
DHCP → iPXE → /boot-vars → NVMe/TCP TLS 1.3 连接 4421
     → GRUB → 内核 → initramfs（消费 NBFT/NBCT）→ rootfs → 登录提示符
```

**交付完成的判据是冷启动到登录提示符**，不是设备入池、不是出现 GRUB，也不是内核开始启动。只在某一阶段通过就报告完成，会把数据面或 initramfs 的问题留到下一次上电。

失败时的首要检查：

| 现象 | 检查 |
| --- | --- |
| 菜单为空或循环重启 | 设备是否绑定 Worker；Worker 是否有系统盘与默认盘；`/boot-vars` 是否投影完整 |
| 菜单后连不上数据面 | 客户端到存储节点 4421 的路由与防火墙；`tlshd` 状态；凭据推送状态 |
| 认证失败 | `kurrent workers credential show <worker>`；认证密钥是否已推送到持有该盘的 Agent |
| 引导错误 `0x7f22208e` | 母盘/系统盘 GPT 是否为 4K 原生；`GET /masters` 的 `gpt` 是否为 `4k` |

其余现象见[故障排查](./troubleshooting.md)。

## 7. 管理凭据

凭据按 Worker 跟盘，轮换/吊销即时生效：

```bash
kurrent workers credential show worker-01
kurrent workers credential set worker-01 --generate
kurrent workers credential revoke worker-01
```

`show` 显示密钥哈希前缀与逐 Agent 推送状态；`set --generate` 就地轮换且不打印明文；`revoke` 使下一次启动被拒绝。

TLS PSK 由认证密钥派生，因此轮换/吊销认证密钥即原子地轮换/吊销 PSK。推送失败由控制面周期对账自动重试，状态在 Worker 详情「NVMe-oF 凭据」页可见。管理面也提供对应 REST 端点，自动化场景见 [API 参考](../reference/api.md)；CLI 是交互操作的首选。

密钥明文只出现在链路必要范围（控制面与 Agent 的 0600 状态文件、存储节点 configfs 与内核 `.nvme` keyring），不引入应用层加密；磁盘与宿主访问边界由部署方负责。

## 相关页面

- [系统盘定制](./system-disk.md)
- [日常运维](./operations.md)
- [故障排查](./troubleshooting.md)
- [引导链](../concepts/boot-chain.md)
- [API 参考](../reference/api.md)
