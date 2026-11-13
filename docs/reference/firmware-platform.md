# 固件平台参考

本页是固件平台侧的参考：补丁集、设备信息字段、网卡支持矩阵、Target 配置与服务端错误。固件与主仓库之间的接口（命令、NV、TOFU、挑战-证明与启动变量）见[固件契约](./firmware-contract.md)；构建、产物选择与部署见[固件构建与部署](../firmware/build-deploy.md)。

## 补丁集

补丁按文件名顺序应用，任一补丁无法应用到固定基线即构建失败。当前补丁链为 10 个：

| 编号 | 名称 | 内容 |
| --- | --- | --- |
| 0001 | `realtek-adaptation` | RTL8125 全系与 RTL8126 原生驱动适配 |
| 0002 | `makefile-ipxe-debug` | debug 目标继承完整网卡驱动集 |
| 0003 | `snponly-local-boot` | 本地介质引导时回退接管 SNP/NII/MNP 设备 |
| 0004 | `device-info-collection` | SMBIOS 内存字段与 PCI 网卡芯片名 |
| 0005 | `nvmeof-adaptation` | NVMe/TCP SAN 驱动、DH-HMAC-CHAP、认证时序修复与 `hostnqn` |
| 0006 | `efi-nvs-backend` | EFI 变量 NVS 后端，含空块删除语义 |
| 0007 | `tofu-fingerprint` | TOFU 指纹固定与校验 |
| 0008 | `devicekey-commands` | ECDSA P-256 设备身份命令 |
| 0009 | `nvmetcp-nbct-acpi-table` | NBCT ACPI 表与内核凭据交接 |
| 0010 | `tls13-psk` | TLS 1.3 PSK 客户端与记录层 |

除补丁外，`embed/auto.ipxe` 是构建级 EMBED 资产，经 `EMBED=` 编译进 `direct-uefi` 产物，不参与补丁序列。补丁的许可边界不完全一致：RTL8125/8126 适配部分仅按 GPL-2.0 授权，不得以 UBDL 再分发；其余自研补丁按其文件头声明授权。再分发前应阅读 `patches/README`。

## 设备信息字段

固件采集四组设置，经 HTTP GET 上报：

| 组 | 设置 | 来源 |
| --- | --- | --- |
| 身份 | `uuid`、`manufacturer`、`product`、`serial`、`board-serial`、`asset` | SMBIOS type 1/2/3 |
| CPU | `cpuvendor`、`cpumodel` | CPUID |
| 内存 | `mem-total`、`mem-type`、`mem-speed` | SMBIOS type 17（补丁 0004） |
| 网卡 | `netX/mac`、`netX/chip`、`netX/busid`、`netX/bustype`、`netX/linktype` | netdev 设置；`chip` 由补丁 0004 启用 |

