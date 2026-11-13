# 固件契约

本页描述固件与主仓库之间的接口：TPM 身份命令、NV 设置、TOFU 行为、挑战-证明格式、控制面上报与启动变量。字段级权威定义在固件仓库的 `docs/device-trust-usage.md`、`docs/capability-reference.md` 与 `patches/README`；控制面端点细节以 `api/control-plane-api.zh-CN.md` 为准。

## 设备身份命令

固件通过 TPM 2.0 提供设备身份；注册、挑战与准入逻辑由主仓库引导脚本与控制面实现。

| 命令 | 前置条件 | 输出 | 持久性 |
| --- | --- | --- | --- |
| `akgen` | 无 | 在 TPM 内创建 restricted signing 密钥（ECC P-256 或 RSA-2048），持久化到 TPM NV；已存在时复用、不覆盖 | TPM NV |
| `akpub` | AK 存在 | 输出 AK 公钥 SPKI DER hex、TPM2B_NAME（68 hex）与算法类型，并读取固件的 Secure Boot 状态，写入临时设置 `ak-public`、`ak-name`、`ak-type`、`sb` | 仅本次启动 |
| `quote <nonce>` | AK 存在 | 对 PCR 0/2/4/7 做 SHA-256 quote，nonce 原样写入 `extraData`；输出 `quote`（TPMS_ATTEST 原始字节 hex，不含 TPM2B 长度前缀）与 `quote-sig`（对 quote 字节的签名 hex） | 仅本次启动 |

要点：

- `akgen` 是幂等入口：引导脚本无条件调用一次，首次创建、之后发现已有 AK 即跳过。
- AK 私钥由 TPM 生成并保管，不可导出，固件也不读取私钥明文。
- PCR 读取与 quote 必须在同一次 TPM 调用内完成，避免两次读取之间的测量变化。
- `quote` 的 `extraData` 必须是 `nonce` 字符串本身的 ASCII 字节（64 个 hex 字符，即 64 字节），不是 hex 解码后的 32 字节；不做任何其他编码变换。

## NV 设置

| 设置名 | 类型 | DHCP 标签 | 持久性 | 写入方 |
| --- | --- | --- | --- | --- |
| `server-fingerprint` | hexraw | 0x5f | NVRAM | TOFU |
| `trust` | hexraw | 0x5a | NVRAM（覆盖根证书表） | TOFU |
| `ak-public` | hexraw | 0x60 | 内存 | `akpub` |
| `ak-name` | hexraw | 0x63 | 内存 | `akpub` |
| `ak-type` | string | 0x64 | 内存 | `akpub` |
| `sb` | string | 0x65 | 内存 | `akpub`（`1` = Secure Boot 已开启，`0` = 关闭） |
| `quote` | hexraw | 0x61 | 内存 | `quote` |
| `quote-sig` | hexraw | 0x62 | 内存 | `quote` |

清除持久设置必须带 `nvo/` 块前缀：

```text
clear nvo/server-fingerprint  # 解除 TOFU pin，下次 HTTPS 重新固定
```

裸 `clear server-fingerprint` 或空值 `set server-fingerprint` 对 NVS 后端无效。AK 不使用 iPXE 设置持久化；清除 TPM 或更换主板会失去原 AK，只能删除登记后重新入池。

## TOFU

TOFU 在 TLS 握手路径自动生效，没有命令与配置项。

| 状态 | 条件 | 行为 |
| --- | --- | --- |
| 首次接触 | 无 `server-fingerprint`，证书校验失败 | 接受握手，把叶证书 SHA-256 指纹写入 `server-fingerprint`，并镜像至 `trust` |
| 已 pin 且匹配 | 指纹存在且一致 | 校验通过，正常下载 |
| 已 pin 且不匹配 | 指纹存在但不一致 | 拒绝握手，HTTP 请求不会发出，不覆盖已有指纹 |

约束：

- 指纹是**叶证书 DER 的 SHA-256**，可用 `openssl x509 -outform DER | sha256sum` 独立核对。
- pin 是全局单锚：固件只信任首个 HTTPS 连接固定的证书。所有 HTTPS 目标必须共用同一证书或可链到该证书；其余流量走 TFTP/HTTP。
- HTTPS 由 iPXE 原生 TLS 实现，版本上限为 1.2。
- 控制面证书轮换后，设备必须清除 `server-fingerprint` 重新固定，否则引导会被拒绝。

## 挑战-证明

控制面签发一次性 nonce：

