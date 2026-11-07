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
| EFI NVS 持久化 | `device-key` 与 `server-fingerprint` 写入 NVRAM，跨重启保留 |
| TOFU 信任 | 首次 HTTPS 握手固定服务器叶证书 SHA-256 指纹，此后不匹配即拒绝 |
| 设备身份命令 | `keygen` / `pubkey` / `sign` / `nvclear` |
| 网卡适配 | RTL8125/8126 原生驱动、`snponly` 本地引导回退 |
| 本地介质引导 | `embed/auto.ipxe`；`direct-uefi` 产物内置脚本，启动即进入网络引导链 |

补丁编号、网卡支持矩阵与字段级细节见[固件平台参考](../reference/firmware-platform.md)。

## 设备身份与信任

首次启动时，固件执行 `keygen`，在 EFI NVRAM 中生成 ECDSA P-256 私钥；私钥不出机器。后续启动发现密钥已存在时拒绝覆盖。`pubkey` 导出公钥后，固件向控制面上报设备指纹与公钥。

绑定与验签强制开启后，仅凭“知道某个 MAC”不足以取得凭据。控制面下发一次性 nonce，设备用私钥签名 `nonce‖mac`；验签通过的设备才会取得引导变量。设备密钥可以在线轮换，成功后旧密钥立即失效。

TOFU 是全局单锚：`server-fingerprint` 一旦存在，固件只信任首个 HTTPS 连接中被固定的证书。所有 HTTPS 目标必须共享同一信任锚；控制面证书轮换后，必须先清除 NVRAM 指纹，否则设备无法再次连接。

安全边界：

- **私钥不出设备**：`device-key` 仅供固件内部使用；公钥与签名可导出。
- **私钥可被启动后的系统读取**：设备私钥保存在带运行时访问权限的 EFI 变量中，启动后的操作系统可以读取。Secure Boot 不关闭这一读取路径，详见[安全模型](../architecture/security.md)。
- **首次接触无认证**：TOFU 在注册窗口内接受首个证书，中间人风险由 PXE 网段隔离承担。
- **nonce 必须一次性**：签名可被重放的前提是 nonce 重复；控制面需保证 nonce 一次性、短 TTL 并绑定 MAC。

命令、NVRAM 设置、签名格式、挑战-应答与 TOFU 状态见[固件契约](../reference/firmware-contract.md)。

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
- 主线固件未签名；Secure Boot 开启时无法加载，需在部署前关闭。
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
