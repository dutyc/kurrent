# 系统盘定制

Kurrent 的系统盘由母盘克隆生成，而非逐台安装。母盘在虚拟机或离线环境中制作，随后放入存储节点的磁盘目录；控制面按 Worker 克隆系统盘，并在建盘时签发该盘的认证凭据。母盘仅作为克隆源，不参与启动。母盘定制属于一次性准备，不随每次部署重复。

> 母盘的磁盘布局、内核、initramfs 与 GRUB 中任一项不符合 SAN 引导契约，所克隆的系统盘均会以相同方式失败。应先验证母盘，再批量建盘。

## 对象与位置

| 对象 | 位置 | 作用 |
| --- | --- | --- |
| 母盘 | 存储节点 `spec.agent.diskDir`，文件名含 `_tpl_` | 克隆源；不被导出，不参与启动 |
| 系统盘 | 同一目录，由控制面创建 | 从母盘克隆或空白创建；有独立 NQN、`os_tag` 与凭据 |
| 设备 | PXE 网段上的裸机 | 启动时取得所属 Worker 默认盘的连接信息 |

母盘必须先在存储节点上被 Agent 扫描到，才能在建盘时被选为来源。

## 定制的必要性

常规安装盘面向本地磁盘启动。Kurrent 的系统盘需经过 iPXE `sanboot` → UEFI 固件 → GRUB → 内核 → initramfs 的多次交接，启动链对磁盘与系统存在额外约束：

- **4K 原生 GPT**：nvmet 按 4096 B 逻辑块导出，512 扇区 GPT 被固件视为无分区，报 `0x7f22208e`
- **ESP 含 `EFI/BOOT/BOOTX64.EFI`**：固件找不到可移动介质回退入口，跳过该盘
- **GRUB ≥ 2.12（推荐 2.14）**：旧版 GRUB 在 SAN 路径卡死，菜单出现后无响应
- **内核启用 NVMe 认证与 TLS**：内核段认证失败（`ret=401`），或无法建立 TLS 会话
- **initramfs 能消费 NBFT/NBCT**：内核启动后没有盘，rootfs 无法挂载
- **启动后不重置承载连接的网络**：systemd 接管网卡后重置 TCP，出现 keep-alive 超时与 `no usable path`
- **`/boot/efi` 不在系统内挂载**：4K FAT ESP 挂载失败，系统进入 emergency mode
- **盘内无身份与凭据**：克隆出的每块盘带同一身份，违背无状态盘前提

上述约束属于引导链契约，并非可选优化。固件仓的母盘脚本将对应步骤封装为 A–F 六个阶段。

## 已验证发行版

