# 引导信任

引导链的三段信任各自独立成立：首跳二进制由目标机 UEFI 验签，引导期间的 HTTPS 只信任签名镜像内嵌的 boot CA，设备身份由 TPM 证明承担。任一环节不成立，启动在该环节停止，不会改走其他路径。

| 环节 | 信任锚 | 验证时机 | 失败后果 |
| --- | --- | --- | --- |
| 首跳二进制 | 目标机 `db` 中的站点签名证书 | 固件加载 TFTP 首跳时 | 未签名或篡改的镜像不执行 |
| 引导 HTTPS | 签名镜像内嵌的 boot CA | 4443 引导 HTTPS 握手 | 拒绝取得脚本与变量，无首次连接信任回落 |
| 设备身份 | TPM AK 与 PCR 0/2/4/7 基线 | 每次引导的 nonce + quote | 不下发凭据 |

## 首跳验签

UEFI 在执行首跳之前用 `db` 校验其签名。站点签名证书属于一次性预置：首次网络启动时由平台注册窗口写入目标机 `db`（出厂已锁厂商密钥的机型经其厂商的管理流程处理），之后不参与每次启动，也不依赖控制面在线。写入流程见[预置目标机信任库](../../tasks/enroll-platform-keys.md)。

首跳把静态脚本、boot CA 与引导 HTTPS 端口一并签进二进制，控制面地址默认不写入；脚本、信任锚与端口因此都不能单独替换，改动任何一项都须重新构建并签名。

## 引导 HTTPS

签名镜像通过 `TRUST=` 与 `CERT=` 固定 boot CA，握手阶段只接受该 CA 签发的证书。这里没有首次连接信任，也没有可关闭校验的开关；boot CA 轮换等同于重新构建固件。

未签名调试产物是唯一采用首次连接信任（TOFU）的形态：首次连接时把叶证书 DER 的 SHA-256 写入 NVRAM 的 `server-fingerprint` 并镜像到 `trust`，之后指纹不匹配即拒绝握手，且不覆盖已有指纹。调试产物在控制面证书轮换后须先执行 `clear nvo/server-fingerprint` 重新固定，否则无法引导。字段与行为见[固件契约](../../reference/firmware-contract.md#tofu-未签名调试产物)。

## 设备身份

设备身份是 TPM 内的证明密钥（AK），私钥在 TPM 内创建、不可导出，固件也不读取私钥明文。每次启动对 PCR 0/2/4/7 生成 SHA-256 quote，nonce 原样写入 `extraData`；控制面验签、比对平台基线并确认 nonce 绑定后，才在 `/boot-vars` 中注入凭据。机制见[设备身份](./device-identity.md)。

## 控制面产物

控制面启动时幂等生成引导链证书，与组件三分根分离：

- `state/pki/ca/boot/ca.crt|ca.key`：boot CA，10 年，只签引导端点证书。
- `state/certs/boot-server.crt|boot-server.key`：4443 叶证书，1 年，SAN 复用 `spec.serverCert.san`；剩余有效期不足 30 天时自动重签，不换 boot CA。

nginx 的 4443 server 只反代引导端点（`/boot/phase`、`/devices/enroll-report`、`/boot-vars`、`/devices/report`、`/devices/challenge`），不要求客户端证书，未知路径直接 404；管理面、WebUI 与 `/api/cp/` 不在该端口。

宿主端口用 `KURRENT_BOOT_HTTPS_PORT` 调整，必须与固件构建时的 `BOOT_HTTPS_PORT` 一致：端口内嵌在签名镜像中，`/api/cp/boot-assets` 的 `secureboot.port_matches` 校验这一点。

## 信任变化的影响

| 变化 | 需要做的操作 |
| --- | --- |
| 更换固件签名密钥或证书 | 重新构建签名产物，并在目标机重新写入 `db` |
| 更换 boot CA | 重新构建签名产物；boot CA 指纹内嵌在镜像中 |
| 更换引导 HTTPS 端口、静态脚本或显式控制面地址 | 重新构建并签名 |
| 更新固件或改变 Secure Boot 策略 | PCR 变化，证明按 `pcr_mismatch` 拒绝，核对后批准新基线 |
| 清除 TPM 或更换主板 | 原 AK 丢失，删除设备登记并在注册窗口内重新入池 |

## 边界

- Secure Boot 只覆盖首跳二进制与引导 HTTPS；`sanboot` 之后的加载器由母盘 ESP 的发行版签名链决定。
- 不做防回滚：旧版本签名固件仍可通过校验，回滚控制依赖发布流程。
- 部分 PXE 固件不校验下载的网络引导程序；条件允许时优先使用本地介质或受控 HTTP 引导。

## 接下来

- 返回[引导链](../boot-chain.md)
- 构建签名镜像：[构建签名固件](../../tasks/build-firmware.md)
- 写入目标机信任库：[预置目标机信任库](../../tasks/enroll-platform-keys.md)
- 接口与变量：[固件契约](../../reference/firmware-contract.md)
- 威胁模型：[安全模型](../architecture/security.md)
