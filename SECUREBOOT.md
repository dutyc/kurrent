# Secure Boot 引导加固说明

该分支提供一个可选的 Secure Boot 引导加固功能。启用后，平台固件仅执行经数字签名的第一段引导代码，引导过程中的 HTTPS 连接仅信任预先指定的 CA。未启用时，现有部署的证书、端口与引导流程不受影响。

本说明对应的分支：

- 控制面仓库（`kurrent`）：`feature/secure-boot`
- 固件仓库（`kurrent-firmware`）：`secureboot`

两个分支均基于各自的 `main`。

## 1. 背景与目标

现有网络引导链存在两个信任缺口：

1. **第一段代码缺少固件级校验**。地址由 DHCP 下发，首跳二进制与引导指针经 TFTP/HTTPS 获取。同一二层内的攻击者可伪造 DHCP 响应、替换首跳二进制或引导指针，使设备执行非预期代码。后续的签名与凭据校验仅能证明请求方持有设备私钥，无法证明实际执行的代码来源。
2. **引导服务器采用首次连接信任（TOFU）**。首次握手接受对端证书并固化，此后才进行校验；首次连接可能被中间人利用，攻击者提供的证书会被持久化为信任锚。

上述两个缺口可在不引入硬件信任根的前提下收窄：由固件验证第一段代码，并由固件仅信任指定的引导 CA。本分支实现这两项约束，其余链路与默认部署保持不变。

## 2. 功能范围

- **第一段代码签名**：`snponly-sb.efi` 由离线密钥签名，UEFI `db` 中的证书由固件验证；未签名、被篡改或证书不匹配的二进制将被拒绝。
- **静态引导逻辑**：菜单、配置与跳转脚本在构建时嵌入签名二进制，不可单独替换；修改脚本须重新构建并签名。
- **固定的引导服务器身份**：boot CA 在构建时嵌入固件，引导 HTTPS 仅接受该 CA 签发的证书，不再依赖首次连接信任。
- **独立的引导入口**：专用 4443 端口与 boot CA，仅暴露设备上报、挑战、`/boot-vars` 与引导资产；管理面与 443 的原有行为不受影响。
- **一次性预置工具**：离线密钥生成、签名产物发布、USB/BIOS 导入包导出、可选 Redfish 导入、目标机只读预检。
- **可观测性**：`/boot-assets` 返回签名产物、manifest、boot CA 与端口状态；WebUI 引导链在产物存在时显示 Secure Boot 检查行。

## 3. 安全能力与边界

### 3.1 可提供的保证

| 能力 | 依据 |
|---|---|
| 固件仅执行经签名的第一段代码 | `db` 证书验证 EFI 签名；未签名或篡改产物被固件拒绝 |
| 引导菜单与脚本不可单独替换 | 静态脚本编译进签名二进制，签名覆盖全部内容 |
| 设备仅连接指定的引导服务器 | boot CA 在构建时嵌入，非该 CA 签发的证书一律失败 |
| 失败关闭，不回退至首次连接信任 | boot CA 缺失或不匹配时链路终止 |
| 设备凭据仅发放给持有设备私钥的请求 | 沿用 `nonce‖mac` 验签；设备密钥在签名代码内生成 |
| 数据面传输保持加密与认证 | 沿用 `sanboot` 4421 NVMe/TCP TLS 1.3 与 DHHC-1/PSK |

概括而言，本功能可证明设备所执行的代码来源与所连接的服务器身份；不提供平台完整性证明、回滚防护与可用性保障。

### 3.2 不提供的能力

