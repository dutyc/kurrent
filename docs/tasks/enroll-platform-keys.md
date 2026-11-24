# 预置目标机信任库

**平台密钥注册**在首次网络启动时把 Secure Boot 平台密钥（PK / KEK / db）写入目标机固件。签名首跳要经 UEFI 验签才允许执行，站点签名证书必须先进入目标机 `db`；平台注册窗口为批量机队提供网络写入通道，不需要逐机导入。

## 开始之前

- 已按[构建签名固件](./build-firmware.md)构建含 `sbenroll` 注册数据的签名镜像，并[交付](./deploy-firmware.md)到 TFTP 根或本地介质。
- 目标机处于 Setup Mode 或 Secure Boot 关闭；出厂已锁厂商密钥并开启 Secure Boot 的机型无法写入，须经其厂商的管理流程处理。
- 注册在受控网段执行：Setup Mode 下同网段任意主机都能写入密钥，窗口开启期间不应接入未受控设备。

## 写入内容

`sbenroll` 按 PK → KEK → db 顺序写入，变量与签名描述符在构建签名镜像时提供（`SB_PK_*` / `SB_KEK_*` / `SB_MS_UEFI_CA_CERT`）：

- db 含产物签名证书与 Microsoft UEFI CA 2011。
- 私钥离线保管，镜像内只嵌公开证书与签名描述符。

## 步骤

1. 在 WebUI 设备页（或 `POST /api/cp/settings/platform-enrollment`）开启平台注册窗口。
2. 目标机网络启动签名镜像：`sbenroll` 写入密钥后向 `/devices/enroll-report` 上报结果，首次 `ok` 自动重启。
3. 重启后固件验签执行同一镜像；WebUI 设备页「平台注册」清单出现该 MAC，即完成写入。
4. 全部设备注册完成后关闭窗口：新机器不再执行注册流程，已注册设备与清单保留。

> **说明：** 写入 PK / KEK / db 只让固件接受签名首跳。设备入池仍要求 TPM AK、`sb=1` 与注册窗口；登记不代表授权。

## 结果语义

| `sbenroll` 结果 | 行为 |
| --- | --- |
| `ok`（首次） | 写入成功，上报后自动重启 |
| `ok`（已注册） | PK 已存在且相同，不重写变量，继续引导 |
| `foreign` | 目标机已装其他 PK，变量不动，继续引导 |
| `failed` | 写入失败（或镜像不含注册数据），继续引导 |

## 验收

- 目标机重启后进入签名引导，设备上报 `sb=1`。
- WebUI 设备页「平台注册」清单出现该 MAC。
- 导出目标机变量核对 PK / KEK / db 内容与写入顺序。
- 已注册设备重复执行不写变量；预置外来 PK 的设备结果 `foreign`、变量不变。

## 接下来

- 构建签名镜像：[构建签名固件](./build-firmware.md)
- 入池与首次启动：[部署](../setup/deployment.md)
- 信任锚：[引导信任](../concepts/boot-chain/trust.md)
- 接口细节：[引导与设备准入 API](../reference/api/boot.md)
