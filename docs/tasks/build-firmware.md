# 构建签名固件

固件在独立仓库 [kurrent-firmware](https://github.com/dutyc/kurrent-firmware) 中构建：拉取固定基线的上游 iPXE、按序应用补丁集、嵌入静态脚本与信任锚，产出 UEFI 产物。部署链路的首跳使用 `build-sb.sh` 的签名产物。

![固件流水线：从上游 iPXE 基线到设备网络引导](/diagrams/zh/firmware-pipeline.svg)

## 开始之前

- Linux x86_64 构建机，具备 `git`、`make`、`gcc`、`openssl`、`python3` 与 `sbsigntool`（`sbsign` / `sbverify`）。
- 能访问上游 iPXE 仓库，首次构建会缓存源码。
- 签名构建需要产物签名密钥与证书、boot CA 证书；需要首次网络启动写入平台密钥时，另需 PK / KEK 的离线密钥与证书。

## 签名构建

```bash
git clone https://github.com/dutyc/kurrent-firmware
cd kurrent-firmware
SB_KEY=/path/to/artifact-signing.key \
SB_CERT=/path/to/artifact-signing.crt \
BOOT_CA_CERT=<状态根>/control-plane/state/pki/ca/boot/ca.crt \
SB_PK_KEY=/path/to/pk.key SB_PK_CERT=/path/to/pk.crt \
SB_KEK_KEY=/path/to/kek.key SB_KEK_CERT=/path/to/kek.crt \
./build/build-sb.sh
```

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `SB_KEY` / `SB_CERT` | 是 | 产物签名密钥与证书 |
| `BOOT_CA_CERT` | 是 | 控制面 boot CA 证书，内嵌进镜像 |
| `BOOT_HTTPS_PORT` | 否 | 引导 HTTPS 端口，默认 `4443`，内嵌进镜像，须与控制面一致 |
| `CONTROLLER_IP` | 否 | 显式固定控制面地址；省略时由设备从 DHCP `next-server` 取得 |
| `SB_PK_KEY` / `SB_PK_CERT` / `SB_KEK_KEY` / `SB_KEK_CERT` | 成组 | 平台密钥注册的 PK / KEK 离线密钥与证书，四者同设或同缺；缺少时镜像不含注册数据，`sbenroll` 恒报 `failed` |
| `SB_MS_UEFI_CA_CERT` | 否 | 写入 `db` 的 Microsoft UEFI CA 证书，默认 `build/certs/MicrosoftUEFI-CA-2011.pem` |
| `SB_OWNER_GUID` | 否 | 平台密钥的 Owner GUID，默认 `a0baa8a3-041d-48a8-bc87-c36d121b5e3d` |
| `KURRENT_SB_TARGET` | 否 | `snponly`（默认）或 `ipxe`，选择构建目标 |

构建脚本据此生成 `sbenroll` 的 PK / KEK / db 签名描述符，db 自动包含产物签名证书与 Microsoft UEFI CA。私钥只参与构建期签名，不进仓库、不进镜像。产物为 `dist/secureboot/snponly-sb.efi`（`KURRENT_SB_TARGET=ipxe` 时为 `ipxe-sb.efi`）与 `manifest.json`。

普通构建 `./build/build.sh` 不签名，产物只用于本地调试与研究；开启 Secure Boot 的目标机拒绝加载。`UPSTREAM_COMMIT=<sha>` 指定上游基线，`JOBS` 控制并行度。

## 流水线

1. 拉取上游 iPXE 并检出固定基线（当前 `e6e51ccb`）。
2. 按文件名顺序应用 `patches/*.patch`，任一补丁无法应用即失败。
3. 安装 `embed/auto.ipxe` 作为 `direct-uefi` 的内置脚本；签名构建再嵌入静态引导脚本、boot CA 与引导端口，并由 `sbsign` 签名。
4. 构建 UEFI 目标，复制到 `dist/`。
5. 生成 `dist/SHA256SUMS` 与签名产物的 `manifest.json`。

补丁基线、逐条职责与升级流程见固件仓库 `patches/README` 与[固件平台参考](../reference/firmware-platform.md#补丁集)。

## 产物

普通构建 `build.sh` 产出：

| 产物 | 形态 | 用途 |
| --- | --- | --- |
| `pxe-uefi/ipxe.efi` | PXE 网络启动 | 主板 PXE 链加载，脚本由 DHCP 下发 |
| `pxe-uefi/snponly.efi` | PXE 网络启动 | 默认首跳 |
| `pxe-uefi/ipxe-debug.efi`、`snponly-debug.efi` | 同上，debug | 网卡初始化与引导定位 |
| `direct-uefi/ipxe.efi` | UEFI 直接引导，内置脚本 | USB/ESP、本地磁盘或 GRUB 链加载 |
| `direct-uefi/ipxe-debug.efi` | 同上，debug | 故障定位 |
| `direct-uefi/snponly.efi` | UEFI 直接引导 | 本地介质引导 |

| 场景 | 选择 |
| --- | --- |
| 主板支持 UEFI Network Boot | `pxe-uefi/snponly.efi`，作为默认首跳 |
| 主板无 PXE 入口，需 USB/ESP 引导 | `direct-uefi/ipxe.efi` |
| 网卡初始化失败 | 对应的 `-debug` 产物 |

签名构建 `build-sb.sh` 的产物与上表对应：`dist/secureboot/snponly-sb.efi` 对应 `pxe-uefi/snponly.efi`，`KURRENT_SB_TARGET=ipxe` 的 `ipxe-sb.efi` 对应全驱动首跳。主仓库的 TFTP 根只放这两份签名产物，交付方式见[交付签名固件](./deploy-firmware.md)。

## 校验

```bash
cd dist && sha256sum -c SHA256SUMS
sbverify --cert <artifact.crt> dist/secureboot/snponly-sb.efi
```

签名产物的哈希以 `manifest.json` 的 `artifact_sha256` 为准，控制面 `/boot-assets` 会与镜像实际哈希、端口与 boot CA 指纹核对。发布页资产使用扁平命名，校验文件内保留目录结构。

## 接下来

- 交付到控制面：[交付签名固件](./deploy-firmware.md)
- 首次启动写入平台密钥：[预置目标机信任库](./enroll-platform-keys.md)
- 信任锚：[引导信任](../concepts/boot-chain/trust.md)
- 补丁集与网卡矩阵：[固件平台参考](../reference/firmware-platform.md)
