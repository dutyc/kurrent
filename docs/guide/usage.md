# 使用流程

> 前提：[部署指南](./deployment) 已完成，控制面与存储节点 `health=ok`，裸机设备已接入 PXE 网段。

## 1. 首次开机与入池

设备网络启动后：DHCP → TFTP 拉取 iPXE 与 `boot.ipxe` 指针 → iPXE 经 HTTPS 取回 `boot.ipxe.cfg` → 首次 `keygen` 生成 ECDSA P-256 私钥（留在 EFI NVRAM，不出机器）→ `/devices/report` 上报指纹与公钥。

设备**只有在注册窗口开启且带有效公钥时**才会自动入池：

1. WebUI → **Devices** → 开启注册窗口（TTL 1–60 分钟，可选放行额度 `max_devices`）。
2. 设备出现在池中（`pooled`）；已入池的存量设备在窗口期内带公钥上报即完成密钥认领。
3. 全部设备认领后，可开启**验签强制**：此后的 `/boot-vars` 只接受通过挑战-应答验签的已绑定设备。

> 注册窗口关闭后上报只更新指纹、不再入池；没有窗口外的注册通道。

## 2. 绑定 Worker

设备与 Worker 严格**一对一**：

- **Devices 页**：选择设备 → 绑定到已有 Worker；已绑定设备默认拒绝，需显式 `force` 换绑（原子：新绑定落盘后再清旧绑定）。
- **Workers 页**：新建 Worker 时填入 MAC，创建即完成绑定；不填 MAC 则得到一台纯空转 Worker，之后再绑设备。

绑定权威是设备台账的 `bound_worker_id`。dnsmasq 的主机名只是绑定操作的派生投影，更新可能滞后，**不参与引导身份判定**。

解绑后设备回池，Worker 的系统盘保留。

## 3. 创建系统盘

先确认存储节点本地已有可用的**母盘**：盘文件放在节点的 `diskDir` 下，文件名包含 `_tpl_` 标记（如 `_tpl_ubuntu2604.img`），后台扫描后出现在母盘列表；母盘需为 4K 原生 GPT。

在 **Workers** 页选中 Worker → 创建系统盘：

- 选择母盘与 `os` / `os_version`（盘的展示与分组元数据）；同一 Worker 的同一 `(os, os_version)` 至多一块。
- 放置由控制面决定：候选节点须持有该母盘、健康且未排空；现有系统盘最少的节点优先；可在计划中逐行 pin 手动覆盖。
- 批量模式：勾选多个 Worker → 批量创建系统盘 → 先预览计划（`dry_run`，与执行同一套逻辑）→ 确认执行。

控制面为每块盘生成 12 位 hex 的 `os_tag` 与完整盘 NQN。nvmet 后端在建盘时**自动签发 DHHC-1 认证密钥**并推送到持有该盘的 Agent。

## 4. 设置默认启动盘

Worker 详情 → 系统盘 → **设为默认**。默认盘决定：

- iPXE 菜单超时后自动选中哪一项；
- `/boot-vars` 投影哪块盘的连接信息（完整 `disk_nqn`、`storager_ip`、TLS PSK 与端口）。

未设置默认盘时，控制面返回 `menu-default=reboot`（1 ms 超时循环重启），避免静默进入错误的系统。同一 Worker 可挂多块系统盘，菜单里可手动选择其他盘。

## 5. 开机链

```text
DHCP → TFTP（snponly.efi + boot.ipxe 指针）→ iPXE
  → HTTPS boot.ipxe.cfg        变量与设备身份链入口
  → /devices/report            设备信息与公钥上报
  → /devices/challenge         一次性 nonce
  → sign(nonce‖mac)            设备私钥签名
  → /boot-vars                 验签通过后下发 disk_nqn / storager_ip / hostnqn / TLS PSK 与端口
  → menu.ipxe                  超时选择默认盘
  → sanboot nvme://<storager>:4421/<disk_nqn>   TLS 1.3 + DH-HMAC-CHAP
  → GRUB → 操作系统
```

TFTP 只承载首跳固件 `snponly.efi` 和 `boot.ipxe` 指针；`boot.ipxe.cfg` 起全部走 HTTPS + TOFU。

数据面只有 `4421` 一个端口，固件段与内核段共用；缺凭据、缺 PSK 或端口未就绪都会**拒绝引导**，不会回落明文。

## 6. 凭据与加密姿态

凭据按 Worker 跟盘，轮换/吊销即时生效：

```bash
kurrent workers credential show worker-01       # 密钥哈希前缀、逐 Agent 推送状态
kurrent workers credential set worker-01 --generate   # 轮换（就地生成，不打印明文）
kurrent workers credential revoke worker-01     # 吊销：下次启动拒绝引导
```

TLS PSK 由认证密钥派生，因此轮换/吊销认证密钥即原子地轮换/吊销 PSK。推送失败由控制面周期对账自动重试，状态在 Worker 详情「NVMe-oF 凭据」页可见。

密钥明文只出现在链路必要范围（控制面与 Agent 的 0600 状态文件、存储节点 configfs 与内核 `.nvme` keyring），不引入应用层加密；磁盘与宿主访问边界由部署方负责。

## 7. 日常操作

| 操作 | 位置 | 语义 |
| --- | --- | --- |
| 删除系统盘 | Worker 详情 → 系统盘 | 可保留或删除盘文件；若是默认盘，`default_disk` 自动清除 |
| 删除 Worker | Workers 页 | 解绑设备、清除该 Worker 的凭据条目与推送台账；盘文件按选项处理 |
| 节点排空 | Agents 页 → 停用 | `enabled=false`：不再接收新盘，存量盘照常服务 |
| 审计 | `kurrent ops list` | 设备注册/认领、绑定、建盘、凭据签发/推送/吊销等操作记录 |