```text
GET /devices/challenge?mac=<MAC>
→ #!ipxe
  set nonce <64 个 hex 字符>
```

设备执行：

```text
quote ${nonce}
```

控制面验证约定：

| 项 | 值 |
| --- | --- |
| quote 本体 | TPMS_ATTEST：magic `0xFF544347`、type `TPM_ST_ATTEST_QUOTE` |
| `extraData` | 等于 nonce 字符串的 ASCII 字节（64 hex 字符） |
| PCR 选择 | 固定 0/2/4/7，仅 SHA-256 |
| PCR 摘要 | 32 字节 SHA-256 |
| AK 签名 | ECC P-256 为 ASN.1 DER；RSA-2048 为 PKCS#1 v1.5，SHA-256 |
| 传输编码 | hex（quote 与签名均为 hex） |

nonce 一次性使用、短 TTL、绑定 MAC。设备不存在、未认领或已吊销时挑战端点返回 404。证明不包含主机名：身份由 MAC 对应的设备台账、AK 与一次性 nonce 约束。

## 设备上报

```text
GET /devices/report?mac=<MAC>&ak_public=<hex>&ak_name=<hex>&ak_type=ecc_p256&sb=1&uuid=...&manufacturer=...
```

| 字段 | 说明 |
| --- | --- |
| `mac` | 必填，设备网卡 MAC |
| `ak_public` | AK 公钥 SPKI DER hex；与 `ak_name` 配对，注册与 AK 认领的必要条件 |
| `ak_name` | TPM2B_NAME hex（68 hex：算法前缀 + 公钥摘要） |
| `ak_type` | `ecc_p256`（默认）或 `rsa2048`；与实际公钥算法不符则视为无身份 |
| `sb` | Secure Boot 状态，`1` = 已开启；注册期必须为 `1` |
| `uuid` | SMBIOS UUID，已有值不会被自报值覆盖 |
| `manufacturer` / `product` / `serial` | 厂商、型号、序列号 |
| `cpumodel` | CPU 型号 |
| `mem-total` / `mem-type` / `mem-speed` | 内存容量、类型、速率 |
| `chip` / `busid` | 网卡芯片与 PCI 地址 |

行为：

- 已在册设备：合并指纹、更新 `last_seen`；注册窗口内带有效 AK 可完成认领，AK 不一致时拒绝覆盖并记录审计。
- 未知 MAC：只在注册窗口开启、放行额度未用尽、携带有效 AK 且 `sb=1` 时入池；否则忽略。
- 已吊销设备：忽略上报，不更新、不复活。
- 响应为空 `#!ipxe` 脚本，不产生脚本副作用。

上报不鉴权，控制面按来源 IP 限速。

## 启动变量

```text
GET /boot-vars?mac=<MAC>&hostname=<name>&nonce=<nonce>&quote=<hex>&quote_sig=<hex>
```

响应默认为 `#!ipxe` 脚本；加 `format=json` 返回同字段的 JSON（snake_case）。编码方式不改变准入条件。

准入顺序：

1. **识别**：MAC → 设备台账 → `bound_worker_id`；主机名仅作存量兜底。
2. **绑定校验**：带 MAC 的请求必须来自该 Worker 绑定的设备，否则返回空脚本。
3. **证明**：校验 quote 结构、AK 签名与 `extraData == nonce`，再比对 PCR 摘要与平台基线。未通过证明的请求拿不到任何密钥；证明强制（默认开启）下直接拒绝。
4. **数据面就绪**：nvmet 后端必须同时具备认证密钥与就绪的数据面端口，否则拒绝下发。
5. **投影**：按下表生成变量，全部在本次引导内有效。

| 变量 | 含义 | 条件 |
| --- | --- | --- |
| `disk-nqn` | 系统盘完整 NQN（原样下发给固件） | 有默认盘 |
| `storager-ip` | 数据面地址 | 有默认盘且其存储节点已登记 |
| `nbft-tls-port` | 数据面 TLS 端口 | nvmet 后端且端口就绪 |
| `nbft-tls-psk` | 由认证密钥派生的 TLS PSK | 证明通过且已有认证密钥 |
| `nbft-secret` | DHHC-1 认证密钥 | 证明通过且已有认证密钥 |
| `hostnqn` | Worker 的 Host NQN | 节点与盘 NQN 可用 |
| `os` / `os-version` | 菜单展示的系统标识 | 默认盘带系统信息 |
| `menu-default` / `menu-timeout` | 自动启动项与超时（毫秒） | 总是下发 |
| `base-iqn` | 盘 IQN 前缀，用于派生 `initiator-iqn` | 有默认盘 |
| `iscsi-sep` | iSCSI 后端连接符（当前引导脚本不消费） | 仅 stgt / lio 后端 |

