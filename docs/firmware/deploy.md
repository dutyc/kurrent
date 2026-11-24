# 部署与预置

构建好的签名产物经 PXE 网段（或本地 ESP）下发。目标机处于 Setup Mode（或关闭 Secure Boot）时首次网络启动，平台注册窗口将 PK / KEK / db 写入固件并自动重启；重启后固件验签执行同一份镜像。部署完成后，设备应在注册窗口内入池。

## 目标与前提

- 目标设备满足[裸机设备要求](../setup/host-prerequisites.md#裸机设备要求)：x86_64 UEFI、TPM 2.0，且未锁厂商密钥（Setup Mode 或 Secure Boot 关闭）；传统 BIOS（Legacy/CSM）引导不再支持；出厂已锁厂商密钥的机型须经其厂商的管理流程处理；
- 已按[部署](../setup/deployment.md)完成控制面、dnsmasq 与 PXE 网段；
- 已按[构建](./build.md)取得签名产物与 `manifest.json`（镜像内嵌平台密钥描述符与产物签名证书）。

## 部署路径

| 条件 | 路径 |
| --- | --- |
| 主板有 UEFI Network Boot / PXE 入口 | PXE：平台 TFTP 下发签名镜像，平台注册窗口完成密钥写入（默认路径） |
| 主板无 PXE 入口 | 用 `direct-uefi` 签名产物放到本地 ESP；密钥同样经平台注册窗口写入 |
| 出厂已锁厂商密钥 | 经其厂商的管理流程处理 |

### PXE

1. 取得 `dist/secureboot/snponly-sb.efi`、`dist/secureboot/ipxe-sb.efi`（`KURRENT_SB_TARGET=ipxe` 构建）与 `manifest.json`。
2. 安装到主仓库 TFTP 根并重启 dnsmasq：

   ```bash
   sudo install -d -m 0755 /path/to/kurrent/tftp/secureboot
   sudo install -m 0644 dist/secureboot/snponly-sb.efi dist/secureboot/ipxe-sb.efi \
     dist/secureboot/manifest.json /path/to/kurrent/tftp/secureboot/
   sudo install -m 0644 dist/secureboot/snponly-sb.efi /path/to/kurrent/tftp/snponly.efi
   sudo install -m 0644 dist/secureboot/ipxe-sb.efi /path/to/kurrent/tftp/ipxe.efi
   cd /path/to/kurrent
   eval "$(kurrent deployment show -o env)"
   docker compose restart kurrent-dnsmasq
   ```

   `tftp/snponly.efi` 与 `tftp/ipxe.efi` 都是文件级挂载，替换后必须重启容器，否则容器仍持有旧 inode；两份缺一 dnsmasq 无法启动。`tftp/secureboot/` 留作 `/boot-assets` 的产物核对与导出源。

   控制面按设备 `boot_firmware` 选择首跳：默认设备取 `snponly.efi`，`boot_firmware=full` 的设备取全驱动 `ipxe.efi`；同一 PXE 网段可按设备混用。

3. 在控制面开启平台注册窗口（`POST /settings/platform-enrollment`，WebUI 设备页同入口）。
4. 目标机网络启动：镜像先请求 `/boot/phase` 执行 `sbenroll` 写入平台密钥，上报结果后自动重启；重启后 Secure Boot 生效，固件验签执行同一镜像，经 4443 完成上报与挑战。注册完成后关闭窗口。

TFTP 只交付首跳固件（`snponly.efi`，或按设备 `boot_firmware` 选中的全驱动 `ipxe.efi`）与 `boot.ipxe` 指针；签名首跳内嵌静态脚本、boot CA 与引导 HTTPS 端口，控制面地址默认不入镜像。后续请求走 4443 HTTPS，只信任该 boot CA。PXE 网段必须按[安全模型](../concepts/architecture/security.md)隔离。

### 本地介质

使用 `direct-uefi` 签名产物（如 `snponly-sb.efi`）：它内嵌与 PXE 相同的静态脚本，平台注册窗口开启时同样执行密钥写入。

USB / ESP：

1. 准备 FAT32 分区（U 盘或本地 ESP）；
2. 创建目录 `EFI/BOOT/`；
3. 将 `dist/direct-uefi/snponly-sb.efi`（或 `ipxe-sb.efi`）复制为 `EFI/BOOT/BOOTX64.EFI`；
4. 在设备启动菜单中选择该 USB/ESP。

若主板仅识别固定 ESP，将文件放入内置磁盘的同一路径，并在启动项中选择它。

GRUB 链加载：

```text
menuentry "Kurrent firmware" {
    chainloader /boot/efi/EFI/kurrent/snponly-sb.efi
}
```

## 首次启动与身份

首次网络启动时固件先完成平台密钥写入（`sbenroll`，窗口开启才执行），随后：

1. 执行 `akgen`，在 TPM 内创建 AK（私钥不可导出）；
2. 通过 `akpub` 导出 AK 公钥、名称与算法；
3. 向控制面 `/devices/report` 上报指纹、AK 与 Secure Boot 状态；
4. 每次引导经 `/devices/challenge` 取得 nonce，用 `quote` 对 PCR 0/2/4/7 生成证明；
5. 经 4443 取回引导变量，只信任构建时固定的 boot CA。

设备必须上报 `sb=1` 才能入池；Secure Boot 关闭时，注册期上报不写台账，只留拒绝审计。

身份语义：

- 设备身份由 TPM 内的 AK 承载；引导 HTTPS 的信任锚是内嵌在签名固件中的 boot CA；更新固件或替换 TFTP 文件不会清除 TPM 中的 AK。
- 清除 TPM 或更换主板会失去原 AK，等同于更换设备身份，需要删除登记并在注册窗口内重新入池。
- 平台密钥（PK / KEK / db）由平台注册窗口在首次网络启动时写入，窗口关闭后 PK 固定；重装或更换固件后重新经该窗口完成写入，无需逐机导入证书。
- boot CA 只签发 4443 叶证书：轮换需重新构建签名产物，平台密钥不随 boot CA 变化。
- 更换硬件或整机替换即形成一台新设备；系统盘与 Worker 身份仍保留在控制面，通过换绑完成迁移。

NVRAM 清理应通过固件可交互环境或受控预置流程完成。AK 生命周期、quote 格式与信任边界见[固件契约](../reference/firmware-contract.md)。

## 验证

| 检查 | 预期 |
| --- | --- |
| 设备启动 | 进入 iPXE 菜单，能看到该 Worker 的系统盘 |
| WebUI → Devices 平台注册 | 列出已完成密钥写入的 MAC |
| WebUI → Devices | 设备在注册窗口内入池 |
| WebUI → 引导链 / `/boot-assets` | 固件 `status=ok`，能力齐全；`secureboot.status=ok` |
| 控制面日志 / 审计 | `/devices/report`、`/devices/challenge`、`/boot-vars` 有对应记录 |
| 数据面 | 选择默认盘后可完成 NVMe/TCP 连接并进入系统 |

`/boot-assets` 中 `status=ok` 且 TPM 身份与 quote 能力齐全才满足引导链要求。`incomplete` 通常表示 TFTP 根里放的是 stock iPXE。`secureboot.status` 不是 `ok` 时，按镜像哈希、端口与 boot CA 三项分别核对。

QEMU/OVMF 验证需要 KVM、OVMF 固件与可用的 nvmet target。固件仓库提供脚本：

```bash
sudo AUTH=1 bash test/nvmet-setup.sh
python3 test/cred-server.py &
# 测试脚本：test/nvmeof-auth-test.ipxe
bash test/run-qemu-auth.sh
```

判据：

- 串口出现认证成功、Property Set、I/O 队列与 GRUB 输出；
- target 端出现固件段与内核段两条控制器记录；
- 抓包中各客户端 TCP 流首字节为 `0x16`，明文端口包数为 0，载荷中无 PSK 或 DH-HMAC-CHAP 密钥明文。

`test/cred-server.py` 仅用于验证，不可作为生产凭据端点。

## 更新与回滚

- 替换 `tftp/snponly.efi` 并重启 dnsmasq 后，PXE 首跳固件即完成更新；
- 本地介质设备需要重新写入新的 `direct-uefi` 产物；
- 保留上一份产物用于回滚；NVRAM 身份与指纹不随固件文件变化；
- 更新后至少验证一台设备完成菜单与一次系统启动。

## 常见失败

- **无 DHCP 应答**：设备是否在 PXE 网段；dnsmasq `interface=` 与防火墙 67/69。
- **DHCP 后无 TFTP 下载**：TFTP 根是否只挂载了首跳固件（`snponly.efi` / `ipxe.efi`）与 `boot.ipxe`；文件权限。
- **卡在固件启动**：产物未签名、签名证书不在目标机 `db`，或 Secure Boot 未开启；用 `sbverify --cert` 与 `tools/secureboot/check-host.sh` 复核，并确认产物为对应架构与形态。
- **引导 HTTPS 证书错误**：固件构建用的 `BOOT_CA_CERT` 与控制面 `state/pki/ca/boot/ca.crt` 不一致；或 4443 叶证书的 SAN 不覆盖设备访问的地址、端口与 `BOOT_HTTPS_PORT` 不一致。
- **设备不入池**：注册窗口是否开启；上报是否携带有效 TPM AK；设备是否被吊销。
- **菜单为空或启动循环**：设备是否绑定 Worker；Worker 是否有系统盘与默认盘。
- **数据面连接失败**：`tlshd`、内核 TLS 配置、4421 端口与凭据推送状态。

## 接下来

- 返回[固件概述](./index.md)
- 构建产物与签名：[构建](./build.md)
- 信任库预置：[Secure Boot 预置](./secure-boot.md)
- 引导链准入：[引导链](../concepts/boot-chain.md)
- 故障排查：[故障排查](../tasks/troubleshooting.md)
