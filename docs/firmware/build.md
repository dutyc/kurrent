# 构建

固件在独立仓库 `kurrent-firmware` 的 `main` 分支上构建：拉取上游 iPXE 固定基线、应用补丁、嵌入脚本并产出 UEFI 产物。构建产物进入主仓库的 TFTP 根与引导链。

## 前提

- Linux x86_64 构建机，具备 `git`、`make`、`gcc`、`openssl`、`python3` 与 `sbsigntool`（`sbsign`/`sbverify`）；
- 能访问上游 iPXE 仓库，首次构建会缓存源码；
- 签名构建需提供签名密钥、签名证书与 boot CA 证书；端口与控制面地址有默认值，见下文。

## 签名构建

```bash
git clone https://github.com/dutyc/kurrent-firmware
cd kurrent-firmware
SB_KEY=/path/to/artifact-signing.key \
SB_CERT=/path/to/artifact-signing.crt \
BOOT_CA_CERT=<状态根>/control-plane/state/pki/ca/boot/ca.crt \
./build/build-sb.sh
```

签名构建必须提供签名密钥、签名证书与 boot CA 证书。端口和控制面地址可以省略：端口省略时为 `4443`，地址省略时由设备从 DHCP `next-server` 取得。只有要把控制面地址固定进镜像时，才设置 `CONTROLLER_IP`。

`build-sb.sh` 在普通构建之上嵌入静态脚本、boot CA 与引导 HTTPS 端口，产出经 `sbsign` 签名的首跳。构建输入、manifest 与预置步骤见 [Secure Boot 预置](./secure-boot.md)。

普通构建 `./build/build.sh` 不签名，产物只用于本地调试与研究；开启 Secure Boot 的目标机会拒绝加载。

```bash
./build/build.sh
UPSTREAM_COMMIT=<sha> ./build/build.sh   # 指定上游基线，补丁须兼容
JOBS=8 ./build/build.sh                  # 并行度
```

## 流水线

1. 拉取上游 iPXE 并检出固定基线（当前 `e6e51ccb`）；
2. 按文件名顺序应用 `patches/*.patch`，任一补丁无法应用即失败；
3. 安装 `embed/auto.ipxe` 作为 `direct-uefi` 的内置脚本；
4. 构建 UEFI 目标，复制到 `dist/`；
5. 生成 `dist/SHA256SUMS`。

补丁基线与升级流程见固件仓库 `patches/README`。升级上游前需逐个确认补丁仍可应用，并重新执行 QEMU 与整机验证。

## 产物与校验

| 产物 | 形态 | 用途 |
| --- | --- | --- |
| `pxe-uefi/ipxe.efi` | PXE 网络启动 | 主板 PXE 链加载，脚本由 DHCP 下发 |
| `pxe-uefi/snponly.efi` | PXE 网络启动 | 默认首跳 |
| `pxe-uefi/ipxe-debug.efi`、`snponly-debug.efi` | 同上，debug | 网卡初始化与引导定位 |
| `direct-uefi/ipxe.efi` | UEFI 直接引导，内置脚本 | USB/ESP、本地磁盘或 GRUB 链加载 |
| `direct-uefi/ipxe-debug.efi` | 同上，debug | 故障定位 |
| `direct-uefi/snponly.efi` | UEFI 直接引导 | 本地介质引导 |

选择原则：

| 场景 | 选择 |
| --- | --- |
| 主板支持 UEFI Network Boot | `pxe-uefi/snponly.efi`，作为默认首跳 |
| 主板无 PXE 入口，需 USB/ESP 引导 | `direct-uefi/ipxe.efi` |
| 网卡初始化失败 | 对应的 `-debug` 产物 |

主仓库默认使用 `tftp/snponly.efi`。不同机型可能需要不同的首跳产物：控制面按设备的 `boot_firmware` 在 DHCP 里选中 `snponly.efi`（默认）或全驱动 `tftp/ipxe.efi`，同一 PXE 网段可按设备混用。全驱动首跳使用现成的 `direct-uefi/ipxe.efi`：它内置 `auto.ipxe`（DHCP 后跳 `tftp://${next-server}/boot.ipxe`），作 TFTP 首跳与作本地介质引导是同一条脚本。

`tftp/*.efi` 不入库，部署时手动放置：`pxe-uefi/snponly.efi` → `tftp/snponly.efi`，`direct-uefi/ipxe.efi` → `tftp/ipxe.efi`；两份都在 dnsmasq 的文件级挂载里，缺一不可。

上表是 `build.sh` 的形态；部署时首跳换成 `build-sb.sh` 的签名产物，形态与 `pxe-uefi/snponly.efi` 相同：`dist/secureboot/snponly-sb.efi` 覆盖 `tftp/snponly.efi`，`KURRENT_SB_TARGET=ipxe` 构建的 `dist/secureboot/ipxe-sb.efi` 覆盖 `tftp/ipxe.efi`。

```bash
cd dist
sha256sum -c SHA256SUMS
```

签名产物的哈希以 `dist/secureboot/manifest.json` 的 `artifact_sha256` 为准，`/boot-assets` 会与镜像实际哈希核对。

发布页资产使用扁平命名（如 `pxe-uefi-snponly.efi`），校验文件内保留 `pxe-uefi/` 目录结构；下载后按目录放置再校验，或只比对目标文件摘要。

## 接下来

- 返回[固件概述](./index.md)
- 部署产物与首次启动：[部署与预置](./deploy.md)
- 签名与信任库：[Secure Boot 预置](./secure-boot.md)
- 补丁集与网卡矩阵：[固件平台参考](../reference/firmware-platform.md)
