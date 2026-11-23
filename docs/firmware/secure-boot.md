# Secure Boot 预置

设备入池要求上报 `sb=1`，因此 Secure Boot 是本平台准入的前提。签名产物由 `build-sb.sh` 构建，随后交付到控制面，并在目标机信任库中预置签名证书。信任锚与轮换影响见[引导信任](./trust.md)。

## 控制面产物

控制面启动时幂等生成，与组件三分根分离：

- `state/pki/ca/boot/ca.crt|ca.key`：boot CA，10 年，只签引导端点证书。
- `state/certs/boot-server.crt|boot-server.key`：4443 叶证书，1 年，SAN 复用 `spec.serverCert.san`；剩余有效期不足 30 天时自动重签，不换 boot CA。
- nginx 的 4443 server 只反代 `/boot-vars`、`/devices/report`、`/devices/challenge`，不要求客户端证书，未知路径直接 404；管理面、WebUI 与 `/api/cp/` 不在该端口。

宿主端口用 `KURRENT_BOOT_HTTPS_PORT` 调整，必须与固件构建时的 `BOOT_HTTPS_PORT` 一致：端口内嵌在签名镜像中，`/api/cp/boot-assets` 的 `secureboot.port_matches` 校验这一点。

## 构建签名固件

签名构建由固件仓的 `build/build-sb.sh` 完成：

```bash
cd kurrent-firmware
SB_KEY=/path/to/artifact-signing.key \
SB_CERT=/path/to/artifact-signing.crt \
BOOT_CA_CERT=/path/to/boot-ca.crt \
./build/build-sb.sh
```

签名构建必须提供签名密钥、签名证书与 boot CA 证书。端口与控制面地址可以省略：端口省略时取 `4443`，地址省略时由设备从 DHCP `next-server` 取得；显式设置 `CONTROLLER_IP` 才会把地址固定进镜像。换 boot CA、端口或显式地址都需重签。

脚本把 `auto.ipxe`、`sb-boot.ipxe.cfg` 与 `sb-menu.ipxe` 一并嵌入，再由 `sbsign` 签名。产物在 `dist/secureboot/`：`snponly-sb.efi` 与 `manifest.json`。manifest 记录产物哈希、签名者、构建时间、端口与 boot CA 指纹。

同一部署（同一 boot CA、同一端口）内可以批量预置同一份镜像。脚本是静态引导逻辑的唯一入口：改菜单、变量或跳转都必须重新构建并签名，不能单独替换。

## 部署到控制面

1. 把 `dist/secureboot/snponly-sb.efi` 与 `manifest.json` 放到控制面宿主的仓库 `tftp/secureboot/`。该目录是产物核对与导出源，不入库。
2. 让 TFTP 首跳交付签名镜像：`install -m 0644 tftp/secureboot/snponly-sb.efi tftp/snponly.efi`，然后 `docker compose restart kurrent-dnsmasq` 使文件级挂载指向新 inode。全驱动首跳同理：固件仓以 `KURRENT_SB_TARGET=ipxe` 生成 `ipxe-sb.efi`，覆盖 `tftp/ipxe.efi` 后让 `boot_firmware=full` 的设备引导。
3. 查询 `/api/cp/boot-assets`，`secureboot.status` 应为 `ok`：镜像哈希与 manifest 一致、端口与部署一致、boot CA 存在且指纹一致。WebUI「引导链」在产物存在时显示 Secure Boot 行。

## 预置目标机信任库

签名证书必须先进入目标机 `db`，否则固件拒绝执行。批量机队的默认路径是网络注册：机器处于 Setup Mode（或 Secure Boot 关闭）时网络启动同一份签名镜像，镜像内嵌的 `sbenroll` 命令写入平台密钥（PK / KEK / db）并重启，重启后固件验签进入生产引导。

1. 控制面开启平台注册窗口（`POST /settings/platform-enrollment`，WebUI 设备页同入口）。
2. 目标机网络启动签名镜像：写入密钥后自动重启，设备页「平台注册」清单出现该 MAC。
3. 注册完成后关闭窗口：新机器不再执行注册流程，已注册设备与清单保留。

平台密钥与签名描述符在构建签名镜像时提供（固件仓 `build-sb.sh` 的 `SB_PK_*` / `SB_KEK_*` / `SB_MS_UEFI_CA_CERT`），私钥离线保管，镜像内只嵌公开证书与签名描述符。写入顺序为 PK → KEK → db，db 含产物签名证书与 Microsoft UEFI CA 2011。注册须在受控网段执行：Setup Mode 下同网段任意主机都能写入密钥；窗口关闭后 PK 固定，后续修改须用离线私钥签名。

出厂已锁厂商密钥并开启 Secure Boot 的机型无法经网络注册，须经其厂商的管理流程处理。目标机状态可用 `tools/secureboot/check-host.sh` 复核。

`sanboot` 之后的加载器由发行版签名链决定：母盘 ESP 的 shim/GRUB/内核必须是发行版签名版本（Ubuntu 26.04 母盘为 Microsoft UEFI CA 2011 签名的 shim → Canonical 签名的 GRUB 与内核），目标机 `db` 需包含对应的 Microsoft CA。未签名的 GRUB 会在 `sanboot` 之后被拒绝。

## 验证

- 设备上报 `sb=1` 并携带有效 TPM AK，才能进入设备池。
- `/api/cp/boot-assets` 的 `secureboot` 段为 `ok`。
- 首次冷启动到 iPXE 菜单、完成证明并进入 `login:`；负向用例见[故障排查](../tasks/troubleshooting.md)。

## 接下来

- [固件概述](./index.md)、[固件组成](./composition.md)
- [引导信任](./trust.md)
- [构建](./build.md)、[部署与预置](./deploy.md)
- [设备身份与证明](../concepts/identity.md)
- [引导链](../concepts/boot-chain.md)
- [安全模型](../concepts/architecture/security.md)
