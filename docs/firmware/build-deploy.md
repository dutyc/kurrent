# 固件构建与部署

本页是一次完整任务：从固件源码构建产物，选择启动形态，部署到设备，并验证首次启动与身份登记。固件属于独立仓库 `kurrent-firmware`，但部署结果进入主仓库的 TFTP 根与控制面引导链。

## 目标与前提

完成后应满足：

- `tftp/snponly.efi` 来自本次构建，dnsmasq 已重启并加载新文件；
- 设备完成一次 PXE 或本地介质引导，进入 iPXE 菜单；
- 设备在注册窗口内上报 TPM AK 公钥并出现在 Devices 台账；
- `/boot-assets` 报告固件能力完整。

前提：

- Linux x86_64 构建机，具备 `git`、`make`、`gcc`；
- 能访问上游 iPXE 仓库，首次构建会缓存源码；
- 目标设备为 x86_64 UEFI，开启 Secure Boot，且 `db` 已导入站点签名证书；传统 BIOS（Legacy/CSM）引导不再支持；
- 已按[部署](../guide/deployment.md)完成控制面、dnsmasq 与 PXE 网段。

## 1. 构建

```bash
git clone https://github.com/dutyc/kurrent-firmware
cd kurrent-firmware
./build/build.sh
```

构建流水线：

1. 拉取上游 iPXE 并检出固定基线（当前 `e6e51ccb`）。
2. 按文件名顺序应用 `patches/*.patch`；任一补丁无法应用即失败。
3. 安装 `embed/auto.ipxe` 作为 `direct-uefi` 的内置脚本。
4. 构建 UEFI 目标，复制到 `dist/`。
5. 生成 `dist/SHA256SUMS`。

可选参数：

```bash
UPSTREAM_COMMIT=<sha> ./build/build.sh   # 指定上游基线，补丁须兼容
JOBS=8 ./build/build.sh                  # 并行度
```

补丁基线与升级流程见固件仓库 `patches/README`。升级上游前需逐个确认补丁仍可应用，并重新执行 QEMU 与整机验证。

部署使用 `build-sb.sh` 产出的签名产物 `dist/secureboot/snponly-sb.efi`；`build.sh` 的未签名产物在开启 Secure Boot 的目标机上会被固件拒绝。签名的输入、产物与预置步骤见 [Secure Boot 引导](./secure-boot.md)。

校验产物：

```bash
cd dist
sha256sum -c SHA256SUMS
```

发布页资产使用扁平命名（如 `pxe-uefi-snponly.efi`），校验文件内保留 `pxe-uefi/` 目录结构；下载后按目录放置再校验，或只比对目标文件摘要。

## 2. 选择产物

| 产物 | 形态 | 用途 |
| --- | --- | --- |
| `pxe-uefi/ipxe.efi` | PXE 网络启动 | 主板 PXE 链加载，脚本由 DHCP 下发 |
| `pxe-uefi/snponly.efi` | PXE 网络启动 | 默认首跳 |
| `pxe-uefi/ipxe-debug.efi`、`snponly-debug.efi` | 同上，debug | 网卡初始化与引导定位 |
| `direct-uefi/ipxe.efi` | UEFI 直接引导，内置脚本 | USB/ESP、本地磁盘或 GRUB 链加载 |
| `direct-uefi/ipxe-debug.efi` | 同上，debug | 故障定位 |
| `direct-uefi/snponly.efi` | UEFI 直接引导 | 本地介质引导 |

选择原则：

| 场景 | 选择 |
| --- | --- |
| 主板支持 UEFI Network Boot | `pxe-uefi/snponly.efi`，作为默认首跳 |
| 主板无 PXE 入口，需 USB/ESP 引导 | `direct-uefi/ipxe.efi` |
| 网卡初始化失败 | 对应的 `-debug` 产物 |

主仓库默认使用 `tftp/snponly.efi`。不同机型可能需要不同的首跳产物，而主线当前只下发 `snponly.efi`，不支持按设备切换；需要 `pxe-uefi/ipxe.efi` 的机器无法在同一 PXE 网段与默认首跳混用，部署前需统一首跳产物。

