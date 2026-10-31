# 固件概述

Kurrent 的设备固件由独立仓库 [kurrent-firmware](https://github.com/dutyc/kurrent-firmware) 构建。它不包含 iPXE 源码，只维护差异补丁与构建资产，可固定在任意上游基线之上重建。

```text
固件仓库构建 dist/ 产物
        │
        ▼ 部署
主仓库 tftp/snponly.efi
        │
        ▼ 设备网络引导
控制面 /boot-vars + 数据面
```

## 职责边界

| 仓库 | 负责 | 交付物 |
| --- | --- | --- |
| kurrent-firmware | 协议驱动、认证、设备信息采集、网卡适配、固件构建 | `dist/` 产物、补丁集、测试工具 |
| kurrent（主仓库） | 控制面、引导脚本、注册窗口、身份与凭据投影、系统盘 | 控制面服务、`tftp/` 脚本、API |

固件不感知业务策略：注册窗口、Worker 绑定、盘选择与凭据签发都在主仓库。固件只提供命令与协议能力，引导脚本决定调用顺序。

## 固件能力

| 能力 | 说明 |
| --- | --- |
| NVMe/TCP SAN 引导 | 原生 `nvmetcp` 驱动；上游 iPXE 不具备该能力 |
| DH-HMAC-CHAP | SHA-256/384/512、DH 组 2048/3072/4096；DHHC-1 格式校验 |
| NBCT 凭据表 | 把会话密钥与 Host NQN 交给 initramfs，避免二次网络取凭据 |
| `hostnqn` 设置 | 按 MAC/Worker 指定发起端身份，支持 target 严格模式 |
| 设备信息上报 | 采集 11 项硬件指纹供 `/devices/report` 使用 |
| EFI NVRAM | `device-key` 与 `server-fingerprint` 跨重启保留 |
| TOFU | 首次 HTTPS 握手固定服务器叶证书指纹 |
| 设备密钥命令 | `keygen` / `pubkey` / `sign`（ECDSA P-256） |
| 网卡适配 | RTL8125 / RTL8126 原生驱动；其他网卡走上游驱动或 SNP |
| 本地介质引导 | `direct-uefi` 产物内置脚本，启动即进入网络引导流程 |

## 产物

构建流水线产出 7 种 UEFI 产物，分两类：

| 目录 | 用途 |
| --- | --- |
| `pxe-uefi/` | 主板 PXE 链加载；脚本由 DHCP/TFTP 下发 |
| `direct-uefi/` | 本地介质或 GRUB 链加载；内置 `auto.ipxe`，启动即进入引导链 |

每类包含 `ipxe.efi`、`snponly.efi` 与 debug 版本（`direct-uefi` 不含 `snponly-debug`）。完整列表、选用与校验见[构建与产物](./build.md)。

## 当前状态

- 生产栈全链路已在受控环境中验证：DHCP → TFTP → iPXE → 控制面 → NVMe/TCP 认证（固件段 + 内核段）→ GRUB → rootfs → 登录提示符。
- 设备身份、TOFU、NVRAM 与 NBCT 已在整机链路中使用。
- 主线固件未签名；Secure Boot 开启时需要关闭或使用单独的签名产物，后者不属于当前主线部署路径。
- iPXE 侧生成 NBFT 仍待实现；当前验证链路使用注入的 NBFT 表模拟固件预生成阶段。
- HTTPS 使用 iPXE 原生 TLS 实现，上限 TLS 1.2；数据面 TLS 1.3 由固件的 NVMe/TCP 路径完成。
- 网卡支持以固件仓库的支持矩阵为准：RTL8126 为编译验证，部分型号依赖 SNP 兜底。

## 与主仓库的契约

固件在引导链上的入口与出口：

```text
DHCP（next-server）
  → TFTP /boot.ipxe
  → HTTPS boot.ipxe.cfg / menu.ipxe
  → /devices/report        指纹 + 公钥
  → /devices/challenge     一次性 nonce
  → /boot-vars             盘 NQN、Host NQN、DHHC-1、TLS PSK
  → sanboot nvme://...
```

固件消费主仓库下发的变量，并把会话信息通过 NBCT 交给内核。字段级契约见[固件契约](../reference/firmware-contract.md)。

## 获取方式

- **发布产物**：从固件仓库 Releases 下载 `pxe-uefi-snponly.efi`、`direct-uefi-ipxe.efi` 等资产，校验 `SHA256SUMS`。
- **自行构建**：按[构建与产物](./build.md)固定上游基线、应用补丁并构建。

主仓库不跟踪固件二进制，`tftp/snponly.efi` 需要由部署方放入。发布产物的选择与校验命令见[部署指南](../guide/deployment.md#prepare-device-firmware)。

## 边界与限制

- 固件不解决首跳信任：DHCP/TFTP 内容由设备直接信任，部署需要隔离 PXE 网段。见[安全模型](../architecture/security.md)。
- 固件不解决密钥被启动系统读取的问题：设备私钥在 NVRAM 中，主线未使用 TPM 度量。
- 固件不管理运行态身份：设备密钥的生成、轮换与绑定由主仓库控制面编排。
- 固件仓库遵循 GPL-2.0；第三方驱动适配可能有更严格的许可边界，见其 `patches/README`。

## 相关页面

- [构建与产物](./build.md)
- [预置与刷写](./provision.md)
- [固件契约](../reference/firmware-contract.md)
- [引导链](../concepts/boot-chain.md)
- 固件仓库文档：<https://github.com/dutyc/kurrent-firmware>
