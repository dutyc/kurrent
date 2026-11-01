# 发行版矩阵

固件仓库为每个发行版提供自包含的母盘定制脚本，统一按 A–F 六阶段执行。本页汇总已完成整机冷启动验收的发行版、验收判据与家族差异；母盘制作步骤见[系统盘定制](../guide/system-disk.md)。

## 已验证发行版

| 发行版 | 安装介质 | 无人值守 | initramfs 消费 | 内核 |
| --- | --- | --- | --- | --- |
| Debian 12（bookworm） | netinst | preseed | initramfs-tools + `nbft-connect` | 6.12（backports） |
| Debian 13（13.6） | netinst | preseed | initramfs-tools + `nbft-connect` | 6.12（自带） |
| Ubuntu 24.04 LTS | live-server | autoinstall | initramfs-tools + `nbft-connect` | 6.8 GA |
| Ubuntu 26.04 LTS | live-server | autoinstall | initramfs-tools + `nbft-connect` | 7.0 |
| Fedora 44 | Server DVD | kickstart | dracut `95nbft` + `nbft-connect` | 6.19 |
| AlmaLinux 10.2 | minimal ISO | kickstart | dracut `95nbft` + `nbft-connect` | 6.12 |
| openSUSE Leap 16.0 | online installer（Agama） | Agama profile | dracut `95nbft` + `nbft-connect` | 6.12 |