数值设置按 iPXE 约定以十六进制展开，服务端需同时兼容十六进制与十进制解析。字符串可能包含空格与特殊字符，上报前应确认 URL 编码行为。无 SMBIOS type 17 的环境取不到内存字段；SNP 引导时 `${netX/chip}` 显示 `snpnet`，只有原生驱动才显示芯片型号。字段级契约见[固件契约](./firmware-contract.md#设备上报)。

## 网卡支持

适配重点是 2.5G/5G 及以上速率网卡；千兆与百兆网卡维持上游基线既有支持，不再新增适配。以下依据上游基线加定制补丁的驱动设备表与实测记录整理，上游升级后范围可能变化。

### 原生驱动

- **Intel e1000e 全系（82540 至 I219 最新步进）**：`intel.c`；物理机实测通过（`direct-uefi`）。
- **Intel I210 / I211 / I350 / 82576**：`intel.c`；含服务器常见 I350。
- **Intel I225 / I226（2.5G）**：`intel.c`；全步进覆盖。
- **Intel 82599 / X540 / X550 / X553 / X552（10G）**：`intelx.c`；含 X553-AT。
- **Intel X710 / XL710（10G / 40G）**：`intelxl.c`；服务器常见。
- **Intel E810 / E823（25G / 100G）**：`ice.c`；新驱动。
- **Realtek RTL8136 / 8139 / 8129 / 8167 / 8168 / 8169**：`realtek.c`；上游基线。
- **Realtek RTL8125 全系（2.5G）**：`realtek.c`（0001）；物理机 DHCP 实测通过。
- **Realtek RTL8126（5G）**：`realtek.c`（0001）；编译验证通过；待硬件实测。
- **Marvell/Aquantia AQC100-114（10G / 5G / 2.5G）**：`aqc1xx.c`；设备表全系覆盖。
- **Broadcom BCM57xx 千兆**：`tg3`；覆盖广。
- **Broadcom BCM5706/5708 等**：`bnx2`；1G/10G 旧系。
- **Broadcom NetXtreme-E（BCM957xxx）**：`bnxt`；服务器新系。
- **virtio / vmxnet3 / ENA / GVE / netvsc**：虚拟化场景。
- **AX88179、LAN7800、SMSC95xx/75xx、DM96xx、CDC ECM/NCM**：USB 网卡仅此几类。

### 协议层

服务器网卡自带 UEFI SNP 栈时，可不在固件内移植驱动，直接使用 `snponly.efi`。该路径只覆盖 UEFI；传统 BIOS/UNDI 产物不在当前构建清单内。

| 网卡 | 路径 | 备注 |
| --- | --- | --- |
| Mellanox ConnectX-4 / 5 / 6 / 7（25G-100G） | `pxe-uefi/snponly.efi` 或 `direct-uefi/snponly.efi` | 零适配；受 SNP 的 iSCSI 挂起缺陷影响 |
| Broadcom bnx2x 系（BCM57710/57711/57712/57800/57810/57840） | 同上 | 卡自带官方引导 ROM；受 SNP 的 iSCSI 挂起缺陷影响 |

### 不支持

| 网卡 | 场景 | 备注 |
| --- | --- | --- |
| Realtek USB RTL8152 / 8153 / 8156 | USB-C 转 RJ45 | iPXE 无此驱动 |
| Intel AX、Realtek 88 系列等现代 WiFi | 笔记本无线 | 仅有旧式 prism2/rtl818x |

### 已知问题

- **Realtek RTL8168 部分版本**：原生驱动初始化挂起，需走 `snponly` 兜底。
- **SNP 的 iSCSI 挂起缺陷**：影响所有只能走固件 SNP 驱动的网卡；`snponly` 路径同样有此风险。
- **Intel I219**：带 `INTEL_PBSIZE_RST` / `NO_PHY_RST` 特殊处理，属于历史缺陷修复。
- **Marvell/Aquantia AQC**：官方轻量实现，链路依赖硬件自协商；AQC113 动态固件加载未实现，固件空白时返回 `-ENOTSUP`。
- **82599 / X550（SFP+）**：兼容性依赖光模块质量。

### 实测记录

| 日期 | 网卡 | 固件 | 结果 |
| --- | --- | --- | --- |
| 2026-08-07 | Realtek RTL8125 | SNP 固件 | iSCSI 挂载挂起 |
| 2026-08-09 | Realtek RTL8168 | 原生驱动 | 部分版本初始化挂起，SNP 兜底 |
| 2026-08-10 | Intel e1000（82540EM 等） | `direct-uefi` | 通过 |
| 2026-08-11 | Realtek RTL8125 | 定制固件（0001） | 正常 |

选择顺序：优先使用原生驱动覆盖的网卡；无原生驱动但自带 UEFI 引导 ROM 的服务器卡使用 `snponly` 产物，并评估 SNP 的 iSCSI 挂起风险；RTL8126 在生产部署前应在目标主板验证；USB 网卡与 WiFi 不作为无盘引导路径。

## Target 配置

以下 NQN 与地址用于本地验证，示例使用生产格式 `nqn.2026-07.com.kurrent:worker-01.ubuntu.0123456789ab`；生产盘 NQN 由控制面按节点 `spec.agent.nqnBase` 生成，默认前缀为 `nqn.2026-07.com.kurrent`，`sanboot` URI 中的 NQN 必须与 target 实际子系统一致。

无认证：

```bash
NQN="nqn.2026-07.com.kurrent:worker-01.ubuntu.0123456789ab"
IMG=/path/to/backing.img
SYS=/sys/kernel/config/nvmet/subsystems/$NQN
PORT=/sys/kernel/config/nvmet/ports/1

mkdir -p "$SYS"
echo 1 > "$SYS/attr_allow_any_host"
mkdir -p "$SYS/namespaces/1"
echo -n "$IMG" > "$SYS/namespaces/1/device_path"
echo 1 > "$SYS/namespaces/1/enable"

mkdir -p "$PORT"
echo ipv4 > "$PORT/addr_adrfam"
echo tcp  > "$PORT/addr_trtype"
echo 127.0.0.1 > "$PORT/addr_traddr"
echo 4420 > "$PORT/addr_trsvcid"
ln -sf "$SYS" "$PORT/subsystems/$NQN"
```

backing 文件应放在有足够权限与空间的目录；部分环境对 `/tmp` 有额外限制。

DH-HMAC-CHAP 密钥格式为 `DHHC-1:XX:<base64(key + CRC32)>`：

```bash
python3 -c 'import zlib,base64; k=b"0123456789abcdef0123456789abcdef"; \
print("DHHC-1:01:"+base64.b64encode(k+zlib.crc32(k).to_bytes(4,"little")).decode())'
```

`XX` 必须为两位数字；`01` 表示 SHA-256。`CRC32` 必须是标准终值（含 final XOR），小端追加在密钥之后；前缀长度或 CRC 不正确时，nvmet 会以 `base64 key decoding error -1` 或 `dhchap status 2` 拒绝。

内核 7.0+ 的 configfs 结构：

```bash
HOSTNQN="nqn.2014-08.org.ipxe:ipxe"
HOST_DIR=/sys/kernel/config/nvmet/hosts/$HOSTNQN

echo 0 > "$SYS/attr_allow_any_host"
mkdir -p "$HOST_DIR"
echo -n "$KEY" > "$HOST_DIR/dhchap_key"
echo ffdhe4096 > "$HOST_DIR/dhchap_dhgroup"
ln -sf "$HOST_DIR" "$SYS/allowed_hosts/$HOSTNQN"
```

`allowed_hosts/` 使用符号链接，不能以目录方式创建；`attr_allow_any_host=1` 会使内核跳过认证。旧版内核的认证接口不同，应以实际内核文档为准。

### 服务端错误速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `base64 key decoding error -1` | `DHHC-1:XX:` 前缀不足 10 字节 | 保持两位数字 |
| `dhchap status 2` | 密钥 CRC32 不是终值 | 用 `zlib.crc32` 重新生成 |
| 免认证直连 | `attr_allow_any_host=1` | 置 0 后重建子系统 |
| `Can't set allow_any_host when explicit hosts are set!` | 残留 `allowed_hosts/` 链接 | 清理旧链接后重试 |

### Host NQN

固件的 Host NQN 默认来自 SMBIOS UUID；QEMU/OVMF 不提供 UUID 时回退为固定 `nqn.2014-08.org.ipxe:ipxe`。控制面按 Worker 下发 `hostnqn`，并通过 `hostnqn` 设置项覆盖默认值。target 启用 `allow_any_host=0` 时，`hosts/` 目录名必须与设备实际发送的值一致。

## sanboot 与数据面 TLS

```text
sanboot nvme://<traddr>:<trsvcid>/<nqn>[?secret=<DHHC-1 key>]
```

无认证示例：

```text
sanboot nvme://10.0.2.2:4420/nqn.2026-07.com.kurrent:worker-01.ubuntu.0123456789ab
```

带认证示例：

```text
sanboot nvme://10.0.2.2:4420/nqn.2026-07.com.kurrent:worker-01.ubuntu.0123456789ab?secret=DHHC-1:01:...
```

成功时日志应出现 `sending Property Set (CC)`、`namespace 1: N blocks of 4096 bytes` 与 `I/O queue established`，随后控制权交给盘上的 GRUB。

密钥不固化在固件中。设备身份是 TPM 2.0 AK（私钥不可导出）：首次启动由 `akgen` 创建并持久化，`akpub` 导出 SPKI 与 TPM2B_NAME，随 `/devices/report` 入池；之后每次启动用 `quote` 对 PCR 0/2/4/7 做 SHA-256 证明，控制面在 `/boot-vars` 验证签名、nonce 绑定与 PCR 基线后才注入凭据：

```ipxe
dhcp || goto failed
chain --autofree https://<control-plane>/devices/challenge?mac=${net0/mac} || goto failed
isset ${nonce} || goto failed
quote ${nonce} || goto failed
chain --autofree https://<control-plane>/boot-vars?mac=${net0/mac}&hostname=${hostname}&nonce=${nonce}&quote=${quote}&quote_sig=${quote-sig} \
  || goto failed
sanboot nvme://<traddr>:<trsvcid>/<nqn>?secret=${nbft-secret}
:failed
shell
```

启用数据面 TLS 时，控制面同时下发 `nbft-tls-psk` 与 `nbft-tls-port`，脚本在 URI 追加 `&tls_psk=${nbft-tls-psk}&tls_trsvcid=${nbft-tls-port}`。端口或密钥缺失时，引导脚本应进入失败分支，不回落明文端口。`/boot-vars` 不要求登录会话，凭据只在一次性 nonce 与设备 quote 证明（AK 签名有效、`extraData` 等于 nonce、PCR 与基线一致）通过后注入；未证明的请求只能取得非凭据变量。

当 `sanboot` URI 的端口等于 `tls_trsvcid` 时，固件对该连接执行 TLS 1.3 PSK 握手：

- PSK identity 为 `NVMe0R01 <hostnqn> <subsysnqn>`。
- 握手与记录层由 0010 补丁实现，使用 `TLS_AES_128_GCM_SHA256` 与 `psk_dhe_ke`。
- 同一组参数随 NBCT 交给 initramfs；内核侧在 `.nvme` keyring 注入 PSK，并以 `--tls` 重连数据端口。

默认路径为固件段与内核段双段加密；HTTPS 引导资产仍走 iPXE 原生 TLS（上限 1.2），与数据面 TLS 1.3 相互独立。

## 相关页面

- [固件概述](../firmware/index.md)
- [固件契约](./firmware-contract.md)
- [固件构建与部署](../firmware/build-deploy.md)
- [发行版矩阵](./distributions.md)
- [引导链](../concepts/boot-chain.md)
- [安全模型](../architecture/security.md)
- 固件仓库源码、补丁与研究记录：<https://github.com/dutyc/kurrent-firmware>
