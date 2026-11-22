# 固件组成

固件由固定基线、补丁集与嵌入内容共同构建，不是单个二进制。

## 上游基线

固件仓库不保存 iPXE 源码。构建时按固定提交拉取上游 iPXE，并在该提交上应用补丁；当前基线为 `e6e51ccb`。基线随固件版本一起固定，升级基线意味着整组补丁重新落位，需要重新执行构建与验证。

## 补丁集

补丁按文件名顺序应用，覆盖以下几类能力：

- 网卡驱动：Realtek RTL8125 与 RTL8126 的原生适配，debug 目标继承完整驱动集；
- 引导路径：本地介质引导时接管 SNP/NII/MNP 设备；
- 设备信息：SMBIOS 内存字段与 PCI 网卡芯片名；
- 数据面：NVMe over TCP SAN 驱动、DH-HMAC-CHAP 认证时序与 `hostnqn`；
- 设备身份：TPM 身份命令与 EFI 变量 NVS 后端；
- 引导信任：TOFU 指纹固定与校验、TLS 1.3 PSK 客户端；
- 交接：NBCT ACPI 表与内核凭据交接。

任一补丁无法应用到固定基线，构建即失败，不会产出混合版本。逐条编号与职责见[固件平台参考](../reference/firmware-platform.md#补丁集)。补丁的许可边界不完全一致，再分发前应阅读固件仓库的 `patches/README`。

## 嵌入内容

构建时写入镜像的内容分两类。

普通构建嵌入 `embed/auto.ipxe`，作为 `direct-uefi` 产物的内置脚本。

签名构建在普通构建之上再嵌入三个静态引导脚本、boot CA 证书与引导 HTTPS 端口，并由 `sbsign` 签名；控制面地址默认不嵌入。

因此静态脚本、信任锚与端口都是镜像的一部分：改菜单、改变量、换 boot CA 或换端口都必须重新构建并签名，不能单独替换。签名构建的输入与产物见 [Secure Boot 预置](./secure-boot.md)。

## 构建形态

固件仓库提供两条构建命令：

- `build/build.sh` 产出未签名产物，用于本地调试与研究；开启 Secure Boot 的目标机拒绝加载；
- `build/build-sb.sh` 需要 `SB_KEY`、`SB_CERT`、boot CA 证书与引导端口，产出具名产物与 `manifest.json`，是部署路径使用的形态。

## 产物形态

产物共 7 个 UEFI 形态，按引导路径分为 PXE 与 direct-uefi 两组，每组提供正式版、debug 版与 `snponly` 变体。PXE 产物由主板网络启动加载，脚本随 DHCP 下发；direct-uefi 产物内置脚本，用于 USB、ESP、本地磁盘或 GRUB 链加载。

产物清单、选择原则与校验方式见[构建](./build.md#产物与校验)。

## 驱动策略

固件优先使用内置原生驱动，覆盖以 2.5G 及以上速率为主的服务器网卡；自带 UEFI 网络栈的网卡可以直接使用 `snponly` 产物，不额外移植驱动。USB 网卡与现代无线网卡不在支持范围内。

网卡矩阵、已知问题与实测记录见[固件平台参考](../reference/firmware-platform.md#网卡支持)。

## 接下来

- [固件概述](./index.md)
- [引导信任](./trust.md)
- [固件平台参考](../reference/firmware-platform.md)