- **平台控制权失守**：具备修改 `db`、刷新固件或物理接触设备能力的主体可重新完成预置，从而绕过本功能。
- **TPM 度量与远程证明**：不证明平台启动过程未被篡改，也不向控制面出具硬件证据。
- **回滚防护**：无法阻止将设备恢复至旧版本固件或旧产物。
- **拒绝服务防护**：同一二层内仍可通过 DHCP/ARP 欺骗或链路中断阻止引导。
- **BMC 失守防护**：带外管理面被控制后，启动项、重装与密钥清除均可被操作。
- **在线吊销**：boot CA 与产物证书不提供在线 CRL；轮换等同于重刷固件并重新预置。
- **发行版内核链的替代**：`sanboot` 之后的 shim/GRUB/内核由发行版签名体系保证，不由本功能替代。
- **设备私钥的静态保护**：私钥保存于 NVRAM，未实施 TPM 封存；具备 NVRAM 读取能力的主体仍可冒用设备身份。

### 3.3 威胁对照

| 威胁 | 启用后的结果 |
|---|---|
| 替换首跳 EFI 或引导指针 | 拒绝：签名不匹配或签名证书不在 `db` |
| 篡改嵌入的菜单或配置 | 无法单独替换；修改内容须重新签名，原签名随之失效 |
| 冒充引导 HTTPS 服务器 | 拒绝：证书无法链至嵌入的 boot CA |
| 首次连接中间人 | 不再适用：不采用 TOFU，首次连接即校验 |
| 重放 `/boot-vars` 请求 | 须持有设备私钥对 `nonce‖mac` 签名，nonce 单次有效 |
| 同一二层阻断或伪造 DHCP | 仍可造成拒绝服务，但无法注入代码或冒充服务器 |
| 修改 `db`、刷新固件或物理接触 | 不在防护范围；平台控制权高于本功能 |
| 回滚至旧版本 | 不提供防护 |
| 读取 NVRAM 设备私钥 | 不提供防护；需 TPM 封存 |

### 3.4 验证状态

| 验证层次 | 状态 |
|---|---|
| 链路闭合（QEMU/OVMF + 真实控制面） | 已验证至系统登录提示 |
| 真机固件（BIOS + USB 导入 `db`） | 待验证；不依赖 BMC |
| Redfish 证书导入 | 仅协议级验证（模拟 BMC）；厂商实现需逐型号验证 |

真机验证不以 BMC 为前提：任意具备 UEFI Secure Boot 的机器均可通过 BIOS 或 USB 导入 `db` 完成。当前功能应表述为“实验室环境已验证”，在完成真机验证前不作为默认功能。

## 4. 开启后的启动过程

```
UEFI 固件 db：产物签名证书
  └─ 验证 snponly-sb.efi（sbsign；内嵌 auto.ipxe / sb-boot.ipxe.cfg / sb-menu.ipxe）
       ├─ HTTPS 仅信任 boot CA（TRUST + CERT，不回落 TOFU）
       │    ├─ /devices/report       设备公钥上报
       │    ├─ /devices/challenge + /sign + /boot-vars
       │    └─ /file/…               内核与 initrd
       └─ sanboot 4421 NVMe/TCP TLS 1.3（既有凭据链）
```

- **平台信任锚**：`db` 中的产物签名证书，决定固件是否执行该引导代码。
- **网络信任锚**：boot CA，决定引导 HTTPS 接受何种证书。
- 两个信任锚相互独立，任一缺失均导致失败关闭。

## 5. 适用与不适用

**适用**

- 具备 UEFI Secure Boot 的服务器或工作站。
- 可在部署前对平台实施**一次性**带外配置（BIOS 或 USB 导入 `db`；有 BMC 且厂商支持时也可使用 Redfish）。
- 要求第一段引导代码与引导服务器均经过身份校验，同时保留零接触的日常启动。

**不适用**

- 无法修改 `db` 且无带外管理通道的设备。此类设备仍使用现有的非 Secure Boot 引导方式。
- 需要 TPM 度量、远程证明、回滚防护或抗 BMC 失守的场景（本功能不覆盖）。
- 未配置发行版签名加载器的系统盘（见第 6 节）。

