# 预置与刷写

固件只有进入设备并完成一次成功的网络引导，才算真正就位。预置路径有两条：主板原生 PXE，或本地介质引导。

## 选择路径

| 条件 | 路径 |
| --- | --- |
| 主板有 UEFI Network Boot / PXE 入口 | PXE，使用平台 TFTP 提供的 `snponly.efi` |
| 主板无 PXE 入口，或 PXE 设置不可用 | 本地介质（USB / ESP / 本地磁盘 / GRUB 链加载） |
| 网卡无原生驱动，但固件自带 SNP 栈 | `snponly` 产物 |
| 需要批量预置且 BMC 支持虚拟介质 | BMC / Redfish 挂载介质，作为可选项 |

推荐优先使用 PXE：固件更新只替换 TFTP 根中的文件，不接触每台设备的启动介质。

## PXE 路径

前提：

- x86_64 UEFI 设备；
- 网卡有原生驱动，或可使用固件 SNP；
- 设备接入控制面的 PXE 网段；
- 主线固件未签名，Secure Boot 需要关闭（见下）。

步骤：

1. 按[构建与产物](./build.md)或 Releases 取得 `pxe-uefi/snponly.efi`。
2. 放入主仓库 `tftp/snponly.efi`，重启 dnsmasq 容器。
3. 在设备 UEFI 设置中启用网络栈，将网络启动置于启动顺序首位。
4. 启动设备，确认 DHCP 分配地址、TFTP 下载首跳、HTTPS 取回脚本。

首跳只下载 `snponly.efi` 与 `boot.ipxe`；后续脚本从 HTTPS 取回。PXE 网段必须按[安全模型](../architecture/security.md)隔离。

## 本地介质路径

适用于没有 PXE 入口的主板。使用 `direct-uefi` 产物：它内置 `auto.ipxe`，启动后执行 DHCP 并跳转到控制面的 TFTP 引导链。

### USB / ESP

1. 准备一个 FAT32 分区（U 盘或本地 ESP）。
2. 创建目录 `EFI/BOOT/`。
3. 将 `dist/direct-uefi/ipxe.efi` 复制为 `EFI/BOOT/BOOTX64.EFI`。
4. 在设备启动菜单中选择该 USB/ESP。

UEFI 会从可移动介质的 `EFI/BOOT/BOOTX64.EFI` 启动。若主板只识别固定 ESP，把文件放入内置磁盘的同一路径，并在启动项中选择它。

### GRUB 链加载

已有 Linux/GRUB 的设备可以直接链加载，不必改启动介质：

```text
menuentry "Kurrent firmware" {
    chainloader /boot/efi/EFI/kurrent/ipxe.efi
}
```

把 `direct-uefi/ipxe.efi` 放到对应路径，更新 GRUB 配置后从该菜单项启动。

### BMC / Redfish

支持虚拟介质的 BMC 可以把上述 USB/ISO 作为虚拟介质挂载，或把启动项指向该介质。该路径只做过协议级验证，具体型号能力不同；它不是 Kurrent 的部署依赖，也不替代 PXE 网段的隔离要求。

## Secure Boot

主线固件未签名：

- Secure Boot 开启时，固件加载会被拒绝，设备无法进入引导链。
- 当前主线的部署路径要求关闭 Secure Boot，或使用组织自行签名的产物；签名产物与预置流程不在主线发布范围内。
- 关闭 Secure Boot 会削弱首跳保护；部署应通过 PXE 网段隔离与二层防护补偿，见[安全模型](../architecture/security.md)。

不要把“关闭 Secure Boot”当成安全方案；它只是当前主线的兼容条件。

## 首次启动与身份

首次启动时固件会：

1. 执行 `keygen`，在 NVRAM 生成设备私钥；
2. 通过 `pubkey` 导出公钥；
3. 向控制面 `/devices/report` 上报指纹与公钥；
4. 首次 HTTPS 握手固定控制面服务器叶证书指纹（TOFU）。

身份语义：

- 设备私钥与服务器指纹存在 NVRAM，**不在盘上**；更新固件或替换 TFTP 文件不会清除它们。
- 清除 `device-key` 等同于更换设备身份，需要重新登记与绑定。
- 清除 `server-fingerprint` 会解除 TOFU，下一次 HTTPS 连接重新固定；控制面证书轮换后必须这样做。
- 更换硬件或整机替换，等于一台新设备；系统盘与 Worker 身份仍在控制面，换绑即可。

NVRAM 清理应通过固件可交互环境或受控预置流程完成；不要把 NVRAM 内容复制到另一台设备。

## 验证

| 检查 | 预期 |
| --- | --- |
| 设备启动 | 进入 iPXE 菜单，能看到该 Worker 的系统盘 |
| WebUI → Devices | 设备在注册窗口内入池 |
| WebUI → 引导链 / `/boot-assets` | 固件 `status=ok`，能力齐全 |
| 控制面日志 / 审计 | `/devices/report`、`/devices/challenge`、`/boot-vars` 有对应记录 |
| 数据面 | 选择默认盘后可完成 NVMe/TCP 连接并进入系统 |

## 更新与回滚

- 替换 `tftp/snponly.efi` 并重启 dnsmasq，即可更新 PXE 首跳固件。
- 本地介质设备需要重新写入新的 `direct-uefi` 产物。
- 保留上一份产物用于回滚；NVRAM 身份与指纹不随固件文件变化。
- 更新后至少验证一台设备完成菜单与一次系统启动。

## 常见失败

| 现象 | 排查 |
| --- | --- |
| 无 DHCP 应答 | 设备是否在 PXE 网段；dnsmasq `interface=` 与防火墙 67/69 |
| DHCP 后无 TFTP 下载 | TFTP 根是否只挂载了 `snponly.efi` 与 `boot.ipxe`；文件权限 |
| 卡在固件启动 | Secure Boot 是否开启；产物是否为对应架构与形态 |
| HTTPS 证书错误 | TOFU 指纹与控制面证书不匹配；证书轮换后未重新预置 |
| 设备不入池 | 注册窗口是否开启；上报是否携带有效公钥；设备是否被吊销 |
| 菜单为空或启动循环 | 设备是否绑定 Worker；Worker 是否有系统盘与默认盘 |
| 数据面连接失败 | `tlshd`、内核 TLS 配置、4421 端口与凭据推送状态 |

## 相关页面

- [固件概述](./index.md)
- [构建与产物](./build.md)
- [固件契约](../reference/firmware-contract.md)
- [引导链](../concepts/boot-chain.md)
- [部署指南](../guide/deployment.md)
