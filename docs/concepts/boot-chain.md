# 引导链

引导链回答启动瞬间的三个问题：以谁的身份、连哪个目标、挂哪块盘。Kurrent 不把答案写进机器或盘，而是由控制面按当前绑定关系在每次启动时投影。

本页描述主路径（NVMe/TCP + TLS 1.3）。固件侧的构建与预置见[固件](../firmware/index.md)，信任边界见[安全模型](../architecture/security.md)。

## 总览

```text
设备上电
  → DHCP（dnsmasq）      地址、next-server
  → TFTP                 snponly.efi + boot.ipxe 指针
  → HTTPS + TOFU         boot.ipxe.cfg（后续脚本全部走 HTTPS）
  → /devices/report      指纹 + 设备公钥
  → /devices/challenge   一次性 nonce
  → sign(nonce‖mac)      设备私钥签名
  → /boot-vars           验签与准入后投影盘 NQN、Host NQN、凭据
  → menu.ipxe            菜单超时选择默认盘
  → sanboot nvme://...   NVMe/TCP 4421，DH-HMAC-CHAP + TLS 1.3
  → NBFT / NBCT          固件向 initramfs 交接连接信息与密钥
  → 内核重连 → rootfs → login
```

控制面与固件分工明确：固件提供协议驱动、设备密钥命令、NVRAM 与 TOFU 能力；引导脚本与准入判断由主仓库维护。

## 1. 网络引导

设备通过 DHCP 取得地址与 `next-server`，从 TFTP 下载两件东西：

- `snponly.efi`：首跳固件。
- `boot.ipxe`：只做一次 HTTPS 跳转的指针。

从 `boot.ipxe.cfg` 开始，所有脚本、菜单与设备接口都走 HTTPS。服务器证书是控制面启动时生成的自签证书，设备在首次握手时固定其指纹（TOFU）。

TFTP 根只暴露这两个文件。更新 `snponly.efi` 后需要重启 dnsmasq 容器：文件级挂载固定 inode。

## 2. 设备身份

首次启动时，固件执行 `keygen`，在 EFI NVRAM 中生成 ECDSA P-256 私钥；私钥不出机器。后续启动发现密钥已存在时拒绝覆盖。

`pubkey` 导出公钥后，固件向 `/devices/report` 上报：

- MAC、UUID、SMBIOS、CPU、内存、网卡等 11 项指纹；
- 设备公钥。

控制面按 MAC 登记设备池。**上报不等于授权**：

- 未知 MAC 只有在注册窗口开启且携带有效公钥时才入池；
- 已入池但未绑定的设备不会被授予任何启动凭据；
- 窗口关闭后，上报只更新指纹，不新增条目。

设备密钥可以在线轮换：用旧密钥签名 `rotate‖mac‖new_pubkey‖nonce`，成功后旧密钥立即失效。

## 3. 挑战-应答

绑定与验签强制开启后，仅凭“知道某个 MAC”不足以取得凭据。设备向 `/devices/challenge` 请求一次性 nonce，然后用私钥签名：

```text
nonce ‖ mac          （无分隔拼接，ECDSA P-256 / SHA-256）
```

nonce 一次性使用并绑定 MAC；`report`、`challenge` 与 `boot-vars` 按来源 IP 限速。签名结果随 `/boot-vars` 请求提交，控制面用设备公钥验证。

## 4. 准入与变量投影

`/boot-vars` 是引导链上最关键的一次判断。控制面依次检查：

1. **识别**：请求 MAC 经设备台账反查 Worker；未传 MAC 或台账未命中时，才用 hostname 兜底。
2. **绑定即认证**：请求 MAC 必须属于命中 Worker 的绑定设备，其他设备不能冒领该 Worker 的引导身份。
3. **身份验签**：验签强制开启时，无密钥、无签名或验签失败一律拒绝。
4. **凭据条件**：Worker 已绑定且密钥库有条目；nvmet 后端还要求存储节点数据面端口就绪。nvmet 条件不满足时拒绝下发；非 nvmet 后端不投影 NVMe-oF 凭据。

通过检查后按设备是否完成验签投影变量：凭据只发给验签通过的设备，非凭据变量在过渡模式下可以下发。

| 变量 | 内容 |
| --- | --- |
| `disk-nqn` | 默认盘的完整 NQN |
| `storager-ip` | 持有该盘的存储节点地址 |
| `hostnqn` | 该 Worker 的发起端 Host NQN |
| `nbft-secret` | DH-HMAC-CHAP 认证密钥 |
| `nbft-tls-psk` / `nbft-tls-port` | 由认证密钥派生的 TLS PSK 与数据面端口 |
| `menu-default` | 菜单超时后的启动项 |

