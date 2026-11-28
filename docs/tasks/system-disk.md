# 定制母盘

母盘是系统盘的克隆源，在虚拟机或离线环境制作一次，放入存储节点磁盘目录；控制面按 Worker 克隆系统盘，并在建盘时签发该盘凭据。本任务介绍如何制作母盘并部署到存储节点。

## 开始之前

- 控制面已部署，至少一个存储节点已接入。
- 有一台 UEFI 虚拟机或物理机用于安装目标发行版，目标盘使用 raw/qcow2 或直通盘。
- 目标发行版在[发行版矩阵](../reference/distributions.md)内；未列入的不视为已验证。
- XFS 场景（Fedora、AlmaLinux、openSUSE Leap）需准备 4K 逻辑块虚拟盘。

## 要求

系统盘要经过 iPXE `sanboot` → UEFI 固件 → GRUB → 内核 → initramfs 的多次交接，启动链对磁盘与系统有额外约束：

- **4K 原生 GPT**：nvmet 按 4096 B 逻辑块导出，512 扇区 GPT 被固件视为无分区，报 `0x7f22208e`
- **ESP 含 `EFI/BOOT/BOOTX64.EFI`**：固件找不到可移动介质回退入口，跳过该盘
- **GRUB ≥ 2.12（推荐 2.14）**：旧版 GRUB 在 SAN 路径挂起，菜单出现后无响应
- **内核启用 NVMe 认证与 TLS**：内核段认证失败（`ret=401`），或无法建立 TLS 会话
- **initramfs 能消费 NBFT/NBCT**：内核启动后没有盘，rootfs 无法挂载
- **启动后不重置承载连接的网络**：systemd 接管网卡后重置 TCP，出现 keep-alive 超时与 `no usable path`
- **`/boot/efi` 不在系统内挂载**：4K FAT ESP 挂载失败，系统进入 emergency mode
- **盘内无身份与凭据**：克隆出的每块盘带同一身份，违背无状态盘前提

这些约束由固件仓脚本封装为 A–F 六个阶段。

| 对象 | 位置 | 作用 |
| --- | --- | --- |
| 母盘 | 存储节点 `spec.agent.diskDir`，文件名含 `_tpl_` | 克隆源；不被导出，不参与启动 |
| 系统盘 | 同一目录，由控制面创建 | 从母盘克隆或空白创建；有独立 NQN、`os_tag` 与凭据 |
| 设备 | PXE 网段上的裸机 | 启动时取得所属 Worker 默认盘的连接信息 |

原生 NBFT 消费不等于原生认证：RHEL/SUSE 的 `95nvmf` 只做无凭据的 `nvme connect-all --nbft`；本项目用 `95nbft` 消费带凭据的 NBCT，构建时以 `--omit nvmf` 关闭原生模块。

> **说明：** 磁盘布局、内核、initramfs 与 GRUB 任一项不符合上述契约，克隆出的系统盘都会以同样方式失败。先验证母盘，再批量建盘。

## 制作母盘

### 1. 安装环境

- 最小化安装目标发行版；ESP 不低于 256 MiB。脚本不扩展分区，默认 ESP 偏小时先扩容。
- XFS 场景：安装盘必须是 4K 逻辑块虚拟盘；512 扇区 XFS 无法在 4096 B 导出上挂载。
- 母盘需 `python3`、`lsblk` / `findmnt` / `blkid` 与可用软件源；其余依赖由脚本安装。
- 脚本在母盘系统内以 root 运行，不在宿主机运行。

安装完成后先做业务软件、驱动、服务与用户定制，这些内容会随母盘克隆到每个系统盘。盘内不得写机器身份、target/NQN、认证密钥或控制面地址。不同角色分别制作 `_tpl_` 母盘。

### 2. 脚本获取

```bash
# download the golden-image-setup artifact from the firmware CI, or generate locally:
git clone https://github.com/dutyc/kurrent-firmware
cd kurrent-firmware
bash golden/shared/build-grub-efi.sh
python3 golden/shared/embed-components.py
ls dist/golden/*/golden-image-setup-*.sh
```

按下表选择与母盘对应的目录与默认参数：

| 发行版 / 目录 | 安装介质与约束 | 内核 / 包源 |
| --- | --- | --- |
| Debian 12<br>`debian12` | netinst；initramfs-tools | `6.12.95+deb12-amd64`（backports）<br>`mirrors.aliyun.com/debian` |
| Debian 13<br>`debian13` | netinst；initramfs-tools | `uname -r`（自带内核满足要求）<br>`mirrors.aliyun.com/debian` |
| Ubuntu 24.04<br>`ubuntu2404` | live-server；autoinstall，ESP 需重建；initramfs-tools | `uname -r`<br>`mirrors.aliyun.com/ubuntu` |
| Ubuntu 26.04<br>`ubuntu2604` | live-server；autoinstall，suite 运行时检测；initramfs-tools | `uname -r`<br>`mirrors.aliyun.com/ubuntu` |
| Fedora 44<br>`fedora44` | Server DVD；XFS/LVM；须在 4K 逻辑块盘上安装；dracut `95nbft` | `uname -r`<br>`mirrors.aliyun.com/fedora` |
| AlmaLinux 10.2<br>`almalinux10` | minimal ISO；kickstart，XFS；须在 4K 逻辑块盘上安装；dracut `95nbft` | `uname -r`<br>`repo.huaweicloud.com/almalinux` |
| openSUSE Leap 16.0<br>`opensuse-leap16` | online installer（Agama）；Agama profile，XFS；目标盘须为空；dracut `95nbft` | `uname -r`<br>`mirrors.tuna.tsinghua.edu.cn/opensuse` |
| Arch Linux<br>`archlinux` | arch ISO（2026.11.01）；archinstall；mkinitcpio | `uname -r`<br>`mirrors.tuna.tsinghua.edu.cn/archlinux` |

