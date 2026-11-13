# 固件概述

Kurrent 的设备固件由独立仓库 [kurrent-firmware](https://github.com/dutyc/kurrent-firmware) 构建。仓库不保存 iPXE 源码，只在上游固定基线 `e6e51ccb` 之上维护差异补丁与构建资产。

固件负责设备侧的引导接入：NVMe/TCP SAN 引导、DH-HMAC-CHAP 认证、NBCT 凭据交接、设备身份密钥与 TOFU。注册窗口、Worker 绑定、系统盘选择与凭据签发属于控制面。

![固件流水线：从上游 iPXE 基线到设备网络引导](/diagrams/firmware-pipeline.svg)

## 职责边界

| 仓库 | 负责 | 交付物 |
| --- | --- | --- |
| kurrent-firmware | 协议驱动、认证、设备信息采集、网卡适配、固件构建 | `dist/` 产物、补丁集、测试工具 |
| kurrent（主仓库） | 控制面、引导脚本、注册窗口、身份与凭据投影、系统盘 | 控制面服务、`tftp/` 脚本、API |

固件不感知业务策略：注册窗口、Worker 绑定、盘选择与凭据签发都在主仓库。固件只提供命令与协议能力，引导脚本决定调用顺序。

## 能力总览

| 能力 | 说明 |
| --- | --- |
| NVMe/TCP SAN 引导 | 原生 `nvmetcp` 驱动，覆盖 ICReq 协商、Connect、Property Set、Identify、I/O 队列与 R2T 读写 |
| DH-HMAC-CHAP 认证 | 单向认证；哈希 SHA-256/384/512，DH 组 0/2048/3072/4096 |
| NBCT 凭据表 | 把会话密钥、Host NQN 与 TLS 交接参数交给内核侧，避免二次网络取凭据 |
| TLS 1.3 PSK 数据面 | 固件段 `sanboot` 按 PSK 建立 TLS 1.3，与内核段共同形成全程加密 |
| Host NQN 设置 | 按 MAC 注入 `hostnqn`，支持 nvmet `allow_any_host=0` 严格模式 |
| 设备信息采集 | SMBIOS 身份与内存、CPUID、网卡字段，经 HTTP 上报控制面 |
| EFI NVS / TPM NV 持久化 | `server-fingerprint` 写入 NVRAM；AK 与平台状态存于 TPM NV，跨重启保留 |
| TOFU 与固定 boot CA | 未签名产物按首次 HTTPS 握手固定叶证书指纹；签名产物只信任构建时嵌入的 boot CA |
| 设备身份与证明 | TCG2 访问、AK 创建与导出、PCR 0/2/4/7 的 SHA-256 quote、`nvclear` |
| 网卡适配 | RTL8125/8126 原生驱动、`snponly` 本地引导回退 |
| 本地介质引导 | `embed/auto.ipxe`；`direct-uefi` 产物内置脚本，启动即进入网络引导链 |

补丁编号、网卡支持矩阵与字段级细节见[固件平台参考](../reference/firmware-platform.md)。

## 设备身份与信任

设备身份是 TPM 2.0 证明密钥（AK）：首次启动在 TPM 内创建 restricted signing 密钥（ECC P-256 或 RSA-2048）并持久化到 TPM NV；私钥不可导出，后续启动复用已存在的 AK，不自动重建。固件向控制面 `/devices/report` 上报设备指纹、AK 公钥（SPKI）、AK 名称与 Secure Boot 状态。

证明复用一轮问答：控制面下发一次性 nonce，设备对 PCR 0/2/4/7 生成 SHA-256 quote，nonce 原样写入 `extraData`；quote 与签名随 `/boot-vars` 提交。控制面验签并与平台基线比对，通过后才下发引导变量。首次证明通过时记录平台基线（TOFU），PCR 变化后需管理员批准新基线。

TOFU 是未签名产物的单锚：`server-fingerprint` 一旦存在，固件只信任首个 HTTPS 连接中被固定的证书。签名产物不采用首次连接信任，引导 HTTPS 只接受构建时嵌入的 boot CA 签发的证书；轮换 boot CA 等同于重刷固件。

安全边界：

- **AK 私钥不可导出**：私钥留在 TPM 内，启动后的系统无法读取或复制设备身份。
- **证明覆盖启动度量，不覆盖运行期**：PCR 0/2/4/7 反映固件、Secure Boot 策略与启动配置；被控的运行期系统仍可在轮换窗口内取得当前 PSK，详见[安全模型](../architecture/security.md)。
- **首跳依赖 Secure Boot**：设备入池要求 Secure Boot 开启；首跳文件由 UEFI 信任库验证签名，TFTP 只交付签名二进制。
- **服务器 TOFU 首次信任**：首次握手仍可能被中间人利用；控制面证书轮换后需重新信任。
- **nonce 必须一次性**：签名可被重放的前提是 nonce 重复；控制面需保证 nonce 一次性、短 TTL 并绑定 MAC。
- **AK 不可在线更换**：更换 AK 只能删除登记后在注册窗口内重新入池。

命令、TPM NV 设置、quote 格式、挑战-证明与 TOFU 状态见[固件契约](../reference/firmware-contract.md)。

## NVMe-oF 引导

固件内置自研 `nvmetcp` 驱动，提供 NVMe over TCP 的 SAN 引导能力：

- 协议流程：ICReq/ICResp 协商 → Connect（Admin）→ 认证 → Property Set（CC.EN=1）→ Identify（控制器/命名空间）→ I/O 队列 Connect → 块读写。
- 认证：DH-HMAC-CHAP 单向认证，哈希 SHA-256/384/512，DH 组 0/2048/3072/4096。
- 仅支持 TCP 传输；不支持 RDMA 与 FC。
- 引导路径为单控制器、单命名空间；Identify 解析 512 B 与 4 KB 块。

Target 配置、`sanboot` 示例、DHHC-1 格式与服务端错误见[固件平台参考](../reference/firmware-platform.md#target-配置)。启动变量的投影规则见[固件契约](../reference/firmware-contract.md#启动变量)。

## 运行边界

- 构建产物仅包含 UEFI 形态，共 7 个。
- 仅支持 NVMe/TCP；不支持 RDMA 与 FC。
- 引导路径为单控制器、单命名空间；Identify 解析 512 B 与 4 KB 块。
- DH-HMAC-CHAP 为单向认证；双向认证不在固件实现范围内。
- HTTPS 下载使用 iPXE 原生 TLS，上限 TLS 1.2；数据面 `sanboot` 使用 TLS 1.3 PSK。
- 部署产物由站点密钥签名并预置到目标机 `db`；未签名产物在开启 Secure Boot 的目标机上不会执行。
- 固件只提供命令与协议能力，不实现注册窗口、Worker 绑定、盘选择或凭据签发。
- iPXE 侧生成 NBFT 仍待实现；当前验证链路使用注入的 NBFT 表模拟固件预生成阶段。

## 相关页面

- [固件平台参考](../reference/firmware-platform.md)
- [固件契约](../reference/firmware-contract.md)
- [构建与部署](./build-deploy.md)
- [发行版矩阵](../reference/distributions.md)
- [引导链](../concepts/boot-chain.md)
- [安全模型](../architecture/security.md)
- 固件仓库源码、补丁与研究记录：<https://github.com/dutyc/kurrent-firmware>
