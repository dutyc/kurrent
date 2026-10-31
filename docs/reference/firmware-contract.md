# 固件契约

本页描述固件与主仓库之间的接口：设备身份命令、NVRAM 设置、TOFU 行为、挑战-应答格式、控制面上报与启动变量。字段级权威定义在固件仓库的 `docs/device-trust-usage.md`、`docs/capability-reference.md` 与 `patches/README`；控制面端点细节以 `api/control-plane-api.zh-CN.md` 为准。

## 设备身份命令

固件提供三个命令，业务逻辑（注册、挑战、准入）全部由主仓库引导脚本与控制面实现。

| 命令 | 前置条件 | 输出 | 持久性 |
| --- | --- | --- | --- |
| `keygen` | 无 | 生成 32 字节 ECDSA P-256 私钥，写入 `device-key`；已存在时拒绝覆盖并返回非零 | NVRAM |
| `pubkey` | `device-key` 存在 | 标准输出 130 个 hex 字符（65 字节未压缩点 `04‖X‖Y`），同时写入临时设置 `pubkey` | 仅本次启动 |
| `sign <data...>` | `device-key` 存在 | 参数按顺序无分隔拼接，SHA-256 → ECDSA P-256 → DER → base64，写入临时设置 `sig` | 仅本次启动 |

要点：

- `keygen` 是幂等入口：引导脚本无条件调用一次，首次生成、之后失败跳过。
- 私钥只在固件内部使用，不写入磁盘，也不通过网络传输。
- `pubkey` 与 `sig` 是命令输出暂存，重启即失；不能跨启动读取。

## NVRAM 设置

| 设置名 | 类型 | DHCP 标签 | 持久性 | 写入方 |
| --- | --- | --- | --- | --- |
| `device-key` | hexraw | 0x5e | NVRAM | `keygen` |
| `server-fingerprint` | hexraw | 0x5f | NVRAM | TOFU |
| `trust` | hexraw | 0x5a | NVRAM（覆盖根证书表） | TOFU |
| `pubkey` | hexraw | 0x60 | 内存 | `pubkey` |
| `sig` | string | 0x61 | 内存 | `sign` |

清除持久设置必须带 `nvo/` 块前缀：

```text
clear nvo/device-key          # 删除设备私钥，下次启动重新生成
clear nvo/server-fingerprint  # 解除 TOFU pin，下次 HTTPS 重新固定
```

裸 `clear device-key` 或空值 `set device-key` 对 NVS 后端无效。

删除 `device-key` 等同于更换设备身份，需要重新登记与绑定；删除 `server-fingerprint` 后，下一次 HTTPS 连接重新进入首次信任状态。两项都是敏感操作，应通过管理流程或受控预置执行。

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

## 挑战-应答

控制面签发一次性 nonce：

```text
GET /devices/challenge?mac=<MAC>
→ #!ipxe
  set nonce <64 个 hex 字符>
```

设备签名：

```text
sign ${nonce}${mac}
```

签名数据为 `nonce‖mac` 无分隔拼接。控制面验签约定：

| 项 | 值 |
| --- | --- |
| 摘要 | SHA-256 |
| 算法 | ECDSA P-256 |
| 签名编码 | ASN.1 DER |
| 传输编码 | base64（标准字母表，无换行） |
| 公钥 | 未压缩点 `04‖X‖Y`，130 hex |

nonce 一次性使用、短 TTL、绑定 MAC。设备不存在、未认领或已吊销时挑战端点返回 404。签名不包含主机名：身份由 MAC 对应的设备台账与一次性 nonce 约束。

在线轮换使用同一挑战端点取得的 nonce，签名数据为：

```text
rotate‖mac‖new_pubkey‖nonce
```

由旧私钥签名。成功后设备公钥立即换成新值，旧密钥在引导验签链路上失效。

## 设备上报

```text
GET /devices/report?mac=<MAC>&pubkey=<130 hex>&uuid=...&manufacturer=...
```

| 字段 | 说明 |
| --- | --- |
| `mac` | 必填，设备网卡 MAC |
| `pubkey` | ECDSA P-256 公钥，注册与密钥认领的必要条件 |
| `uuid` | SMBIOS UUID，已有值不会被自报值覆盖 |
| `manufacturer` / `product` / `serial` | 厂商、型号、序列号 |
| `cpumodel` | CPU 型号 |
| `mem-total` / `mem-type` / `mem-speed` | 内存容量、类型、速率 |
| `chip` / `busid` | 网卡芯片与 PCI 地址 |

