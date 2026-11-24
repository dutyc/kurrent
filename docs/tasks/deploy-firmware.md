# 交付签名固件

签名产物经 TFTP 首跳（默认路径）或本地介质交付给设备。交付完成后，目标机在平台注册窗口内完成首次启动与入池。

## 开始之前

- 控制面已完成部署，PXE 网段就绪（见[部署](../setup/deployment.md)）。
- 已按[构建签名固件](./build-firmware.md)取得 `snponly-sb.efi`、`ipxe-sb.efi` 与 `manifest.json`。
- 目标设备满足[裸机设备要求](../setup/host-prerequisites.md#裸机设备要求)：x86_64 UEFI、TPM 2.0，且处于 Setup Mode 或 Secure Boot 关闭；出厂已锁厂商密钥的机型经其厂商的管理流程处理。

## 交付到控制面

1. 把 `snponly-sb.efi`、`ipxe-sb.efi` 与 `manifest.json` 放入主仓库 `tftp/secureboot/`。该目录是 `/boot-assets` 的产物核对与导出源，不入库。
2. 覆盖 TFTP 首跳并重启 dnsmasq：

   ```bash
   sudo install -m 0644 tftp/secureboot/snponly-sb.efi tftp/snponly.efi
   sudo install -m 0644 tftp/secureboot/ipxe-sb.efi tftp/ipxe.efi
   eval "$(kurrent deployment show -o env)"
   docker compose restart kurrent-dnsmasq
   ```

   `tftp/snponly.efi` 与 `tftp/ipxe.efi` 都是 dnsmasq 的文件级挂载，替换后必须重启容器，否则容器仍持有旧 inode；两份缺一 dnsmasq 无法启动。

3. 核对产物：`GET /api/cp/boot-assets` 的 `secureboot.status` 应为 `ok`（镜像哈希与 manifest 一致、端口与部署一致、boot CA 指纹一致）。

控制面按设备的 `boot_firmware` 选择首跳：默认设备取 `snponly.efi`，`boot_firmware=full` 的设备取全驱动 `ipxe.efi`；同一 PXE 网段可按设备混用。

## 交付路径

| 条件 | 路径 |
| --- | --- |
| 主板有 UEFI 网络启动入口 | PXE：TFTP 交付签名首跳，默认路径 |
| 主板无 PXE 入口 | 本地介质：`direct-uefi` 签名产物写入 USB / ESP |
| 出厂已锁厂商密钥 | 经其厂商的管理流程处理 |

### 本地介质

`direct-uefi` 产物内嵌与 PXE 相同的静态脚本，平台注册窗口开启时同样执行密钥写入。

1. 准备 FAT32 分区（U 盘或本地 ESP）。
2. 创建 `EFI/BOOT/` 目录。
3. 将 `dist/direct-uefi/snponly-sb.efi`（或 `ipxe-sb.efi`）复制为 `EFI/BOOT/BOOTX64.EFI`。
4. 在设备启动菜单中选择该介质。

若主板仅识别固定 ESP，将文件放入内置磁盘的同一路径。GRUB 链加载：

```text
menuentry "Kurrent firmware" {
    chainloader /boot/efi/EFI/kurrent/snponly-sb.efi
}
```

## 首次启动

交付完成后，目标机首次网络启动依次完成：

1. 平台密钥写入：平台注册窗口开启时执行 `sbenroll` 写入 PK / KEK / db，首次成功自动重启（见[预置目标机信任库](./enroll-platform-keys.md)）。
2. 设备身份：执行 `akgen` 在 TPM 内创建 AK，`akpub` 导出公钥。
3. 上报与证明：向 `/devices/report` 上报指纹、AK 与 Secure Boot 状态；此后每次引导经 `/devices/challenge` 取 nonce，用 `quote` 对 PCR 0/2/4/7 生成证明。
4. 投影：经 4443 取回引导变量，只信任内嵌的 boot CA。

设备必须上报 `sb=1` 才能入池；Secure Boot 关闭时，注册期上报不写台账，只留拒绝审计。身份语义见[设备身份](../concepts/boot-chain/device-identity.md)。

## 验收

| 检查 | 预期 |
| --- | --- |
| 设备启动 | 进入 iPXE 菜单，能看到该 Worker 的系统盘 |
| WebUI → Devices 平台注册 | 列出已完成密钥写入的 MAC |
| WebUI → Devices | 设备在注册窗口内入池 |
| WebUI → 引导链 / `/boot-assets` | 固件 `status=ok`，能力齐全；`secureboot.status=ok` |
| 控制面日志 / 审计 | `/devices/report`、`/devices/challenge`、`/boot-vars` 有对应记录 |
| 数据面 | 选择默认盘后可完成 NVMe/TCP 连接并进入系统 |

`secureboot.status` 不是 `ok` 时，按镜像哈希、端口与 boot CA 三项分别核对。

## 更新与回滚

- 替换 `tftp/snponly.efi` 或 `tftp/ipxe.efi` 并重启 dnsmasq 后，PXE 首跳即完成更新。
- 本地介质设备需要重新写入新的 `direct-uefi` 产物。
- 保留上一份产物用于回滚；TPM 身份与指纹不随固件文件变化。
- 更新后至少验证一台设备完成菜单与一次系统启动。

## 失败

- 无 DHCP 应答、DHCP 后无 TFTP 下载、引导 HTTPS 证书错误：按[故障排查](./troubleshooting.md)定位。
- 卡在固件启动：产物未签名、签名证书不在目标机 `db`，或 Secure Boot 未开启；用 `sbverify --cert` 与固件仓 `tools/secureboot/check-host.sh` 复核。
- 菜单为空或启动循环：设备未绑定 Worker、Worker 无系统盘或默认盘。

## 接下来

- 构建产物与签名：[构建签名固件](./build-firmware.md)
- 首次启动写入平台密钥：[预置目标机信任库](./enroll-platform-keys.md)
- 引导链准入：[引导链](../concepts/boot-chain.md)
- 故障排查：[故障排查](./troubleshooting.md)