验收指完整链路：PXE 冷启动 → 固件段认证 → GRUB → 内核 → initramfs 消费 NBFT/NBCT → rootfs → 登录提示符。仅验证到 GRUB 或内核启动不构成验收。逐版本的安装介质版本、内核小版本与验收日期见固件仓库[发行版矩阵](https://github.com/dutyc/kurrent-firmware/blob/main/docs/golden-image-matrix.md)。

## 脚本与软件源

| 发行版 | 定制脚本 | 默认软件源 | 安装约束 |
| --- | --- | --- | --- |
| Debian 12 | `test/debian12/golden-image-setup-debian12.sh` | 阿里云（bookworm + backports） | ESP 不小于 256 MiB |
| Debian 13 | `test/debian13/golden-image-setup-debian13.sh` | 阿里云（trixie） | ESP 不小于 256 MiB |
| Ubuntu 24.04 | `test/ubuntu2404/golden-image-setup-ubuntu2404.sh` | 阿里云（noble） | ESP 需重建 |
| Ubuntu 26.04 | `test/ubuntu2604/golden-image-setup-ubuntu2604.sh` | 阿里云（suite 运行时检测） | ESP 需 4K 化 |
| Fedora 44 | `test/fedora44/golden-image-setup-fedora44.sh` | 阿里云（fedora + updates） | XFS；须在 4K 逻辑块盘上安装 |
| AlmaLinux 10.2 | `test/almalinux10/golden-image-setup-almalinux10.sh` | 华为云（BaseOS/AppStream/CRB/Extras） | XFS；须在 4K 逻辑块盘上安装 |
| openSUSE Leap 16.0 | `test/opensuse-leap16/golden-image-setup-leap16.sh` | TUNA OSS | XFS；目标盘须为空 |

脚本均内嵌 `initramfs-nbft` 组件与 GRUB 2.14 产物，单文件可拷入母盘执行。组件或 GRUB 产物更新后，需在固件仓库执行 `python3 test/shared/embed-components.py` 重新内嵌。

## 原生 NBFT 与 NBCT

原生 NBFT 消费不等于原生认证。RHEL 的 `nm-initrd-generator`/`95nvmf` 与 SUSE 的 dracut `95nvmf` 能从 NBFT 建立网络与发现连接，但 NBFT 标准表不含 DH-HMAC-CHAP 密钥与 TLS-PSK，其代码路径是无凭据的 `nvme connect-all --nbft`。

本项目使用 `95nbft` 消费自定义 NBCT 表：表内携带传输地址、子系统 NQN、Host NQN、DH-HMAC-CHAP 密钥与 TLS 交接参数，逐条执行 `nvme connect --dhchap-secret`；记录带 `tls_trsvcid` 时改为在内核 `.nvme` keyring 注入 PSK 并以 `--tls` 重连。构建时以 `--omit nvmf` 关闭原生模块，避免其抢先建立无认证连接。

## 发行版家族差异

### Debian 与 Ubuntu

- initramfs 集成走 initramfs-tools 的 `hooks/nbft` 与 `scripts/local-top/nbft`。
- Debian 12 的 6.1 内核不满足 NVMe 认证要求，需 backports 6.12；Debian 13 与 Ubuntu 使用自带内核。
- Ubuntu 24.04 的云镜像 ESP 仅 106 MiB，需重建为 4K FAT32；26.04 默认 ESP 已满足容量要求，仅需 4K 化。
- GRUB 参数包含 `net.ifnames=0 ip=dhcp nbft_auto`；Debian 12 自带的 GRUB 2.06 在 SAN 路径卡死，脚本默认使用内嵌 GRUB 2.14。

### Fedora

- dracut `95nbft` 模块在 `initqueue/settled` 与 `pre-mount` 双挂载，以 `/run/nbft-done` 去重。
- 依赖 `dracut-network` 与 `systemd-networkd`；initrd 内用 `09-nbft.network`（DHCP + KeepConfiguration）。
- XFS 扇区大小在 `mkfs` 时确定，安装盘必须为 4K 逻辑块盘；否则母盘无法在 nvmet 的 4096 B 导出上挂载。
- LVM 根需要显式 `lvm vgchange -ay`，并在 GRUB 参数中加入 `rd.lvm.lv=<vg>/<lv>`。

### RHEL 系与 SUSE 系

| 差异点 | AlmaLinux 10.2 | openSUSE Leap 16.0 |
| --- | --- | --- |
| 安装器 | Anaconda + kickstart | Agama + JSON profile |
| 包管理 | dnf | zypper |
| initrd 网络 | `35network-manager` + `nm-initrd-generator` | 同左 |
| 原生 NBFT 模块 | `95nvmf` + `nm-initrd-generator` | dracut `95nvmf` |
| 本仓库做法 | `--omit nvmf`，安装 `95nbft` | 同左 |
| TLS 组件 | `keyutils` + `ktls-utils` | 同左 |
| crypto-policies | 必须把 `gnutls.config` 装进 initrd | 同左 |
| initrd 路径 | `/boot/initramfs-<KVER>.img` | `/boot/initrd-<KVER>`，拷入 ESP 时改名 |
| 布局约束 | XFS，4K 逻辑块盘 | XFS，4K 逻辑块盘，目标盘须为空 |

RHEL 系的 `crypto-policies` 是 initrd 的硬依赖：GnuTLS 的 `@SYSTEM` 默认优先级来自 `/etc/crypto-policies/back-ends/gnutls.config`，缺失时 `tlshd` 启动即报 `gnutls: The request is invalid. (-50)` 并退出，随后 `nvme connect --tls` 失败并进入 emergency mode。

RHEL 9.x 尚未实测；其内核为 5.14 系，NVMe/TCP TLS 是上游 6.7+ 特性，是否由发行版回移需先核对 `CONFIG_NVME_TCP_TLS` 与 `ktls-utils` 版本。

## 新增发行版

1. 从最接近的现有变体派生脚本，核对安装介质内核与 initrd 文件名、无人值守数据、包管理器源格式与 initrd 工具链。
2. 目标盘使用 4K 逻辑块虚拟盘；XFS 与 Agama 场景需特别注意磁盘约束。
3. 完成无人值守安装后，在母盘内执行 A–F；`--skip-disk` 只跳过 GPT 转换与 ESP 重建，ESP 内容仍会刷新。
4. 在隔离网络完成 PXE 冷启动验收：target 端两条控制器记录、抓包判据与登录提示符。
5. 组件变更后重跑 `embed-components.py`，并以 dash 与 bash 各跑一次 `test/test-nbft-connect-nbct.sh`。

## 相关页面

- [系统盘定制](../guide/system-disk.md)
- [固件概述](../firmware/index.md)
- [固件契约](./firmware-contract.md)
- 固件仓库发行版矩阵：<https://github.com/dutyc/kurrent-firmware/blob/main/docs/golden-image-matrix.md>
