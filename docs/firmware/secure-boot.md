# Secure Boot 引导

本页说明如何让首跳固件与引导 HTTPS 具备可验证的信任锚，并与 TPM 证明配合。设备入池要求上报 `sb=1`，所以 Secure Boot 是本平台准入的前提，而不是可选项。

## 信任对象

引导链有三个互不替代的信任锚：

| 对象 | 锚 | 失效后果 |
| --- | --- | --- |
| 首跳二进制（iPXE） | 目标机 UEFI `db` 中的站点签名证书 | 未签名或签名不匹配的固件不会执行 |
| 引导 HTTPS（4443） | 签名固件内嵌的 boot CA（`TRUST=` / `CERT=`） | 不是该 CA 签发的服务器证书一律拒绝，不回落 TOFU |
| 设备身份 | TPM AK 与 PCR 0/2/4/7 quote | 无 AK 或 quote 不通过时不下发凭据 |

换固件签名密钥需要更新目标机 `db`；换 boot CA 等于固件重刷（指纹烘在镜像里）；改固件或 Secure Boot 策略会改变 PCR，证明按 `pcr_mismatch` 拒绝，核对后批准新基线。

## 控制面产物

控制面启动时幂等生成，与组件三分根分离：

- `state/pki/ca/boot/ca.crt|ca.key`：boot CA，10 年，只签引导端点证书。
- `state/certs/boot-server.crt|boot-server.key`：4443 叶证书，1 年，SAN 复用 `spec.serverCert.san`；剩余有效期不足 30 天时自动重签，不换 boot CA。
- nginx 的 4443 server 只反代 `/boot-vars`、`/devices/report`、`/devices/challenge`，不要求客户端证书，未知路径直接 404；管理面、WebUI 与 `/api/cp/` 不在该端口。

宿主端口用 `KURRENT_BOOT_HTTPS_PORT` 调整，必须与固件构建时的 `BOOT_HTTPS_PORT` 一致：端口烘在签名镜像里，`/api/cp/boot-assets` 的 `secureboot.port_matches` 校验这一点。

## 构建签名固件

签名构建由固件仓的 `build/build-sb.sh` 完成：

```bash
cd kurrent-firmware
SB_KEY=/path/to/artifact-signing.key \
SB_CERT=/path/to/artifact-signing.crt \
BOOT_CA_CERT=/path/to/boot-ca.crt \
BOOT_HTTPS_PORT=4443 \
./build/build-sb.sh
```

需要的输入：

- `SB_KEY` / `SB_CERT`：离线代码签名密钥与证书，证书须导入目标机 `db`。
- `BOOT_CA_CERT`：控制面的 boot CA 证书（`state/pki/ca/boot/ca.crt`），构建时作为 `TRUST=` / `CERT=` 固定进固件。
- `CONTROLLER_IP`：控制面地址固定时填写；留空则使用 DHCP `next-server`。

脚本把 `auto.ipxe`、`sb-boot.ipxe.cfg`（含端口与控制面地址）与 `sb-menu.ipxe` 一并嵌入，再由 `sbsign` 签名。产物在 `dist/secureboot/`：`snponly-sb.efi` 与 `manifest.json`；manifest 记录产物哈希、签名者、构建时间、端口与 boot CA 指纹。

脚本是静态引导逻辑的唯一入口：改菜单、变量或跳转都必须重新构建并签名，不能单独替换。

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

`sanboot` 之后的加载器由发行版签名链决定：金盘 ESP 的 shim/GRUB/内核必须是发行版签名版本（Ubuntu 26.04 金盘为 Microsoft UEFI CA 2011 签名的 shim → Canonical 签名的 GRUB 与内核），目标机 `db` 需包含对应的 Microsoft CA。未签名的 GRUB 会在 `sanboot` 之后被拒绝。

## 验证

- 设备上报 `sb=1` 并携带有效 TPM AK，才能进入设备池。
- `/api/cp/boot-assets` 的 `secureboot` 段为 `ok`。
- 首次冷启动到 iPXE 菜单、完成证明并进入 `login:`；负向用例见[故障排查](../guide/troubleshooting.md)。

## 边界

- Secure Boot 只覆盖第一段 iPXE 与引导 HTTPS；`sanboot` 之后的加载器由发行版签名链决定。
- 不做防回滚：旧版本签名固件仍可通过校验，回滚控制依赖发布流程。
- boot CA 轮换等于固件重刷；固件升级或信任库变化会改变 PCR，需要管理员批准新基线。
- 部分 PXE 固件不校验下载的 NBP；条件允许时优先本地介质或受控 HTTP 引导。真实硬件逐型号验收尚未完成。

## 相关页面

- [固件构建与部署](./build-deploy.md)
- [设备身份与证明](../concepts/identity.md)
- [引导链](../concepts/boot-chain.md)
- [安全模型](../architecture/security.md)
