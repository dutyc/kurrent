# 构建与产物

固件仓库不保存 iPXE 源码，构建时拉取固定上游基线并应用补丁集。这样可以在上游安全更新或新硬件支持出现时，重新建立一条可复现的构建链。

## 环境

- Linux x86_64（构建机，不需要与设备同架构之外的特殊环境）。
- `git`、`make`、`gcc`；iPXE 构建过程中的其他依赖由上游 Makefile 检查。
- 可访问上游 iPXE 仓库；首次构建会把源码缓存到 `.cache/ipxe-upstream`。

## 构建

```bash
git clone https://github.com/dutyc/kurrent-firmware
cd kurrent-firmware
./build/build.sh
```

流水线：

1. 拉取上游 iPXE 并检出固定基线（当前 `e6e51ccb`）。
2. 按文件名顺序应用 `patches/*.patch`；任一补丁无法应用即失败。
3. 安装 `embed/auto.ipxe` 作为 `direct-uefi` 的内置脚本。
4. 构建 UEFI 目标，复制到 `dist/`。
5. 生成 `dist/SHA256SUMS`。

可选参数：

```bash
UPSTREAM_COMMIT=<sha> ./build/build.sh   # 指定上游基线（补丁须兼容）
JOBS=8 ./build/build.sh                  # 并行度
```

补丁基线与升级流程见固件仓库的 `patches/README`。升级上游前，需要逐个确认补丁仍能应用，并重新跑 QEMU 与整机验证。

## 产物

| 产物 | 形态 | 用途 |
| --- | --- | --- |
| `dist/pxe-uefi/ipxe.efi` | PXE 网络启动（UEFI，native 驱动） | 主板 PXE 链加载，脚本由 DHCP 下发 |
| `dist/pxe-uefi/ipxe-debug.efi` | 同上，debug | 故障定位 |
| `dist/pxe-uefi/snponly.efi` | PXE 网络启动（SNP 专用） | native 驱动不可用时的首跳 |
| `dist/pxe-uefi/snponly-debug.efi` | 同上，debug | 故障定位 |
| `dist/direct-uefi/ipxe.efi` | UEFI 直接引导（内置脚本） | USB/ESP、本地磁盘或 GRUB 链加载 |
| `dist/direct-uefi/ipxe-debug.efi` | 同上，debug | 故障定位 |
| `dist/direct-uefi/snponly.efi` | UEFI 直接引导（SNP 专用，内置脚本） | 无 native 驱动时的本地介质引导 |

debug 产物启用 `DEBUG=realtek:3`，输出更多网卡初始化日志。

### 怎么选

| 场景 | 选择 |
| --- | --- |
| 主板支持 PXE / UEFI Network Boot | `pxe-uefi/snponly.efi`（默认首跳） |
| 主板无 PXE 入口，需 USB/ESP 引导 | `direct-uefi/ipxe.efi` |
| native 驱动不可用，需要固件 SNP 栈 | `pxe-uefi/snponly.efi` 或 `direct-uefi/snponly.efi` |
| 网卡初始化失败、需要定位 | 对应 `-debug` 产物 |

主仓库默认使用 `tftp/snponly.efi`。除非需要自定义驱动或本地介质引导，不必替换为完整的 `ipxe.efi`。

## 校验

```bash
cd dist
sha256sum -c SHA256SUMS
```

发布页的资产是扁平命名（如 `pxe-uefi-snponly.efi`），但校验文件内的路径保留 `pxe-uefi/` 目录结构。下载后按目录放置再校验，或只取目标文件比对摘要。

## 放入平台仓库

```bash
sudo install -m 0644 dist/pxe-uefi/snponly.efi /path/to/kurrent/tftp/snponly.efi
cd /path/to/kurrent
docker compose restart kurrent-dnsmasq
```

`tftp/snponly.efi` 是文件级挂载，替换后必须重启 dnsmasq 容器，否则容器仍持有旧 inode。

验证固件能力：

```bash
curl -sS \
  --cert control_plane/state/pki/users/admin/client.crt \
  --key control_plane/state/pki/users/admin/client.key \
  --cacert control_plane/state/certs/server.crt \
  https://<控制面IP>/api/cp/boot-assets | jq '.firmware'
```

`status=ok` 且六项能力齐全（HTTPS 引导、NVS 持久化、设备密钥生成、公钥导出、挑战签名、TOFU 锚定）才满足引导链要求。`incomplete` 表示固件存在但缺少能力，通常是把 stock iPXE 放进了 TFTP 根。

## 更新与回滚

- **更新**：替换 `tftp/snponly.efi` 并重启 dnsmasq。设备 NVRAM 中的身份密钥与服务器指纹不受影响，除非显式清除。
- **回滚**：保留上一份产物，按同样方式替换回退。
- **验证**：重新引导一台测试设备，确认能进入菜单并完成一次系统启动；`/boot-assets` 能力完整。

## 测试

固件仓库提供 QEMU 验证脚本与整机链路记录：

```bash
sudo bash test/nvmet-setup.sh        # 本机 NVMe/TCP target
python3 test/cred-server.py 8000     # 认证凭据端点
bash test/run-qemu-auth.sh           # 一轮认证引导验证
```

测试细节、pcap 分析与各发行版验证记录以固件仓库文档为准。

## 许可

固件仓库整体遵循 GPL-2.0，与上游 iPXE 的双许可兼容；部分第三方驱动适配仅按 GPL-2.0 授权。再分发前阅读 `patches/README`。

## 相关页面

- [固件概述](./index.md)
- [预置与刷写](./provision.md)
- [固件契约](../reference/firmware-contract.md)
- 固件仓库构建文档：<https://github.com/dutyc/kurrent-firmware/blob/main/docs/build-artifacts.zh-CN.md>
