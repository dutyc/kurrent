# 部署前置

部署前需要满足以下主机、网络与设备前提。部署步骤见[部署](../setup/deployment.md)，端口表见[架构总览](../concepts/architecture/index.md#端口)，安全边界见[安全模型](../concepts/architecture/security.md)。

## 主机角色

| 角色 | 最低要求 | 职责 |
| --- | --- | --- |
| 控制面服务器（×1） | Linux x86_64；Docker 与 Compose v2；Node.js 22.23.4+、24.22.1+ 或 26.12.1+ 与 npm（构建 WebUI）；一个 PXE 侧接口（独立网卡或 VLAN 子接口） | DHCP/TFTP、443 WebUI/API、PKI、设备台账与引导变量 |
| 存储节点（×1 起） | Linux x86_64；Docker 与 Compose v2；NVMe/TCP TLS 内核能力；`tlshd >= 1.5.0`；一块 btrfs/xfs 数据盘 | Agent（4840）与 nvmet-host（4421），承载母盘与系统盘 |
| 裸机设备（×N） | x86_64 UEFI；TPM 2.0；可开启 Secure Boot；UEFI 网络启动（SNP），要求见下文 | 通过 PXE 入池并启动系统盘 |

同机部署时角色不合并，仍运行一个 Agent。

控制面必须有一个 PXE 网段地址；WebUI、API 与 Agent 回连走管理网地址。

裸机设备只支持 UEFI 引导；Legacy/CSM 不再支持（产物只有 UEFI 形态）。

## 裸机设备要求

以下要求针对 `main` 线；无 TPM 设备使用[仓库与分支](../reference/branches.md)中的 `legacy/no-tpm` 线，不要求 TPM 与 Secure Boot。`main` 线四项必须同时满足：

| 项 | 要求 | 不满足时 |
| --- | --- | --- |
| 架构与固件 | x86_64；仅 UEFI 引导，需在固件设置中关闭 CSM/Legacy | 没有对应产物，无法进入引导链 |
| TPM | TPM 2.0（独立 TPM 或 Intel PTT / AMD fTPM）；可创建 ECC P-256 或 RSA-2048 的 AK；SHA-256 PCR 0/2/4/7 可读 | 注册期拒绝入池（`no_tpm`）；证明失败不下发凭据 |
| Secure Boot | 未锁厂商密钥（Setup Mode 或可关闭）；首次网络启动经平台注册窗口写入 PK / KEK / db，db 含站点签名证书与 Microsoft UEFI CA | 注册期拒绝入池（`sb_disabled`）；未签名首跳不执行 |
| 网络引导 | 支持 UEFI IPv4 网络启动（DHCP + TFTP），网卡提供 UEFI SNP 栈 | 无法下载首跳；网卡无 UEFI SNP 时改用全驱动首跳（设备 `boot_firmware=full`） |

- 本地磁盘不是前提；设备不持有持久状态。
- CPU 与内存只需满足所装发行版要求。
- AK 绑定主板与 TPM：清 TPM 或换主板等同换设备，需删登记后重新入池。
- 不用 USB 或无线网卡引导；网卡兼容性见[固件平台参考](../reference/firmware-platform.md)。
- 平台密钥写入见 [预置目标机信任库](../tasks/enroll-platform-keys.md)；出厂已锁厂商密钥的机型经其厂商的管理流程处理。

在目标机核对：

```bash
ls /sys/firmware/efi                           # UEFI 引导
mokutil --sb-state                             # Secure Boot 状态
tpm2_getcap properties-fixed | grep -i family  # TPM 版本
tpm2_getcap pcrs | grep -i sha256              # SHA-256 PCR 银行
```

SNP 栈只能从 UEFI 设置或厂商文档确认；命令需要 `mokutil` 与 `tpm2-tools`。

## 网络与端口

- **管理面**：浏览器与存储节点访问 443。
- **引导面**：设备访问 DHCP（67/UDP）、TFTP（69/UDP）与引导 HTTPS（4443）；`spec.networking.interface` 绑定这一侧。

两侧建议用独立网卡或 VLAN；单网卡时 PXE 网段必须隔离。

设备必须能路由到存储节点 4421。完整端口表见[架构总览](../concepts/architecture/index.md#端口)。

## 主机依赖

两台主机需要 Docker、Compose v2、`git`、`curl`、`jq`、`openssl` 与 Go 1.27+（构建 CLI）；控制面另需 Node.js 22.23.4+、24.22.1+ 或 26.12.1+ 与 npm。安装命令见[部署](../setup/deployment.md#主机依赖)。

## 存储节点内核与 tlshd

- 内核需 `CONFIG_NVME_TCP_TLS`、`CONFIG_NVME_TARGET_TCP_TLS` 与 `CONFIG_NVME_KEYRING`。
- `tlshd >= 1.5.0` 且在运行；低版本会让旧 PSK 遮蔽新 PSK。
- 包版本低于 1.5.0 时按 `storager/nvmeof/nvmet-host/README.md` 源码安装。
- 4421 建口失败时节点上报未就绪，控制面拒绝引导，不回落明文。

`host-prepare.sh` 需 root，幂等加载模块、挂载 configfs 并持久化；诊断用 `kurrent host check --role storage`。

## 数据盘

- btrfs/xfs 支持 reflink，克隆接近秒级；ext4 回退全量拷贝。
- 写入 `/etc/fstab`（UUID）或 systemd mount，否则 `diskDir` 落到根文件系统。

## 宿主基线

控制面只应暴露 22、53/67/69、80/443；存储节点另有 4840/4421。

- 控制面管理面无 TCP 监听；nvmet-host 走共享卷 UDS。
- 控制面容器不挂载 `docker.sock`。
- CA 私钥、`users.yml`、`sessions.yml`、`admin.conf`、`audit.jsonl` 为 600。
- ufw 不过滤 Docker 发布端口；入站限制在 `DOCKER-USER` 链集中配置。

`sudo kurrent host check`（分机用 `--role control|storage`，同机用 `all`）；`--strict` 把 SKIP 计为失败。完整检查项见[安全检查清单](../setup/deployment.md#安全检查清单)与[日常运维](../tasks/operations.md#安全基线)。

## 接下来

- [部署](../setup/deployment.md)
- [架构总览](../concepts/architecture/index.md#端口)
- [安全模型](../concepts/architecture/security.md)
- [配置参考](../reference/configuration.md)
