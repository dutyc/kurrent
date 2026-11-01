# 固件概述

Kurrent 的设备固件由独立仓库 [kurrent-firmware](https://github.com/dutyc/kurrent-firmware) 构建。仓库不保存 iPXE 源码，只在上游固定基线 `e6e51ccb` 之上维护差异补丁与构建资产。

固件负责设备侧的引导接入：NVMe/TCP SAN 引导、DH-HMAC-CHAP 认证、NBCT 凭据交接、设备身份密钥与 TOFU。注册窗口、Worker 绑定、系统盘选择与凭据签发属于控制面。

```text
固件仓库 dist/ 产物
        │
        ▼ 部署
主仓库 tftp/snponly.efi
        │
        ▼ 设备网络引导
控制面 /boot-vars + 数据面
```

## 职责边界

| 仓库 | 负责 | 交付物 |
| --- | --- | --- |
| kurrent-firmware | 协议驱动、认证、设备信息采集、网卡适配、固件构建 | `dist/` 产物、补丁集、测试工具 |
| kurrent（主仓库） | 控制面、引导脚本、注册窗口、身份与凭据投影、系统盘 | 控制面服务、`tftp/` 脚本、API |

固件不感知业务策略：注册窗口、Worker 绑定、盘选择与凭据签发都在主仓库。固件只提供命令与协议能力，引导脚本决定调用顺序。

## 能力

- **NVMe/TCP SAN 引导**：补丁 0005；原生 `nvmetcp` 驱动，覆盖 ICReq 协商、Connect、Property Set、Identify、I/O 队列与 R2T 读写。
- **DH-HMAC-CHAP 认证**：补丁 0005；单向认证；哈希 SHA-256/384/512，DH 组 0/2048/3072/4096。
- **NBCT 凭据表**：补丁 0009；把会话密钥、Host NQN 与 TLS 交接参数交给内核侧，避免二次网络取凭据。
- **TLS 1.3 PSK 数据面**：补丁 0010；固件段 `sanboot` 按 PSK 建立 TLS 1.3，与内核段共同形成全程加密。
- **Host NQN 设置**：补丁 0005；按 MAC 注入 `hostnqn`，支持 nvmet `allow_any_host=0` 严格模式。
- **设备信息采集**：补丁 0004；SMBIOS 身份与内存、CPUID、网卡字段，经 HTTP 上报控制面。
- **EFI NVS 持久化**：补丁 0006；`device-key` 与 `server-fingerprint` 写入 NVRAM，跨重启保留。
- **TOFU 信任**：补丁 0007；首次 HTTPS 握手固定服务器叶证书 SHA-256 指纹，此后不匹配即拒绝。
- **设备身份命令**：补丁 0008；`keygen` / `pubkey` / `sign` / `nvclear`。
- **网卡适配**：补丁 0001-0003；RTL8125/8126 原生驱动、`snponly` 本地引导回退。
- **本地介质引导**：`embed/auto.ipxe`；`direct-uefi` 产物内置脚本，启动即进入网络引导链。

## 补丁集

补丁按文件名顺序应用，任一补丁无法应用到固定基线即构建失败。当前补丁链为 10 个：

1. **0001 `realtek-adaptation`**：RTL8125 全系与 RTL8126 原生驱动适配
2. **0002 `makefile-ipxe-debug`**：debug 目标继承完整网卡驱动集
3. **0003 `snponly-local-boot`**：本地介质引导时回退接管 SNP/NII/MNP 设备
4. **0004 `device-info-collection`**：SMBIOS 内存字段与 PCI 网卡芯片名
5. **0005 `nvmeof-adaptation`**：NVMe/TCP SAN 驱动、DH-HMAC-CHAP、认证时序修复与 `hostnqn`
6. **0006 `efi-nvs-backend`**：EFI 变量 NVS 后端，含空块删除语义
7. **0007 `tofu-fingerprint`**：TOFU 指纹固定与校验
8. **0008 `devicekey-commands`**：ECDSA P-256 设备身份命令
9. **0009 `nvmetcp-nbct-acpi-table`**：NBCT ACPI 表与内核凭据交接
10. **0010 `tls13-psk`**：TLS 1.3 PSK 客户端与记录层

除补丁外，`embed/auto.ipxe` 是构建级 EMBED 资产，经 `EMBED=` 编译进 `direct-uefi` 产物，不参与补丁序列。补丁的许可边界不完全一致：RTL8125/8126 适配部分仅按 GPL-2.0 授权，不得以 UBDL 再分发；其余自研补丁按其文件头声明授权。再分发前应阅读 `patches/README`。

## 设备信息

固件采集四组设置，经 HTTP GET 上报：

| 组 | 设置 | 来源 |
| --- | --- | --- |
| 身份 | `uuid`、`manufacturer`、`product`、`serial`、`board-serial`、`asset` | SMBIOS type 1/2/3 |
| CPU | `cpuvendor`、`cpumodel` | CPUID |
| 内存 | `mem-total`、`mem-type`、`mem-speed` | SMBIOS type 17（补丁 0004） |
| 网卡 | `netX/mac`、`netX/chip`、`netX/busid`、`netX/bustype`、`netX/linktype` | netdev 设置；`chip` 由补丁 0004 启用 |

数值设置按 iPXE 约定以十六进制展开，服务端需同时兼容十六进制与十进制解析。字符串可能包含空格与特殊字符，上报前应确认 URL 编码行为。无 SMBIOS type 17 的环境取不到内存字段；SNP 引导时 `${netX/chip}` 显示 `snpnet`，只有原生驱动才显示芯片型号。字段级契约见[固件契约](../reference/firmware-contract.md)。

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

## 设备身份与信任

固件提供设备身份密钥、NVRAM 持久化与 TOFU 信任能力；注册、挑战-响应与授权由控制面引导脚本编排。

| 命令 | 行为 | 失败路径 |
| --- | --- | --- |
| `keygen` | 以 EFI RNG 为熵源生成 ECDSA P-256 私钥，写入 `device-key` | 密钥已存在时拒绝覆盖，退出非零 |
| `pubkey` | 导出 130 位十六进制未压缩公钥 `04‖X‖Y`，并写入临时设置 `pubkey` | 无 `device-key` 时报错 |
| `sign <data...>` | 参数按顺序无分隔拼接，SHA-256 后以 ECDSA P-256 签名，输出 base64(DER)，并写入临时设置 `sig` | 无 `device-key` 时拒绝签名 |
| `nvclear` | 清除 `device-key`、`server-fingerprint` 与 `trust` | 任一项失败时末行输出 `incomplete`，退出非零 |

| 设置 | 类型 | 持久性 | 来源 |
| --- | --- | --- | --- |
| `device-key` | hexraw | NVRAM | `keygen` |
| `server-fingerprint` | hexraw | NVRAM | TOFU |
| `trust` | hex | NVRAM | TOFU 镜像的根证书覆盖 |
| `pubkey` | hexraw | 内存 | `pubkey` |
| `sig` | string | 内存 | `sign` |

持久设置是封装 tag，清理时必须带 `nvo/` 前缀，例如 `clear nvo/device-key`；裸 `clear device-key` 只清除内存副本。一次清理三项使用 `nvclear`。

签名数据：

```text
data = nonce ‖ mac
signature = base64( DER( ECDSA-P256( SHA-256( data ) ) ) )
```

`nonce` 由控制面下发，一次性使用；`mac` 为设备引导网卡 MAC，小写冒号格式。主机名不参与签名，身份约束由 MAC 到设备台账的绑定与一次性 nonce 保证。公钥为未压缩点 `0x04‖X(32B)‖Y(32B)`，签名编码为 ASN.1 DER，传输使用标准 base64。

TOFU 信任：

| 阶段 | 条件 | 行为 |
| --- | --- | --- |
| 首次接触 | 未保存 `server-fingerprint` | 接受握手，写入叶证书 DER 的 SHA-256 指纹 |
| 已固定且匹配 | 指纹一致 | 正常下载 |
| 已固定且不匹配 | 指纹不一致 | 拒绝连接，不覆盖已有指纹 |

TOFU 是全局单锚：`server-fingerprint` 一旦存在，固件只信任首个 HTTPS 连接中被固定的证书。所有 HTTPS 目标必须共享同一信任锚，或使用可链到该证书的证书链；其余流量应走 TFTP/HTTP。控制面证书轮换后，必须先清除 NVRAM 指纹，否则设备无法再次连接。

生命周期：

1. 首次启动执行 `keygen`，生成设备私钥；后续启动因密钥已存在而跳过。
2. `pubkey` 导出公钥，随设备信息上报控制面完成注册。
3. 控制面下发一次性 nonce；设备执行 `sign ${nonce}${net0/mac}` 并回传签名。
4. 验签通过后下发引导变量，设备进入正常引导。
5. 更换硬件或整机替换形成新设备身份；系统盘与 Worker 身份仍保留在控制面，通过设备换绑完成迁移。
6. 身份重置使用 `nvclear` 或 `clear nvo/device-key`，之后必须重新注册。

安全边界：

- **私钥不出设备**：`device-key` 仅供固件内部使用；公钥与签名可导出。
- **私钥可被启动后的系统读取**：设备私钥保存在带运行时访问权限的 EFI 变量中，启动后的操作系统可以读取。Secure Boot 不关闭这一读取路径，详见[安全模型](../architecture/security.md)。
- **首次接触无认证**：TOFU 在注册窗口内接受首个证书，中间人风险由 PXE 网段隔离承担。
- **nonce 必须一次性**：签名可被重放的前提是 nonce 重复；控制面需保证 nonce 一次性、短 TTL 并绑定 MAC。

## NVMe-oF 引导

固件内置自研 `nvmetcp` 驱动，提供 NVMe over TCP 的 SAN 引导能力。以下 NQN 与地址用于本地验证，示例使用生产格式 `nqn.2026-07.com.kurrent:worker-01.ubuntu.0123456789ab`；生产盘 NQN 由控制面按节点 `spec.agent.nqnBase` 生成，默认前缀为 `nqn.2026-07.com.kurrent`，`sanboot` URI 中的 NQN 必须与 target 实际子系统一致。

- 协议流程：ICReq/ICResp 协商 → Connect（Admin）→ 认证 → Property Set（CC.EN=1）→ Identify（控制器/命名空间）→ I/O 队列 Connect → 块读写。
- 认证：DH-HMAC-CHAP 单向认证，哈希 SHA-256/384/512，DH 组 0/2048/3072/4096。
- 仅支持 TCP 传输；不支持 RDMA 与 FC。
- 引导路径为单控制器、单命名空间；Identify 解析 512 B 与 4 KB 块。

### Target 配置

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

#### 服务端错误速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `base64 key decoding error -1` | `DHHC-1:XX:` 前缀不足 10 字节 | 保持两位数字 |
| `dhchap status 2` | 密钥 CRC32 不是终值 | 用 `zlib.crc32` 重新生成 |
| 免认证直连 | `attr_allow_any_host=1` | 置 0 后重建子系统 |
| `Can't set allow_any_host when explicit hosts are set!` | 残留 `allowed_hosts/` 链接 | 清理旧链接后重试 |

### Host NQN

固件的 Host NQN 默认来自 SMBIOS UUID；QEMU/OVMF 不提供 UUID 时回退为固定 `nqn.2014-08.org.ipxe:ipxe`。控制面按 Worker 下发 `hostnqn`，并通过 `hostnqn` 设置项覆盖默认值。target 启用 `allow_any_host=0` 时，`hosts/` 目录名必须与设备实际发送的值一致。

### sanboot 与凭据注入

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

密钥不固化在固件中。控制面 `/boot-vars` 按设备返回 iPXE 脚本，设置引导变量：

```ipxe
dhcp || goto failed
chain --autofree https://<control-plane>/devices/challenge?mac=${net0/mac} || goto failed
isset ${nonce} || goto failed
sign ${nonce}${net0/mac} || goto failed
chain --autofree https://<control-plane>/boot-vars?mac=${net0/mac}&hostname=${hostname}&nonce=${nonce}&sig=${sig} \
  || goto failed
sanboot nvme://<traddr>:<trsvcid>/<nqn>?secret=${nbft-secret}
:failed
shell
```

启用数据面 TLS 时，控制面同时下发 `nbft-tls-psk` 与 `nbft-tls-port`，脚本在 URI 追加 `&tls_psk=${nbft-tls-psk}&tls_trsvcid=${nbft-tls-port}`。端口或密钥缺失时，引导脚本应进入失败分支，不回落明文端口。`/boot-vars` 不要求登录会话，凭据只在一次性 nonce 与设备签名验证通过后注入；未验签的请求只能取得非凭据变量。

### TLS 1.3 双段加密

当 `sanboot` URI 的端口等于 `tls_trsvcid` 时，固件对该连接执行 TLS 1.3 PSK 握手：

- PSK identity 为 `NVMe0R01 <hostnqn> <subsysnqn>`。
- 握手与记录层由 0010 补丁实现，使用 `TLS_AES_128_GCM_SHA256` 与 `psk_dhe_ke`。
- 同一组参数随 NBCT 交给 initramfs；内核侧在 `.nvme` keyring 注入 PSK，并以 `--tls` 重连数据端口。

默认路径为固件段与内核段双段加密；HTTPS 引导资产仍走 iPXE 原生 TLS（上限 1.2），与数据面 TLS 1.3 相互独立。

### 验证

QEMU/OVMF 验证需要 KVM、OVMF 固件与可用的 nvmet target。固件仓库提供脚本：

```bash
sudo AUTH=1 bash test/nvmet-setup.sh
python3 test/cred-server.py &
# 测试脚本：test/nvmeof-auth-test.ipxe
bash test/run-qemu-auth.sh
```

判据：串口出现认证成功、Property Set、I/O 队列与 GRUB 输出；target 端出现固件段与内核段两条控制器记录；抓包中各客户端 TCP 流首字节为 `0x16`，明文端口包数为 0，载荷中无 PSK 或 DH-HMAC-CHAP 密钥明文。`test/cred-server.py` 仅用于验证，不可作为生产凭据端点。

### 常见失败

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| Identify 返回 `0x8018` | Property Set 被跳过，认证未完全收尾 | 确认固件包含完整补丁链 |
| 日志无 `sending Property Set (CC)` | 认证未完成或阶段竞态 | 检查 target 认证配置与密钥 |
| 认证后连接重置 | AuthReceive 重复发送 | 使用包含 0005 认证修复的固件 |
| `authentication required but no secret` | target 要求认证但 URI 无 `secret` | 检查 `/boot-vars` 变量注入 |
| Connect 阶段失败 | NQN、端口或 Host NQN 不匹配 | 核对 target 配置与 `ss -tln` |
| TLS 握手失败 | URI 端口不是 `tls_trsvcid`，或 PSK 不匹配 | 核对控制面下发参数与数据面端口 |

## 运行边界

- 构建产物仅包含 UEFI 形态，共 7 个。
- 仅支持 NVMe/TCP；不支持 RDMA 与 FC。
- 引导路径为单控制器、单命名空间；Identify 解析 512 B 与 4 KB 块。
- DH-HMAC-CHAP 为单向认证；双向认证不在固件实现范围内。
- HTTPS 下载使用 iPXE 原生 TLS，上限 TLS 1.2；数据面 `sanboot` 使用 TLS 1.3 PSK。
- 主线固件未签名；Secure Boot 开启时无法加载，需在部署前关闭。
- 固件只提供命令与协议能力，不实现注册窗口、Worker 绑定、盘选择或凭据签发。
- iPXE 侧生成 NBFT 仍待实现；当前验证链路使用注入的 NBFT 表模拟固件预生成阶段。

## 相关页面

- [构建与部署](./build-deploy.md)
- [固件契约](../reference/firmware-contract.md)
- [引导链](../concepts/boot-chain.md)
- [系统盘定制](../guide/system-disk.md)
- [发行版矩阵](../reference/distributions.md)
- [安全模型](../architecture/security.md)
- 固件仓库源码、补丁与研究记录：<https://github.com/dutyc/kurrent-firmware>