无默认盘时控制面返回 `menu-default=reboot` 与短超时，设备重启等待建盘或设置默认盘；不会构造明文回退。

## 引导脚本变量

`boot.ipxe.cfg` 由平台维护，操作者通常无需修改。它定义：

| 变量 | 含义 |
| --- | --- |
| `controller_ip` | 控制面地址，默认取 DHCP `next-server` |
| `https-port` / `https-url` | 控制面 HTTPS 入口 |
| `menu-url` | 菜单脚本的绝对 URL |
| `storager-ip` | 数据面默认地址（可被 `/boot-vars` 覆盖） |
| `base-iqn` | 基础 IQN，用于派生 `initiator-iqn` |
| `iscsi-sep` | iSCSI 连接符默认值（当前引导脚本不消费） |
| `initiator-iqn` | iSCSI 发起端名称 |

## 数据面 URI

菜单按以下形式拼装 NVMe/TCP 根路径：

```text
nvme://<storager-ip>:<nbft-tls-port>/<disk-nqn>?secret=<nbft-secret>&tls_psk=<nbft-tls-psk>&tls_trsvcid=<nbft-tls-port>
```

- 有 `nbft-secret` 时附加 `secret=`；无认证目标的旧部署可以省略。
- `tls_psk` 与 `tls_trsvcid` 始终附加，用于固件段 TLS 1.3 握手。
- 缺少 `disk-nqn`、`nbft-tls-port` 或 `nbft-tls-psk` 时菜单拒绝引导，不回落明文端口。
- 启动成功后 `keep-san 1` 保留 SAN 连接，交给内核与 initramfs。

## NBCT 与 NBFT

**NBCT**（NVMe Boot Credentials Table）是固件在 sanboot 时安装的 ACPI 表，每个已认证会话一条定长记录：

| 字段 | 内容 |
| --- | --- |
| `traddr` | 数据面地址 |
| `trsvcid` | 数据面端口 |
| `nqn` | 子系统 NQN |
| `hostnqn` | Host NQN |
| `secret` | DH-HMAC-CHAP 密钥 |

NBCT 只存在于内存，不落 NVRAM。initramfs 的 `nbft-connect` 读取 `/sys/firmware/acpi/tables/NBCT*`，以 `--dhchap-secret` 与 `--hostnqn` 逐条重连，无需二次网络获取凭据；没有 NBCT 时回退 `nvme connect-all --nbft`。

**NBFT**（NVMe Boot Firmware Table）是发现表，描述传输地址、子系统与网卡信息。iPXE 侧生成 NBFT 尚未进入主线；当前验证链路使用注入的 NBFT 表模拟固件预生成阶段。固件主线负责 NBCT 接力，NBFT 消费位于 initramfs。

## 版本与兼容

- 完整链路要求固件包含 NVMe/TCP、认证、NVS、TOFU、TPM AK 与 quote、Host NQN 与 NBCT 补丁；缺少 TPM 身份的固件无法入池，也无法取得凭据。
- 控制面不向未通过证明的请求下发任何密钥；证明强制默认开启。
- 升级控制面不要求同时升级固件，但新增变量（例如新的 TLS 端口）需要固件侧能够消费；升级前应核对固件能力。

## 安全边界

- 密钥只在证明通过后注入，`/boot-vars` 不提供注册通道。
- AK 私钥不可导出，引导后的系统无法读取或复制设备身份；证明覆盖 PCR 0/2/4/7，不覆盖运行期。
- 首跳依赖 Secure Boot 验证签名固件；签名固件只信任构建时嵌入的 boot CA，不使用首次连接信任。
- TPM 或主板被清除/更换后原 AK 丢失，需删除登记后重新入池；退役设备应清除 `server-fingerprint`。

## 相关页面

- [固件概述](../firmware/index.md)
- [固件平台参考](./firmware-platform.md)
- [构建与产物](../firmware/build-deploy.md)
- [预置与刷写](../firmware/build-deploy.md)
- [设备身份与信任](../firmware/index.md#设备身份与信任)
- [引导链](../concepts/boot-chain.md)
- [安全模型](../architecture/security.md)
- [API 参考](./api.md)
