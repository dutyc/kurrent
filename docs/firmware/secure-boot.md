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
2. 让 TFTP 首跳交付签名镜像：`install -m 0644 tftp/secureboot/snponly-sb.efi tftp/snponly.efi`，然后 `docker compose restart kurrent-dnsmasq` 使文件级挂载指向新 inode。
3. 查询 `/api/cp/boot-assets`，`secureboot.status` 应为 `ok`：镜像哈希与 manifest 一致、端口与部署一致、boot CA 存在且指纹一致。WebUI「引导链」在产物存在时显示 Secure Boot 行。

## 预置目标机信任库

签名证书必须先进入目标机 `db`，否则固件会拒绝执行（一次性预置，不是每次启动的依赖）：

- 生成离线签名密钥：`tools/secureboot/keygen-offline.sh ~/offline/kurrent-sb`。私钥保持离线，脚本拒绝写入仓库或容器。
- **USB/BIOS（受支持路径，无需 BMC）**：`tools/secureboot/export-usb.sh <usb> <artifact-signing.crt> <snponly-sb.efi> <boot-ca.crt>` 生成导入包（只含公开证书），在目标机 Key Management 中导入 `db-cert.cer`。
- **Redfish（可选，需 BMC 与厂商支持）**：`KURRENT_BMC_USER=... KURRENT_BMC_PASSWORD=... tools/secureboot/enroll-redfish.sh --bmc <bmc> --cert <artifact-signing.crt> --enable-secureboot`。脚本先读 Secure Boot 与 `db` 状态、确认已有 Microsoft UEFI CA，再幂等导入证书并读回指纹；`--dry-run` 只读。该工具仅完成协议级验证（模拟 BMC），厂商实现须逐型号确认。
- 目标机：关闭 CSM、开启 Secure Boot，用 `tools/secureboot/check-host.sh <artifact-signing.crt>` 复核状态与 `db`。

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