该功能不要求 USB 或 BMC 存在于启动路径；二者仅为一次性预置 `db` 的可选通道。

## 6. 平台前提

1. **固件**：UEFI 启动，CSM 关闭，Secure Boot 可开启，`db` 可导入自定义证书。
2. **Microsoft UEFI CA**：`db` 须包含签发系统盘 shim 的 CA（当前 Ubuntu 26.04 母盘使用 Microsoft UEFI CA 2011）。多数服务器默认具备；采用 2023 CA 签名的加载器时，`db` 须包含 Microsoft UEFI CA 2023。
3. **系统盘加载器**：`sanboot` 之后的 shim/GRUB/内核由发行版签名体系保证。母盘 ESP 应为发行版签名链：
   - `EFI/BOOT/BOOTX64.EFI`：Microsoft UEFI CA 2011 签名的 shim
   - `EFI/BOOT/grubx64.efi`：Canonical 签名的 GRUB
   - `EFI/BOOT/mmx64.efi`：MOK Manager
   - 未签名的 GRUB 将在 `sanboot` 之后被固件拒绝。
4. **控制面**：boot CA 与 4443 叶证书由控制面生成；宿主防火墙须放行 `KURRENT_BOOT_HTTPS_PORT`（默认 4443）。443 的证书与原有引导流程不受影响。
5. **网络**：固件通过 DHCP `next-server` 或构建时固定的 `CONTROLLER_IP` 定位控制面；引导 HTTPS 证书的 SAN 须覆盖该地址。

## 7. 组件构成

- 控制面：独立 boot CA（10 年）与 4443 叶证书（1 年）、仅暴露引导端点的 4443 入口、`/boot-assets` 的 Secure Boot 状态、引导链页面条件行。
- 固件：`build-sb.sh`（嵌入静态脚本、固定 boot CA、关闭 TOFU、签名并生成 manifest）、`snponly-sb.efi`、签名校验工具。
- 预置与运维：离线密钥生成、USB 导出、目标机只读预检、Redfish 幂等导入工具。
- 部署文档与 API 字段说明。

## 8. 启用步骤

### 8.1 生成产物签名密钥（离线）

```bash
tools/secureboot/keygen-offline.sh ~/offline/kurrent-sb
```

私钥须离线保管；脚本拒绝写入仓库或容器。

### 8.2 构建签名固件

在固件仓执行：

```bash
SB_KEY=~/offline/kurrent-sb/artifact-signing.key \
SB_CERT=~/offline/kurrent-sb/artifact-signing.crt \
BOOT_CA_CERT=<控制面>/control_plane/state/pki/ca/boot/ca.crt \
BOOT_HTTPS_PORT=4443 \
CONTROLLER_IP=<控制器 IP> \
build/build-sb.sh
```

产物为 `dist/secureboot/snponly-sb.efi` 与 `manifest.json`；`tools/verify-sb.sh` 可复核签名。

### 8.3 发布至控制面

将产物与 manifest 放入宿主 `tftp/secureboot/`。`GET /boot-assets` 的 `secureboot` 段应为 `ok`，WebUI“引导链”将显示 Secure Boot 行。

### 8.4 预置目标机 `db`（二选一）

**USB / BIOS（受支持路径，无需 BMC）**

```bash
tools/secureboot/export-usb.sh <usb-root> \
  ~/offline/kurrent-sb/artifact-signing.crt \
  dist/secureboot/snponly-sb.efi \
  <控制面>/control_plane/state/pki/ca/boot/ca.crt
```

按包内说明在 Key Management 导入 `db-cert.cer`。

**Redfish（可选，需 BMC 与厂商支持）**

```bash
KURRENT_BMC_USER=... KURRENT_BMC_PASSWORD=... \
tools/secureboot/enroll-redfish.sh \
  --bmc <bmc> \
  --cert ~/offline/kurrent-sb/artifact-signing.crt \
  --enable-secureboot
```