当前完成整机冷启动验证的发行版为 Debian 12/13、Ubuntu 24.04/26.04、Fedora 44、AlmaLinux 10.2 与 openSUSE Leap 16.0。各版本的安装介质、安装约束、initramfs 消费方式、定制脚本与默认包源见下文[脚本获取](#_3-脚本获取)；版本、软件源与家族差异见[发行版矩阵](../reference/distributions.md)，原生 NBFT 消费的研究记录见固件仓[发行版研究](https://github.com/dutyc/kurrent-firmware/blob/main/docs/nvmeof-research.md)。

未列入上述范围的版本不应视为已验证，RHEL 9.x 尚未实测。原生 NBFT 消费不等于原生认证：RHEL 与 SUSE 自带的 `95nvmf` 只执行无凭据的 `nvme connect-all --nbft`，本项目的 `95nbft` 消费携带凭据的 NBCT，构建时以 `--omit nvmf` 关闭原生模块。

> “已验证”指完整链路：PXE 冷启动 → 固件段认证 → GRUB → 内核 → initramfs 消费 NBFT/NBCT → rootfs → 登录提示符。仅验证到 GRUB 或内核启动不视为完成。

## 制作流程

### 1. 安装环境

- UEFI 虚拟机或物理机；目标盘使用 raw/qcow2 或直通盘。
- 最小化安装目标发行版；ESP 容量不低于 256 MiB。脚本不自动扩展分区，部分安装介质的默认 ESP 较小，应在运行前扩容。
- XFS 场景（Fedora、AlmaLinux、openSUSE Leap）：安装盘必须为 **4K 逻辑块**虚拟盘。XFS 的扇区大小在 `mkfs` 时确定；512 扇区 XFS 无法在 nvmet 的 4096 B 导出上挂载。
- 母盘内需具备 `python3`、`lsblk` / `findmnt` / `blkid`（util-linux）与可用的软件源；`nvme-cli`、`jq`、`keyutils`、`ktls-utils`、`initramfs-tools` 等由脚本 A 阶段安装。
- 脚本应在**母盘系统内**以 root 身份运行，不在宿主机运行。

安装完成后，先行完成业务软件、驱动、服务与用户等系统内容定制；上述内容将随母盘克隆至每个系统盘。盘内不得写入机器身份、target/NQN、认证密钥或控制面地址；不同角色应制作不同的 `_tpl_` 母盘。

### 2. 内核能力确认（可选）

脚本 A 阶段安装依赖并检查内核的 NVMe 认证能力；Debian 12 另行安装 bookworm-backports 的 6.12 内核。如需提前检查完整能力：

```bash
# 内核需启用 NVMe 认证与 NVMe/TCP TLS
grep -E 'CONFIG_NVME_(AUTH|TCP_TLS|TARGET_TCP_TLS|KEYRING)' /boot/config-$(uname -r)

# tlshd 负责内核段 TLS 握手（版本需 >= 1.5.0）
dpkg-query -W ktls-utils 2>/dev/null || rpm -q ktls-utils
systemctl status tlshd
```

Debian 12 的 6.1 内核不满足认证要求；其余变体默认使用当前 `uname -r`。内核不支持 NVMe/TCP TLS 时，该版本不得作为生产母盘，数据面亦不得回落至明文。

### 3. 脚本获取

```bash
git clone https://github.com/dutyc/kurrent-firmware
cd kurrent-firmware
ls test/*/golden-image-setup-*.sh
```

脚本位于固件仓 `test/<目录>/golden-image-setup-*.sh`，所有目录都会向 initramfs 注入 `nbft-connect`。选择与母盘对应的目录与默认参数：

| 发行版 / 目录 | 安装介质与约束 | 内核 / 包源 |
| --- | --- | --- |
| Debian 12<br>`debian12` | netinst；initramfs-tools | `6.12.95+deb12-amd64`（backports）<br>`mirrors.aliyun.com/debian` |
| Debian 13<br>`debian13` | netinst；initramfs-tools | `uname -r`（自带内核满足要求）<br>`mirrors.aliyun.com/debian` |
| Ubuntu 24.04<br>`ubuntu2404` | live-server；autoinstall，ESP 需重建；initramfs-tools | `uname -r`<br>`mirrors.aliyun.com/ubuntu` |
| Ubuntu 26.04<br>`ubuntu2604` | live-server；autoinstall，suite 运行时检测；initramfs-tools | `uname -r`<br>`mirrors.aliyun.com/ubuntu` |
| Fedora 44<br>`fedora44` | Server DVD；XFS/LVM；须在 4K 逻辑块盘上安装；dracut `95nbft` | `uname -r`<br>`mirrors.aliyun.com/fedora` |
| AlmaLinux 10.2<br>`almalinux10` | minimal ISO；kickstart，XFS；须在 4K 逻辑块盘上安装；dracut `95nbft` | `uname -r`<br>`repo.huaweicloud.com/almalinux` |
| openSUSE Leap 16.0<br>`opensuse-leap16` | online installer（Agama）；Agama profile，XFS；目标盘须为空；dracut `95nbft` | `uname -r`<br>`mirrors.tuna.tsinghua.edu.cn/opensuse` |

脚本内嵌 `initramfs-nbft` 组件与 GRUB 2.14 产物，**单个 `.sh` 文件可拷入母盘运行**，无需复制完整仓库。仅在修改组件并重新内嵌时，需在仓库内执行：

```bash
python3 test/shared/embed-components.py
```

### 4. 脚本执行

在母盘系统内执行；脚本可置于任意目录：

```bash
# 查看该变体支持的选项
sudo bash golden-image-setup-debian12.sh --help

# 完整执行 A–F；E 阶段会要求确认
sudo bash golden-image-setup-debian12.sh

# 跳过 E 的确认，适合快照或可丢弃虚拟机
sudo bash golden-image-setup-debian12.sh --yes
```

`--help` 输出脚本头部的用法说明；各变体的选项集合一致。

执行时按 A→F 顺序输出 `==> [A] ...` 至 `==> [F] ...`。关键步骤失败时脚本立即退出并返回非零状态；修正条件后可重新执行，脚本具备幂等性。E 阶段将 GPT 转换为 4K 并重建 ESP，已符合目标格式的步骤将跳过，但**首次运行前应创建快照**。转换完成后，512 字节逻辑块环境（VMware/QEMU 默认虚拟盘）无法读取该盘，此为 SAN 母盘的预期形态。

完整执行 A–F 后关机，即得到可克隆的母盘镜像。

### 5. 常用选项

- **`-h`, `--help`**：打印用法说明并退出。
- **`-k VER`**：指定内核版本；Debian 12 默认 `6.12.95+deb12-amd64`，其余默认 `uname -r`。通常无需修改。
- **`-d DIR`**：指定 initramfs 组件目录；默认使用仓库内的 `initramfs-nbft/`，不在仓库时使用内嵌组件。仅在修改组件时使用。
- **`--skip-disk`**：跳过 E 的 GPT 转换与 ESP 重建；ESP 内容仍会在每次执行时刷新。用于 chroot、ESP 由外部维护，或只需更新引导产物而不改动磁盘布局的场景。ESP 容量或格式不符合契约时，此项无法替代完整执行。
- **`--yes`**：跳过 E 阶段的交互确认；仅在可丢弃环境使用。
- **`--grub-efi FILE`**：使用本地构建的 `BOOTX64.EFI` 替换内嵌 GRUB 2.14；Debian 12 自带的 GRUB 2.06 不影响默认流程，脚本默认使用内嵌 2.14。除特定需求外不应使用。
- **`--no-serial`**：不添加 `console=ttyS0,115200`；适用于无串口的物理机。
- **`--mirror URL`**：切换软件源；默认使用对应发行版的国内镜像（Debian/Ubuntu/Fedora 为阿里云，AlmaLinux 为华为云，openSUSE Leap 为 TUNA）。
- **`--keep-sources`**：保留母盘原软件源；设置后 `--mirror` 不生效。

### 6. 典型用法

- 首次完整制作（虚拟机/快照）：`sudo bash golden-image-setup-<distro>.sh`
- 无人值守完整制作：`sudo bash golden-image-setup-<distro>.sh --yes`
- chroot 或 ESP 已单独维护：`sudo bash golden-image-setup-<distro>.sh --skip-disk --yes`（不重写 GPT/ESP，仍写入最新引导产物）
- 物理机无串口：`sudo bash golden-image-setup-<distro>.sh --no-serial`
- 内网或本地软件源：`sudo bash golden-image-setup-<distro>.sh --keep-sources`，或 `--mirror <URL>`
- 指定内核：`sudo bash golden-image-setup-<distro>.sh -k <version>`

### 7. 脚本职责与边界

脚本自动完成 A–F 阶段，包括安装依赖与内核、写入 initramfs 组件、修正 fstab 与 GRUB、将 GPT 转换为 4K 原生布局并重建 ESP，以及执行无状态静态检查。修改 `fstab` 与 GRUB 默认参数时保留 `.golden-*.bak` 备份；512→4K GPT 转换前将原 GPT 备份至 `/root/golden-image-gpt-512-*.bak`。冷启动验收通过前应保留上述备份。

脚本不执行以下操作：安装操作系统、创建或扩容 ESP、创建根分区或根文件系统、安装业务软件、写入 target/NQN/凭据或控制面地址。ESP 小于 256 MiB、未找到 GPT 或无法定位 root 所在盘时，脚本直接终止，不提供自动分区修复。

> `--skip-disk` 只跳过 GPT 转换与 ESP 重建，不跳过 ESP 内容刷新：E 阶段仍会把当前内核、initrd、`BOOTX64.EFI` 与 grub.cfg 写入 ESP。修改内核、重建 initramfs 或调整 GRUB 参数后，以 `--skip-disk` 重跑即可让新产物生效；若 ESP 本身需要扩容或重新格式化，仍须完整执行 E。

## A–F 六阶段

脚本按顺序执行以下六个阶段。

### A. 系统与内核

安装 `nvme-cli`、`jq`、`keyutils`、`ktls-utils` 等运行依赖，并确认内核带 NVMe 认证与 NVMe/TCP TLS。内核段要完成 DH-HMAC-CHAP 认证和 TLS，两者缺一不可。

Debian 12 需要 backports 6.12；Debian 13、Ubuntu 24.04/26.04、Fedora 44、AlmaLinux 10.2 与 openSUSE Leap 16.0 的自带内核满足要求。认证失败的典型现象是内核段 `ret=401` 或 `authentication setup failed`。

### B. initramfs 消费模块

initramfs 必须在内核启动后重新连接系统盘。两种集成方式：

| 框架 | 集成 | 关键点 |
| --- | --- | --- |
| initramfs-tools（Debian/Ubuntu） | `hooks/nbft` 与 `scripts/local-top/nbft` | `nbft_auto` 参数；dash/ash 语义；`tlshd` 配置文件 |
| dracut（Fedora/AlmaLinux/Leap） | `95nbft` 模块 | `--omit nvmf`；Fedora 用 `systemd-networkd`，AlmaLinux 与 Leap 走 NetworkManager；`--no-hostonly`；Fedora 的 LVM 显式激活 |

RHEL 系与 SUSE 系还需把 `/etc/crypto-policies/back-ends/gnutls.config` 装进 initramfs；缺失时 `tlshd` 以 `gnutls: The request is invalid. (-50)` 退出，内核段 TLS 无法建立。

`nbft-connect` 先查 NBCT（带内凭据），逐条 `nvme connect`；没有 NBCT 时回退 `connect-all --nbft`。它还会注入固定的 hostid，避免内核 nvme-fabrics 的 host tracking 拒绝自定义 hostnqn。

验证 initramfs 内容：

```bash
# Debian / Ubuntu
lsinitramfs /boot/initrd.img-$(uname -r) | grep -E 'nbft|tlshd|nvme'

# dracut（Fedora / AlmaLinux / Leap）
lsinitrd /boot/initramfs-$(uname -r).img | grep -E 'nbft|tlshd|nvme'
lsinitrd /boot/initrd-$(uname -r) 2>/dev/null | grep -E 'nbft|tlshd|nvme'
```

### C. 系统配置

- `fstab` 的 root 分区写 **UUID**，不写 `/dev/nvme0n1p2` 之类的设备名。
- 注释 `/boot/efi` 行。ESP 由固件与 GRUB 读取；系统内挂载 4K FAT ESP 会失败并进入 emergency mode。
- 保留 DHCP；hostname 只放占位值，生产 hostname 由控制面下发。
- 盘内不写 target 地址、端口、NQN、认证密钥或控制面地址。

### D. GRUB 启动参数

```text
net.ifnames=0 biosdevname=0 ip=dhcp nbft_auto
```

dracut 系再追加 `rd.neednet=1`。如果 NetworkManager 或 systemd-networkd 会在启动后接管同一张网卡，按变体脚本的做法用 `systemd.mask=...` 阻止其重置承载连接；否则会出现 keep-alive 超时与 `no usable path`。

移除 `quiet`：它会把 info 级日志压到 WARNING，串口上看不到内核段认证成功，验收会误判。

GRUB 版本要求 ≥ 2.12；Debian 12 自带的 2.06 在 SAN 路径实测卡死。脚本默认内嵌 GRUB 2.14，也可用 `--grub-efi` 注入宿主产物。

### E. 磁盘布局

母盘必须是 **4K 原生 GPT**：GPT 头在 4096 偏移、分区表在 8192，备份头在盘尾。脚本要求 ESP 至少 256 MiB，并以 4K FAT32 重建，写入：

```text
EFI/BOOT/BOOTX64.EFI    GRUB 2.14（可移动介质回退入口）
EFI/BOOT/grub.cfg       SAN 引导菜单
vmlinuz-<kernel>        内核
initramfs-<kernel>.img  initramfs（含 NBFT 消费模块）
```

initrd 在 ESP 内的文件名随变体不同（`initramfs-<kernel>.img` 或 `initramfs-<kernel>`），须与 grub.cfg 的 `initrd` 行一致；Leap 从 `/boot/initrd-<kernel>` 改名写入。

GRUB 菜单用 `root=UUID=<root-uuid>` 定位根分区；LVM 根还要加 `rd.lvm.lv=<vg>/<lv>`。ESP 卷 UUID、`grub.cfg` 的 `search --fs-uuid` 与 fstab 注释行必须一致。

手工转换 512 GPT 或查阅实现细节时，参见固件仓[母盘定制指南](https://github.com/dutyc/kurrent-firmware/blob/main/docs/golden-image-customization-debian12.md)的磁盘章节与 `test/shared/golden-esp-verify.sh`。

### F. 无状态校验

在母盘内检查不应存在的身份与凭据：

```bash
grep -rn "hostnqn\|dhchap\|secret\|kurrent:host" /etc /usr/local/sbin /usr/share/initramfs-tools 2>/dev/null
grep -n "HOSTNAME\|hostname" /etc/hostname /etc/hosts
```

期望：没有 target、NQN 或密钥命中；hostname 仅为占位值。hostid 是 `nbft-connect` 的固定兜底，属于固件侧契约，不应写成盘内身份文件。

## 部署到存储节点

1. 关闭母盘虚拟机，确保镜像已落盘。
2. 将镜像复制到目标存储节点 `spec.agent.diskDir`，文件名包含 `_tpl_`。应放入整盘镜像（GPT、ESP 与 root 分区），不应放入单个分区镜像；`_tpl_` 大小写不敏感：

   ```text
   /pool1/disk_dir/_tpl_debian12.img
   ```

3. 确认 Agent 容器可读该文件；等待后台扫描。母盘出现在 `/masters` 或 WebUI 的母盘列表，`gpt` 应为 `4k`。
4. 在 Worker 上创建系统盘时选择该母盘。控制面生成 `os_tag` 与盘 NQN，Agent 用 reflink 克隆出独立系统盘，nvmet 建 target 并签发 DHHC-1 凭据。
5. 母盘不得直接导出给设备启动，仅作为克隆源。

控制面亦支持创建 `empty` 空白盘用于手工安装；生产路径以母盘克隆为主。空白盘安装完成后仍须通过本页 A–F 校验，否则不能作为 SAN 系统盘使用。

## 验收

- 单盘克隆后，Worker 的第一块系统盘自动成为默认盘；启动串口应看到固件段与内核段各一次认证成功，随后 rootfs 挂载并出现 `login:`。
- 不出现 `0x7f22208e`、`ret=401`、keep-alive 断链或 emergency mode。
- 采样抓包时，数据面只应出现 `4421` 的 TLS；不应出现明文 `4420` 或明文 PSK。
- 盘内 grep 无身份与密钥；同一母盘克隆出的不同系统盘，NQN、`os_tag` 与凭据均不同。

完整冷启动步骤属于首次交付；本页仅涉及母盘的制作与校验。

## 故障排查

- **`Boot from SAN device ... failed`，错误 `0x7f22208e`**：GPT 不是 4K 原生；重跑 E 阶段或转换 GPT；确认 `/masters` 的 `gpt=4k`。
- **内核段 `ret=401` / `authentication setup failed`**：内核未启用 NVMe 认证；换满足要求的内核；检查 `CONFIG_NVME_AUTH`。
- **内核段 TLS 失败**：`tlshd` 不在 initramfs、版本过低或缺 `/etc/tlshd/config`；检查 initramfs 内容与 `tlshd` 配置。
- **启动后 keep-alive 超时 / `no usable path`**：systemd 网络服务重置了承载连接；按变体脚本加 `systemd.mask`；核对 cmdline。
- **emergency mode，`boot-efi.mount` 失败**：fstab 仍挂载 `/boot/efi`；注释该行并重建 initramfs 与 GRUB。
- **GRUB 菜单卡死**：GRUB 版本过低（如 2.06）；使用内嵌 2.14 或 `--grub-efi`。
- **母盘不在列表中**：文件名无 `_tpl_`、不在 `diskDir` 或 Agent 不可读；检查命名、路径与权限；等待扫描。
- **克隆报 512 GPT**：母盘布局不符合要求；在母盘上执行 E 阶段，确认 4K GPT。

## 相关页面

- 对象与放置：[设备、Worker 与系统盘](../concepts/entities.md)
- 母盘与数据面：[存储与数据面](../concepts/storage.md)
- 完整操作：[首次交付](./usage.md)