生成的脚本内嵌 initramfs 组件与 GRUB 2.14，单个 `.sh` 可拷入母盘运行，无需完整仓库。

### 3. 脚本执行

在母盘系统内执行：

```bash
sudo bash golden-image-setup-debian12.sh --help
sudo bash golden-image-setup-debian12.sh          # 完整 A–F，E 阶段需确认
sudo bash golden-image-setup-debian12.sh --yes    # 跳过确认，适合快照或可丢弃虚拟机
```

- 关键步骤失败即退出并返回非零；修正后可重跑，脚本幂等。
- E 阶段转换 4K GPT 并重建 ESP；首次运行前创建快照。
- 转换后 512 字节逻辑块环境（VMware/QEMU 默认盘）无法读取该盘；这是 SAN 母盘的预期形态。

完整执行 A–F 后关机，即得可克隆母盘。

常用选项：

- `--skip-disk`：跳过 GPT 转换与 ESP 重建（ESP 内容仍刷新）；ESP 不符合契约时不能替代完整执行。
- `--yes`：跳过 E 阶段确认，仅用于可丢弃环境。
- `--no-serial`：不加 `console=ttyS0,115200`，用于无串口物理机。
- `--mirror URL` / `--keep-sources`：切换或保留软件源。

其余选项见 `--help`。

### 4. 脚本做什么

A–F：装依赖与内核 → 写 initramfs 消费模块 → 修 fstab 与 GRUB → 转换 4K GPT 并重建 ESP → 无状态校验。

脚本不安装系统、不建分区、不扩 ESP、不装业务软件、不写 target/NQN/凭据或控制面地址。

修改 fstab 与 GRUB 前保留 `.golden-*.bak`；转换前 GPT 备份到 `/root/golden-image-gpt-512-*.bak`，冷启动验收通过前保留。

磁盘契约与实现细节见固件仓[发行版矩阵](https://github.com/dutyc/kurrent-firmware/blob/main/docs/zh/distributions.md)。

## 部署到存储节点

1. 关闭母盘虚拟机，确保镜像已落盘。
2. 复制到目标存储节点 `spec.agent.diskDir`，文件名含 `_tpl_`；必须是整盘镜像（GPT、ESP 与 root 分区），`_tpl_` 大小写不敏感：

   ```text
   /pool1/disk_dir/_tpl_debian12.img
   ```

3. 等待 Agent 后台扫描；母盘出现在 `/masters` 或 WebUI 母盘列表，`gpt` 应为 `4k`。
4. 在 Worker 上创建系统盘时选择该母盘；控制面生成 `os_tag` 与盘 NQN，Agent 用 reflink 克隆并签发 DHHC-1 凭据。创建步骤见[从母盘创建系统盘](./create-system-disk.md)。
5. 母盘不得直接导出给设备启动。

> **说明：** 控制面也支持 `empty` 空白盘用于手工安装；生产路径以母盘克隆为主，空白盘安装完成后仍须通过相同的验收。

## 验收

- 启动串口应看到固件段与内核段各一次认证成功，随后 rootfs 挂载并出现 `login:`；首块系统盘自动为默认盘。
- 不出现 `0x7f22208e`、`ret=401`、keep-alive 断链或 emergency mode。
- 抓包时数据面只应有 `4421` 的 TLS；盘内 grep 无身份与密钥，克隆出的盘 NQN、`os_tag` 与凭据各不相同。

“已验证”指完整链路：PXE 冷启动 → 固件段认证 → GRUB → 内核 → initramfs 消费 NBFT/NBCT → rootfs → 登录提示符。只到 GRUB 或内核不算完成。

## 故障排查

- **`Boot from SAN device ... failed`，错误 `0x7f22208e`**：GPT 不是 4K 原生；重跑 E 阶段；确认 `/masters` 的 `gpt=4k`。
- **内核段 `ret=401` / `authentication setup failed`**：内核未启用 NVMe 认证；换满足要求的内核。
- **内核段 TLS 失败**：`tlshd` 不在 initramfs、版本过低或缺配置。
- **启动后 keep-alive 超时 / `no usable path`**：systemd 网络服务重置了承载连接；按变体脚本加 `systemd.mask`。
- **emergency mode，`boot-efi.mount` 失败**：fstab 仍挂载 `/boot/efi`；注释该行并重建 initramfs 与 GRUB。
- **GRUB 菜单无响应**：GRUB 版本过低；使用内嵌 2.14 或 `--grub-efi`。
- **母盘不在列表中**：文件名无 `_tpl_`、不在 `diskDir` 或 Agent 不可读。
- **克隆报 512 GPT**：在母盘上执行 E 阶段，确认 4K GPT。

## 接下来

- 从母盘克隆系统盘：[从母盘创建系统盘](./create-system-disk.md)
- 对象与放置：[设备、Worker 与系统盘](../concepts/entities.md)
- 母盘与数据面：[存储与数据面](../concepts/storage.md)
- 完整操作：[部署](../setup/deployment.md)