脚本先检查 `db` 是否已有 Microsoft UEFI CA，随后幂等导入证书并读回校验，可选开启 Secure Boot。凭据亦可通过 `--credential-file` 指向 0600 文件传入；`--dry-run` 仅执行读取。

该工具仅完成协议级验证（模拟 BMC），厂商实现须逐型号确认证书导入与 Setup Mode 行为；首次使用建议先以 `--dry-run` 读取目标机状态，不支持时改用 USB/BIOS。

### 8.5 启动与复核

开启 Secure Boot，从签名镜像（本地介质、HTTP Boot 或 PXE）启动，并按以下方式复核：

- `tools/secureboot/check-host.sh <artifact-signing.crt>`：Secure Boot 状态、Setup Mode、`db` 内容。
- `GET /boot-assets`：`secureboot.status = ok`、`manifest_matches = true`、`port_matches = true`、`boot_ca_matches = true`。
- 串口或控制台应出现固定 CA 的 4443 请求与 `Registered SAN device`，内核输出 `Kernel is locked down from EFI Secure Boot mode`，最终进入登录提示。

## 9. 日常运维

- **叶证书**：4443 叶证书有效期 1 年，控制面每次启动检查剩余有效期，不足 30 天即重签；续签不更换 boot CA，固件无需重刷。
- **boot CA**：有效期 10 年，仅能通过重签固件更换；更换等同于一次固件重刷并重新预置 `db`。
- **固件升级**：部分平台固件升级会清空 `db`，须重新执行第 8.4 节。
- **产物更新**：修改嵌入脚本或菜单须重新构建并重新签名；`/boot-assets` 会校验产物与 manifest 是否一致。
- **停用**：删除 `tftp/secureboot/` 产物，并使目标机不再从签名镜像启动；引导链页面的 Secure Boot 行将自动隐藏。
- **撤销信任**：通过 BMC 厂商工具或 BIOS 从 `db` 移除产物签名证书；不支持单项移除时，清键后重刷固件。
- **私钥丢失**：使用新密钥重签固件，并在目标机导入新的 `db` 证书；旧证书可保留或移除。

## 10. 不提供的保障

- 不实施 TPM 度量/封存、远程证明与 anti-rollback。
- 不覆盖 BMC 失守、拒绝服务与同二层攻击。
- 不提供 boot CA 的在线 CRL 与在线吊销。
- 不承诺 Redfish 厂商实现的一致性；该路径仅完成协议级验证，须逐型号确认。
- 不替代发行版的内核/initramfs 签名链；本功能仅保证第一段代码与引导服务器。
- 部分 PXE 固件不校验下载的 NBP；正式部署优先采用本地介质或固件原生 HTTP Boot。

## 11. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 固件报 `Access Denied -- rejected probably by Secure Boot` | 产物未签名、签名证书不在 `db`，或二进制被篡改 | 使用 `sbverify --cert` 复核产物；确认 `db` 含对应证书 |
| iPXE 访问 4443 报 `Permission denied` | boot CA 不匹配、服务器证书链不完整，或端口不一致 | 确认固件构建使用的 `BOOT_CA_CERT` 与控制面 `state/pki/ca/boot/ca.crt` 一致；检查 4443 证书 SAN |
| 引导链页面未显示 Secure Boot 行 | `tftp/secureboot/` 无产物 | 放入 `snponly-sb.efi` 与 `manifest.json` |
| `sanboot` 后停滞或返回固件 | 系统盘加载器未签名 | 将母盘 ESP 换为发行版签名 shim/GRUB，并确认 `db` 含对应 Microsoft CA |
| Redfish 导入失败 | 厂商不支持、需进入 Setup Mode，或权限不足 | 依据退出码与错误信息处理；改用 USB/BIOS 路径 |
| 修改配置后仍启动旧菜单 | 静态脚本已嵌入签名二进制 | 重新构建、签名并发布新产物 |