行为：

- 已在册设备：合并指纹、更新 `last_seen`；注册窗口内带有效公钥可认领密钥，密钥不一致时拒绝覆盖并记录审计。
- 未知 MAC：只在注册窗口开启、放行额度未用尽且带有效公钥时入池；否则忽略。
- 已吊销设备：忽略上报，不更新、不复活。
- 响应为空 `#!ipxe` 脚本，不产生脚本副作用。

上报不鉴权，控制面按来源 IP 限速。

## 启动变量

```text
GET /boot-vars?mac=<MAC>&hostname=<name>&nonce=<nonce>&sig=<base64>
```

响应默认为 `#!ipxe` 脚本；加 `format=json` 返回同字段的 JSON（snake_case）。编码方式不改变准入条件。

准入顺序：

1. **识别**：MAC → 设备台账 → `bound_worker_id`；主机名仅作存量兜底。
2. **绑定校验**：带 MAC 的请求必须来自该 Worker 绑定的设备，否则返回空脚本。
3. **验签**：用设备台账中的公钥验证 `nonce‖mac`。未验签的请求可以取得非凭据变量，但拿不到任何密钥；验签强制开启后直接拒绝。
4. **数据面就绪**：nvmet 后端必须同时具备认证密钥与就绪的数据面端口，否则拒绝下发。
5. **投影**：按下表生成变量，全部在本次引导内有效。

| 变量 | 含义 | 条件 |
| --- | --- | --- |
| `disk-nqn` | 系统盘完整 NQN（原样下发给固件） | 有默认盘 |
| `storager-ip` | 数据面地址 | Worker 已绑定到节点 |
| `nbft-tls-port` | 数据面 TLS 端口 | nvmet 后端且端口就绪 |
| `nbft-tls-psk` | 由认证密钥派生的 TLS PSK | 验签通过且已有认证密钥 |
| `nbft-secret` | DHHC-1 认证密钥 | 验签通过且已有认证密钥 |
| `hostnqn` | Worker 的 Host NQN | 节点与盘 NQN 可用 |
| `os` / `os-version` | 菜单展示的系统标识 | 默认盘带系统信息 |
| `menu-default` / `menu-timeout` | 自动启动项与超时（毫秒） | 总是下发 |
| `base-iqn` / `iscsi-sep` | iSCSI 安装器的根路径参数 | 仅 stgt / lio 后端 |

无默认盘时控制面返回 `menu-default=reboot` 与短超时，设备重启等待建盘或设置默认盘；不会构造明文回退。

## 引导脚本变量

`boot.ipxe.cfg` 由平台维护，操作者通常不需要修改。它定义：

| 变量 | 含义 |
| --- | --- |
| `controller_ip` | 控制面地址，默认取 DHCP `next-server` |
| `https-port` / `https-url` | 控制面 HTTPS 入口 |
| `chain-url` | 安装器与工具资产根路径 |
| `menu-url` | 菜单脚本的绝对 URL |
| `storager-ip` | 数据面默认地址（可被 `/boot-vars` 覆盖） |
| `base-iqn` | iSCSI 基础 IQN |
| `iscsi-sep` | iSCSI 连接符（按后端类型覆盖） |
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

- 完整链路要求固件包含 NVMe/TCP、认证、NVS、TOFU、设备身份命令、Host NQN 与 NBCT 补丁；缺少身份命令的旧固件会在 `keygen`/`sign` 处失败，无法取得凭据。
- 控制面不向未验签请求下发任何密钥；验签强制开启后，旧固件无法完成引导。
- 升级控制面不要求同时升级固件，但新增变量（例如新的 TLS 端口）需要固件侧能够消费；升级前先核对固件能力。

## 安全边界

- 密钥只在验签通过后注入，`/boot-vars` 不提供注册通道。
- 设备私钥保存在 NVRAM，引导后的系统可以读取；当前主线没有 Secure Boot/TPM 度量来隔离它。
- TOFU 首次连接没有密码学保护，依赖 PXE 网段隔离；pin 之后具备完整性校验。
- 不要把 NVRAM 内容复制到另一台设备；退役设备应清除 `device-key` 与 `server-fingerprint`。

## 相关页面

- [固件概述](../firmware/index.md)
- [构建与产物](../firmware/build.md)
- [预置与刷写](../firmware/provision.md)
- [引导链](../concepts/boot-chain.md)
- [安全模型](../architecture/security.md)
- [API 参考](./api.md)
