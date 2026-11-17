# 引导链

一台设备从加电到挂载根文件系统，要依次回答三个问题：执行的代码是否可信、请求方是不是那台设备、应该连哪个存储目标。传统装机把答案固定在机器和磁盘里，Kurrent 不这样做：每次启动，控制面按当前的绑定关系把答案投影给设备。这样系统才能在不同机器之间流动，而机器本身不携带任何固定身份。

主路径为 NVMe/TCP + TLS 1.3：系统盘通过网络以 NVMe 协议访问，传输全程加密。

## 一次启动的三个阶段

| 阶段 | 回答的问题 | 信任锚 | 失败结果 |
| --- | --- | --- | --- |
| 首跳 | 代码是否可信 | UEFI `db` 站点证书 | 固件拒绝执行 |
| 身份 | 是否那台设备 | TPM AK + PCR 基线 | 不入池、无凭据 |
| 数据面 | 连哪个 target | DHHC-1 + TLS 1.3 | 拒绝连接 |

![引导链：首跳、设备身份与数据面三个阶段](/diagrams/boot-chain.svg)

三个阶段依次进行，后一阶段以前一阶段的结论为前提。任一阶段失败，启动即停止，不会改走其他路径。

## 首跳

设备开机后先通过 DHCP 拿到地址和 `next-server`（下一跳服务器地址），再从 TFTP 下载第一个执行文件 `snponly.efi`。这是整个引导链里唯一一段无法用网络手段保护的过程——下载本身是明文的，所以可信性必须由目标机固件自己保证。

这个文件必须经过签名，签名证书要预先导入目标机的 UEFI `db`（固件内置的受信签名证书库）。验签不通过，固件直接拒绝执行，网络引导不会开始。

签名镜像里内嵌了三样东西：

- 静态引导脚本
- boot CA 证书
- 引导 HTTPS 端口（默认 4443）

内嵌的意义在于：脚本与信任锚随镜像一起被签名保护，攻击者无法单独替换其中任何一项。控制面地址默认不在镜像里，由 DHCP 提供。此后的菜单、变量与设备接口都只通过 boot CA 锚定的 HTTPS 下载。

## 设备身份

首跳解决了「代码可信」，接下来要解决「设备是谁」。首次启动时，固件在本机 TPM 内创建证明密钥（AK），私钥不可导出，因此这把密钥只能代表这台物理机器。设备向控制面上报指纹与 AK 公钥后按 MAC 入池。

登记不产生权限。每次启动，设备用 AK 对 PCR 0/2/4/7 生成 quote——TPM 对这几个启动度量寄存器当前值的签名报告。控制面把报告里的值与平台基线比对，确认启动环境没有被改动。证明通过的设备才可能取得凭据。

## 数据面

身份确认后，控制面把当前绑定投影成一组启动变量。`/boot-vars` 依次检查识别、绑定、证明与凭据条件，一次成功请求的返回形如：

```ipxe
#!ipxe
# boot vars for worker-01
set disk-nqn nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89
set storager-ip 192.168.80.3
set hostnqn nqn.2026-07.com.kurrent:host.worker-01
set nbft-secret DHHC-1:01:<base64>
set nbft-tls-psk <64 hex>
set nbft-tls-port 4421
set os ubuntu
set menu-default boot-os
set menu-timeout 5000
```

这些变量告诉固件：连哪台存储节点（`storager-ip`）、挂哪块盘（`disk-nqn`）、以什么身份连（`hostnqn`）、用什么密钥认证（`nbft-secret`）和加密（`nbft-tls-psk`）。固件按这些变量在端口 `4421` 上完成 DH-HMAC-CHAP 认证与 TLS 1.3 握手，没有明文回退。示例中的密钥与 PSK 为占位符，实际值只下发给证明通过的设备。

## 交接给内核

固件建立的连接只服务引导阶段；操作系统启动后，根文件系统要由内核自己挂载。为此固件在 `sanboot` 时安装 NBCT 凭据表，记录本次会话的 target、NQN 与 DHHC-1 密钥。

initramfs 里的 `nbft-connect` 读取 NBCT 与 NBFT（网络与子系统发现信息表），在同一端口以 `--tls` 重新连接并挂载根文件系统。盘内不包含固定的机器身份，内核拿到的一切连接信息都来自本次启动的投影。

## 失败与观测

任一阶段失败都不会回退到其他路径：

- 首跳验签失败：固件不执行。
- 身份或准入失败：`/boot-vars` 返回空脚本。
- 数据面缺凭据或端口未就绪：拒绝下发。

设备反复重启通常是未绑定或没有默认盘。原因码写入审计与设备的证明状态，管理员可以在 WebUI 设备详情里看到。

## 信任边界

首跳（DHCP → TFTP `snponly.efi`）由 UEFI Secure Boot 验证。目标机 `db` 只信任站点签名证书，未签名或被篡改的首跳不会执行，内嵌的静态脚本也随之不可替换。

首跳之后的 HTTPS 由内嵌的 boot CA 锚定，设备身份由 TPM AK 与 PCR 基线保护。

因此，同二层攻击者能造成拒绝服务（伪 DHCP/ARP、链路中断），但无法替换首跳、替换脚本或冒充引导服务器。被控宿主、运行期被控设备与物理访问不在该边界内解决。

## 固件分工

| 能力 | 提供方 |
| --- | --- |
| DHCP/TFTP 客户端、HTTPS、boot CA 固定、NVRAM | 固件 |
| TPM AK 创建、PCR quote | 固件 |
| NVMe/TCP 驱动、DH-HMAC-CHAP、NBCT | 固件 |
| 引导脚本、注册窗口、`/boot-vars` 准入 | 主仓库控制面 |
| 系统盘、凭据、菜单生成 | 主仓库控制面 |

## 相关页面

- 各阶段的完整机制：[首跳](./boot-chain/first-hop.md)、[设备身份](./boot-chain/device-identity.md)、[数据面](./boot-chain/data-plane.md)、[交接给内核](./boot-chain/handover.md)
- 固件构建与预置：[固件](../firmware/index.md)
- AK、quote 格式与信任锚：[固件契约](../reference/firmware-contract.md)
- 盘、凭据与数据面：[存储与数据面](./storage.md)
- 完整威胁模型：[安全模型](../architecture/security.md)
- 失败后的操作入口：[日常运维](../guide/operations.md)