没有默认盘时返回 `menu-default=reboot`；池中未绑定设备同样进入重启循环，等待绑定。nvmet 后端缺少密钥或端口未就绪时，控制面拒绝下发完整变量；非 nvmet 后端不投影 NVMe-oF 密钥与 PSK。所有路径都不拼接明文 URI。

## 5. 菜单

`menu.ipxe` 经 HTTPS 加载，显示当前默认盘的 OS 标识：

- 超时后启动默认盘；
- 要切换系统盘，在 WebUI 的 Worker 详情中设置另一块盘为默认盘，下一次启动生效；
- 启动项来自台账投影，不依赖盘内配置。

## 6. 数据面连接

`sanboot` 的目标形如：

```text
nvme://<storager>:<tls_port>/<disk_nqn>?secret=<dhhc1>&tls_psk=<psk>&tls_trsvcid=<tls_port>
```

连接分两层完成：

- **认证**：DH-HMAC-CHAP 完成连接控制，回答“谁可以连”。
- **加密**：TLS 1.3 在同一个端口 `4421` 上保护传输内容。

固件自己完成 TLS 握手。认证失败、PSK 缺失或端口未就绪时连接失败；没有明文回退端口。

## 7. 交接给操作系统

固件在 `sanboot` 时安装一张 **NBCT** 凭据表（ACPI 表），记录本会话的：

- target 地址与端口；
- 子系统 NQN 与 Host NQN；
- DH-HMAC-CHAP 密钥。

TLS PSK 与数据面端口随 `sanboot` URI 交给后续阶段；NBFT 负责网络与子系统发现信息。

initramfs 的 `nbft-connect` 消费 **NBFT**（网络与子系统发现）与 **NBCT**（凭据与 Host NQN 中继），用同一端口以 `--tls` 重连，挂载根文件系统。

盘内不写死机器身份：身份、凭据与连接信息都来自本次启动的投影。

> 当前状态：NBCT 由固件生成；iPXE 侧生成 NBFT 仍待实现。已验证的整机链路用注入的 NBFT 表模拟“固件预生成”阶段。

## 失败路径

| 现象 | 原因 |
| --- | --- |
| 设备未入池 | 注册窗口未开启；上报缺少有效公钥；设备已被吊销 |
| 设备入池但无法启动 | 未绑定 Worker；或 Worker 没有系统盘 |
| 设备反复重启 | Worker 没有默认盘；或池中设备尚未绑定 |
| 验签强制后提示无密钥/无签名 | 设备未完成密钥认领；引导脚本未取得 nonce |
| 启动变量不下发 | Worker 无凭据；后端不是 nvmet；数据面端口未就绪 |
| 菜单超时后重启 | 未设置默认盘 |
| 数据面连接失败 | PSK 未送达存储节点 keyring；`tlshd` 版本或内核 TLS 配置不满足 |
| HTTPS 握手失败 | 控制面证书轮换后，设备 NVRAM 仍固定旧指纹 |

## 信任边界

第一跳（DHCP → TFTP `snponly.efi` → TFTP `boot.ipxe`）没有密码学保护：设备固件不验证这段内容，iPXE 也尚未取得服务器证书。部署把 PXE 网段本身作为信任边界，并在其上保留：

- 设备身份 TOFU（NVRAM 密钥 + 注册窗口 + 绑定）；
- 服务器 TOFU；
- 挑战-应答与绑定即认证；
- 数据面加密与无明文回退。

同一二层内的攻击者、被控宿主或物理访问不在该边界内解决。完整的威胁模型与升级路径见[安全模型](../architecture/security.md)。

## 固件分工

| 能力 | 提供方 |
| --- | --- |
| DHCP/TFTP 客户端、HTTPS、TOFU、NVRAM | 固件 |
| `keygen` / `pubkey` / `sign` | 固件 |
| NVMe/TCP 驱动、DH-HMAC-CHAP、NBCT | 固件 |
| 引导脚本、注册窗口、`/boot-vars` 准入 | 主仓库控制面 |
| 系统盘、凭据、菜单生成 | 主仓库控制面 |

固件的构建、产物与设备预置见[固件概述](../firmware/index.md)与[预置与刷写](../firmware/provision.md)。
