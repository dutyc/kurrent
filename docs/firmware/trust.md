# 引导信任

固件在三条独立路径上建立信任：首跳二进制由目标机 UEFI 验签，引导期间访问的 HTTPS 只信任构建时嵌入的 boot CA，设备身份由 TPM 证明承担。三者互不替代，任一环节不成立都会阻止引导或阻止凭据下发。

## 三个信任锚

| 对象 | 信任锚 | 失效后果 |
| --- | --- | --- |
| 首跳二进制（iPXE） | 目标机 UEFI `db` 中的站点签名证书 | 未签名或签名不匹配的固件不会执行 |
| 引导 HTTPS（4443） | 签名固件内嵌的 boot CA | 不是该 CA 签发的服务器证书一律拒绝，不回落首次连接信任 |
| 设备身份 | TPM AK 与 PCR 0/2/4/7 quote | 无 AK 或 quote 不通过时不下发凭据 |

## 首跳验签

UEFI 在执行首跳之前用 `db` 校验其签名。签名证书是一次性预置项：首次网络启动时由平台注册窗口写入目标机 `db`（出厂已锁厂商密钥的机型经其厂商的管理流程处理），之后不参与每次启动，也不需要网络可达的信任服务。未签名产物在这条路径上直接被 UEFI 拒绝，不会出现「校验失败但继续运行」的分支。

预置方式与检查命令见 [Secure Boot 预置](./secure-boot.md#预置目标机信任库)。

## 引导 HTTPS

签名固件通过 `TRUST=` 与 `CERT=` 固定 boot CA，握手阶段只接受该 CA 签发的证书。这里没有首次连接信任，也没有可关闭校验的开关；boot CA 的轮换等同于重新构建固件。

未签名调试产物是唯一采用 TOFU 的形态：首次连接时把叶证书 DER 的 SHA-256 写入 NVRAM 的 `server-fingerprint`，并镜像到 `trust`；之后指纹不匹配即拒绝握手，且不覆盖已有指纹。

调试产物在控制面证书轮换后必须先执行 `clear nvo/server-fingerprint` 重新固定，否则无法引导。TOFU 是全局单锚，所有 HTTPS 目标必须共用同一证书或可链到该证书。字段与行为见[固件契约](../reference/firmware-contract.md#tofu-未签名调试产物)。

## 设备身份

设备身份是 TPM 内的证明密钥（AK），私钥不可导出，固件也不读取私钥明文。每次启动对 PCR 0/2/4/7 生成 SHA-256 quote，nonce 原样写入 `extraData`；控制面验签、比对平台基线并确认 nonce 绑定后，才在 `/boot-vars` 中注入凭据。

平台侧的身份语义与失败原因见[设备身份](../concepts/boot-chain/device-identity.md)。

## 信任变化的影响

| 变化 | 需要做的操作 |
| --- | --- |
| 更换固件签名密钥或证书 | 重新构建签名产物，并在每台目标机更新 `db` |
| 更换 boot CA | 重新构建签名产物；boot CA 指纹内嵌在镜像中 |
| 更换引导 HTTPS 端口、静态脚本或显式控制面地址 | 重新构建并签名 |
| 更新固件或改变 Secure Boot 策略 | PCR 变化，证明按 `pcr_mismatch` 拒绝，核对后批准新基线 |
| 清除 TPM 或更换主板 | 原 AK 丢失，删除设备登记并在注册窗口内重新入池 |

## 边界

- Secure Boot 只覆盖第一段 iPXE 与引导 HTTPS；`sanboot` 之后的加载器由母盘 ESP 的发行版签名链决定。
- 不做防回滚：旧版本签名固件仍可通过校验，回滚控制依赖发布流程。
- 部分 PXE 固件不校验下载的网络引导程序；条件允许时优先使用本地介质或受控 HTTP 引导。

## 接下来

- [固件概述](./index.md)
- [Secure Boot 预置](./secure-boot.md)
- [固件契约](../reference/firmware-contract.md)
- [安全模型](../concepts/architecture/security.md)
