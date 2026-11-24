# 固件概述

固件是设备在操作系统之前运行的那一层。它把 UEFI 的网络能力扩展成一条可验证、可认证的 NVMe/TCP 引导路径，并在把连接交给内核之前完成设备身份证明。

固件由独立仓库 [kurrent-firmware](https://github.com/dutyc/kurrent-firmware) 构建，产物是主仓库引导链的第一环。它基于固定的上游 iPXE 基线，加上本项目的补丁集与嵌入内容构建而成，协议能力与控制面因此可以按各自的节奏发布。

![固件流水线：从上游 iPXE 基线到设备网络引导](/diagrams/zh/firmware-pipeline.svg)

## 启动过程中的固件

一次启动里，固件依次完成网络引导、身份证明、可信连接与交接。

### 网络与脚本

固件加载网卡驱动，执行 DHCP 取得地址与 `next-server`，从 TFTP 取回引导指针，再从控制面的 4443 端口取回引导脚本与变量。

签名产物只信任构建时嵌入的 boot CA，不采用首次连接信任；首跳二进制本身由目标机 UEFI `db` 验签。

### 设备身份

设备身份是 TPM 2.0 证明密钥（AK）。首次启动时固件在 TPM 内创建受限签名密钥并持久化到 TPM NV，私钥不可导出；之后每次启动复用同一个 AK，对 PCR 0/2/4/7 生成 quote。控制面验签并比对平台基线，通过后才向该设备下发凭据。

### 数据面连接

固件内置 `nvmetcp` 驱动，按 NVMe/TCP 协议连接系统盘所在的 target：ICReq/ICResp 协商、Admin 队列 Connect、DH-HMAC-CHAP 认证、控制器使能、Identify、I/O 队列建立，随后进入块读写。

认证通过后，`sanboot` 按控制面下发的 PSK 建立 TLS 1.3，使固件段与内核段在同一端口上形成全程加密。

### 交接给内核

连接建立后，固件把凭据与发现信息写入 NBCT，内核在 initramfs 阶段据此重连数据面并挂载根文件系统。固件不生成 NBFT 表；当前验证链路注入该表模拟交接阶段。

完整的信任前提、失败表现与各段连接见[引导链](../concepts/boot-chain.md)。

## 与控制面的分工

固件负责协议驱动、认证、设备身份命令与信任锚固定。注册窗口、Worker 绑定、系统盘选择与凭据签发属于控制面：设备每次启动只陈述自己能证明什么，是否入池、取得哪块盘由控制面判定。

设备侧看到的启动脚本由控制面生成；证明失败时设备得到空脚本，原因码写入审计与设备记录。两侧的校验顺序见[设备身份](../concepts/boot-chain/device-identity.md)。

## 固件状态

控制面在 `/api/cp/boot-assets` 汇总固件状态：首跳文件及其能力、第二段脚本可达性、菜单、Secure Boot 产物与数据面端口。WebUI 的「引导链」页展示同一组结果，并按固件、服务、数据面、Worker、上报逐项给出当前能否引导。

`firmware.status` 为 `incomplete` 通常表示 TFTP 根中放的是上游 iPXE 而非本项目产物；`secureboot.status` 不是 `ok` 时，按镜像哈希、端口与 boot CA 三项分别核对。

## 运行边界

- 仅支持 UEFI 与 NVMe/TCP，不提供 Legacy/CSM 形态，也不支持 RDMA 与 FC。
- 引导路径为单控制器、单命名空间；Identify 解析 512 B 与 4 KB 块。
- DH-HMAC-CHAP 为单向认证，双向认证不在固件实现范围内。
- 引导 HTTPS 使用 iPXE 原生 TLS，上限 TLS 1.2；数据面使用 TLS 1.3 PSK，两者相互独立。
- 未签名产物只用于本地调试；开启 Secure Boot 的目标机拒绝加载，也不满足设备入池条件。

## 本章内容

- 固件由哪些基线、补丁与嵌入内容构成：[固件组成](./composition.md)
- 首跳、引导 HTTPS 与设备身份各自信任什么：[引导信任](./trust.md)
- 从上游基线构建并签名：[构建](./build.md)
- 把产物放进 PXE 网段或本地介质，完成首次启动：[部署与预置](./deploy.md)
- 预置目标机信任库、交付签名产物：[Secure Boot 预置](./secure-boot.md)
- 补丁集、网卡矩阵、Target 配置：[固件平台参考](../reference/firmware-platform.md)
- 固件与主仓库之间的命令与变量：[固件契约](../reference/firmware-contract.md)
- 发行版侧脚本与家族差异：[发行版矩阵](../reference/distributions.md)

## 接下来

- [引导链](../concepts/boot-chain.md)
- [安全模型](../concepts/architecture/security.md)
- 固件仓库源码、补丁与研究记录：<https://github.com/dutyc/kurrent-firmware>