上表是 `build.sh` 的形态；部署时首跳换成 `build-sb.sh` 的 `dist/secureboot/snponly-sb.efi`，形态与 `pxe-uefi/snponly.efi` 相同。

## 3. 部署到设备

| 条件 | 路径 |
| --- | --- |
| 主板有 UEFI Network Boot / PXE 入口 | PXE，使用平台 TFTP 提供的 `snponly.efi` |
| 主板无 PXE 入口，或 PXE 设置不可用 | 本地介质（USB / ESP / 本地磁盘 / GRUB 链加载） |
| 需要批量预置且 BMC 支持虚拟介质 | BMC / Redfish 挂载介质，可选 |

### PXE 路径

1. 取得 `dist/secureboot/snponly-sb.efi` 与 `manifest.json`。
2. 安装到主仓库 TFTP 根并重启 dnsmasq：

   ```bash
   sudo install -d -m 0755 /path/to/kurrent/tftp/secureboot
   sudo install -m 0644 dist/secureboot/snponly-sb.efi dist/secureboot/manifest.json \
     /path/to/kurrent/tftp/secureboot/
   sudo install -m 0644 dist/secureboot/snponly-sb.efi /path/to/kurrent/tftp/snponly.efi
   cd /path/to/kurrent
   eval "$(kurrent deployment show -o env)"
   docker compose restart kurrent-dnsmasq
   ```

   `tftp/snponly.efi` 是文件级挂载，替换后必须重启容器，否则容器仍持有旧 inode。`tftp/secureboot/` 留作 `/boot-assets` 的产物核对与导出源。

3. 在设备 UEFI 设置中启用网络栈与 Secure Boot，将网络启动置于启动顺序首位。
4. 启动设备，确认 DHCP 分配地址、TFTP 下载首跳、首跳经 4443 完成上报与挑战。

TFTP 只交付首跳 `snponly.efi`（与 `boot.ipxe` 指针）；签名首跳内嵌静态脚本、boot CA 与控制面地址，后续请求走 4443 HTTPS，只信任该 boot CA。余下的 TLS 1.3 数据面 `sanboot` 与内核段保持既有凭据链。PXE 网段必须按[安全模型](../architecture/security.md)隔离。需要 `pxe-uefi/ipxe.efi` 的机器不能在同一 PXE 网段与默认首跳混用。

### 本地介质路径

使用 `direct-uefi` 产物：它内置 `auto.ipxe`，启动后执行 DHCP 并跳转到控制面的 TFTP 引导链。

USB / ESP：

1. 准备 FAT32 分区（U 盘或本地 ESP）。
2. 创建目录 `EFI/BOOT/`。
3. 将 `dist/direct-uefi/ipxe.efi` 复制为 `EFI/BOOT/BOOTX64.EFI`。
4. 在设备启动菜单中选择该 USB/ESP。

若主板仅识别固定 ESP，将文件放入内置磁盘的同一路径，并在启动项中选择它。

GRUB 链加载：

```text
menuentry "Kurrent firmware" {
    chainloader /boot/efi/EFI/kurrent/ipxe.efi
}
```

BMC / Redfish 虚拟介质只做过协议级验证，具体型号能力不同；它不是 Kurrent 的部署依赖，也不替代 PXE 网段的隔离要求。

## 4. 首次启动与身份

首次启动时固件会：

1. 执行 `akgen`，在 TPM 内创建 AK（私钥不可导出）；
2. 通过 `akpub` 导出 AK 公钥、名称与算法；
3. 向控制面 `/devices/report` 上报指纹、AK 与 Secure Boot 状态；
4. 每次引导经 `/devices/challenge` 取得 nonce，用 `quote` 对 PCR 0/2/4/7 生成证明；
5. 经 4443 取回引导变量，只信任构建时固定的 boot CA。

设备必须上报 `sb=1` 才能入池；Secure Boot 关闭时注册期上报只记指纹。

身份语义：

- 设备身份由 TPM 内的 AK 承载；引导 HTTPS 的信任锚是烘在签名固件里的 boot CA；更新固件或替换 TFTP 文件不会清除 TPM 中的 AK。
- 清除 TPM 或更换主板会失去原 AK，等同于更换设备身份，需要删除登记并在注册窗口内重新入池。
- boot CA 轮换等于重刷固件：需要重新构建签名产物，并在目标机重新预置 `db` 证书。
- 更换硬件或整机替换即形成一台新设备；系统盘与 Worker 身份仍保留在控制面，通过换绑完成迁移。

NVRAM 清理应通过固件可交互环境或受控预置流程完成。AK 生命周期、quote 格式与信任边界见[固件契约](../reference/firmware-contract.md)。

## 5. 验证

| 检查 | 预期 |
| --- | --- |
| 设备启动 | 进入 iPXE 菜单，能看到该 Worker 的系统盘 |
| WebUI → Devices | 设备在注册窗口内入池 |
| WebUI → 引导链 / `/boot-assets` | 固件 `status=ok`，能力齐全；`secureboot.status=ok` |
| 控制面日志 / 审计 | `/devices/report`、`/devices/challenge`、`/boot-vars` 有对应记录 |
| 数据面 | 选择默认盘后可完成 NVMe/TCP 连接并进入系统 |

`/boot-assets` 中 `status=ok` 且 TPM 身份与 quote 能力齐全才满足引导链要求。`incomplete` 通常表示 TFTP 根里放的是 stock iPXE。`secureboot.status` 不是 `ok` 时，按镜像哈希、端口与 boot CA 三项分别核对。

### QEMU 验证

QEMU/OVMF 验证需要 KVM、OVMF 固件与可用的 nvmet target。固件仓库提供脚本：

```bash
sudo AUTH=1 bash test/nvmet-setup.sh
python3 test/cred-server.py &
# 测试脚本：test/nvmeof-auth-test.ipxe
bash test/run-qemu-auth.sh
```

判据：串口出现认证成功、Property Set、I/O 队列与 GRUB 输出；target 端出现固件段与内核段两条控制器记录；抓包中各客户端 TCP 流首字节为 `0x16`，明文端口包数为 0，载荷中无 PSK 或 DH-HMAC-CHAP 密钥明文。`test/cred-server.py` 仅用于验证，不可作为生产凭据端点。

## 更新与回滚

- 替换 `tftp/snponly.efi` 并重启 dnsmasq 后，PXE 首跳固件即完成更新。
- 本地介质设备需要重新写入新的 `direct-uefi` 产物。
- 保留上一份产物用于回滚；NVRAM 身份与指纹不随固件文件变化。
- 更新后至少验证一台设备完成菜单与一次系统启动。

## 常见失败

- **无 DHCP 应答**：设备是否在 PXE 网段；dnsmasq `interface=` 与防火墙 67/69。
- **DHCP 后无 TFTP 下载**：TFTP 根是否只挂载了 `snponly.efi` 与 `boot.ipxe`；文件权限。
- **卡在固件启动**：产物未签名、签名证书不在目标机 `db`，或 Secure Boot 未开启；用 `sbverify --cert` 与 `tools/secureboot/check-host.sh` 复核，并确认产物为对应架构与形态。
- **引导 HTTPS 证书错误**：固件构建用的 `BOOT_CA_CERT` 与控制面 `state/pki/ca/boot/ca.crt` 不一致；或 4443 叶证书的 SAN 不覆盖设备访问的地址、端口与 `BOOT_HTTPS_PORT` 不一致。
- **设备不入池**：注册窗口是否开启；上报是否携带有效 TPM AK；设备是否被吊销。
- **菜单为空或启动循环**：设备是否绑定 Worker；Worker 是否有系统盘与默认盘。
- **数据面连接失败**：`tlshd`、内核 TLS 配置、4421 端口与凭据推送状态。

## 相关页面

- [固件概述](./index.md)
- [固件平台参考](../reference/firmware-platform.md)
- [固件契约](../reference/firmware-contract.md)
- [引导链](../concepts/boot-chain.md)
- [安全模型](../architecture/security.md)
- [故障排查](../guide/troubleshooting.md)
- 固件仓库构建文档：<https://github.com/dutyc/kurrent-firmware/blob/main/docs/build-artifacts.zh-CN.md>
