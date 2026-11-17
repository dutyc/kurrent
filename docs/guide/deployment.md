# 部署

本页带你从空白主机走到设备冷启动登录。部署单元是**控制面服务器 + 存储节点**：两者可以分开部署，也可以在同一台机器上；每台存储服务器运行一个 Agent。

**完成判据**：设备冷启动到登录提示符。

参考：[配置参考](../reference/configuration.md)（声明）、[CLI 参考](../reference/cli.md)（命令）、[架构总览](../architecture/index.md)（端口）、[安全模型](../architecture/security.md)（信任边界）。

## 部署准备

开始之前，确认四件事：

- 主机与设备满足[部署前置](../reference/host-prerequisites.md)：x86_64 UEFI、TPM 2.0、Secure Boot、SNP 网卡；
- PXE 网段独立，控制面是该网段唯一的 DHCP/TFTP/4443 提供者；
- 仓库、`kurrent` CLI 与 WebUI 构建依赖就绪（安装见[CLI 参考](../reference/cli.md#安装与凭据)）；
- 主仓与固件仓取 `main` 分支；无 TPM 设备取两侧的 `legacy/no-tpm`（见[仓库与分支](../reference/branches.md)）。

以下命令在主仓库根执行；首次获取：

```bash
git clone https://github.com/dutyc/kurrent && cd kurrent
```

同机部署共用一个状态根；默认 `~/.local/state/kurrent`，备份见[日常运维](./operations.md#备份与恢复)。

### 网络

三条路径各走各的端口：

| 路径 | 内容 | 端口 |
| --- | --- | --- |
| 管理面 | 浏览器、存储节点 | 443 |
| 引导面 | DHCP、TFTP、引导 HTTPS（`spec.networking.interface` 绑定） | 67、69、4443 |
| 数据面 | 设备访问存储节点 | 4421 |

示例：PXE `192.168.80.0/24`，控制面 `192.168.80.1`，池 `.50–.100`，网关 `.2`，DNS `223.5.5.5`；管理 `192.168.1.10`，节点 `192.168.1.20`。

完整端口表见[架构总览](../architecture/index.md#端口)。

### 主机依赖

控制面与节点都需要：Docker、Compose v2、`git`、`curl`、`jq`、`openssl`，以及构建 CLI 的 Go（1.27+，或允许 `toolchain` 自动下载）；控制面另需 Node.js 22.23.4+、24.22.1+ 或 26.12.1+ 与 npm。

```bash
sudo apt install -y git curl jq openssl
docker --version && docker compose version && go version && node --version && npm --version
```

Docker/Compose 与 Node.js 不在上面的 apt 命令内，需预先按发行版方式安装。

存储节点执行宿主准备并挂载数据盘（`mkfs` 只对空盘执行）：

```bash
sudo bash storager/nvmeof/host-prepare.sh
kurrent host check --role storage

sudo mkfs.btrfs /dev/sdb
sudo mkdir -p /pool1 && sudo mount /dev/sdb /pool1
```

数据盘要写入 `/etc/fstab`（UUID）或 systemd mount，否则重启后 `diskDir` 会落在根文件系统上。

## 第 1 步：控制面服务器

### 1.1 声明

先生成一份默认声明，再填入实际网络：

```bash
kurrent config print init-defaults > control_plane/kurrent.yaml
```

```yaml
apiVersion: kurrent.io/v1
kind: ControlPlaneConfiguration
metadata:
  name: kurrent-cp
spec:
  networking:
    interface: enp3s0
    subnet: 192.168.80.0/24
    dhcpRange: 192.168.80.50,192.168.80.100
    gateway: 192.168.80.2
    dns: 223.5.5.5
```

- `interface` 填 PXE 接口，不是管理网口。
- `dhcpRange` 是单个 `start,end` 字符串。

如果要用管理 IP 访问 WebUI，先把该地址加进服务器证书的 SAN（证书覆盖的地址列表），否则浏览器会报证书不匹配：

```yaml
  serverCert:
    san: "IP:192.168.1.10,IP:192.168.80.1,IP:127.0.0.1,DNS:localhost"
```

```bash
kurrent config validate && kurrent config show
```

### 1.2 启动

WebUI 需要先构建出静态产物，再由 `kurrent init` 一并拉起：

```bash
(cd webui/app && npm ci && npm run build && test -f dist/index.html)
kurrent init
```

启动失败会自动回滚到上一份声明；凭据交换失败时重跑即可续做。完整流程见[CLI 参考](../reference/cli.md#控制面初始化)。

### 1.3 验证

```bash
STATE=${KURRENT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/kurrent}
curl -k https://127.0.0.1/healthz
curl -ks https://127.0.0.1/ | grep -q '<div id="root">'
kurrent config status
```

浏览器打开 `https://<控制面管理地址>/`，用 `kurrent token create --usage admin` 签发的 token 登录；浏览器 mTLS 导入见[日常运维](./operations.md#管理入口)。

CLI 凭据由 `kurrent init` 安装；轮换后重跑 `kurrent init` 或 `kurrent credentials install`。

后续步骤的 API 调用沿用下面这个辅助函数：

```bash
STATE=${KURRENT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/kurrent}
P=$STATE/control-plane/state
api() { curl -sS --cert "$P/pki/users/admin/client.crt" --key "$P/pki/users/admin/client.key" \
  --cacert "$STATE/control-plane/state/certs/server.crt" "https://<控制面管理地址>/api/cp$1" "${@:2}"; }
```

### 1.4 防火墙与宿主基线

```bash
sudo ufw allow 67,69/udp && sudo ufw allow 80,443/tcp && sudo ufw reload
sudo ufw allow 53 && sudo ufw reload    # 仅当 spec.networking.dns 指向控制面
sudo kurrent host check
```

4443 是 Docker 发布端口，ufw 无法拦截；按[安全检查清单](#安全检查清单)在网络层只放通引导网段。

### 1.5 签发节点 token

存储节点加入时需要一个一次性 token：

```bash
kurrent token create --agent-id 3f2a9c1d8b7e --cp-url https://192.168.1.10
```

- `--agent-id` 为 12 位小写 hex；省略时由控制面分配。
- token 默认一次性、60 分钟有效；批量加入用 `--uses N`。

nvmet-host 凭据由 agent enroll 自动派生。命令输出即下一步的 join 命令。

## 第 2 步：存储节点

### 2.1 声明

```bash
kurrent config print node-defaults > storager/kurrent.yaml
```

```yaml
apiVersion: kurrent.io/v1
kind: NodeConfiguration
spec:
  agent:
    backend: nvmet
    advertiseUrl: https://192.168.1.20:4840
    diskDir: /pool1/storager_img
    nqnBase: nqn.2026-07.com.kurrent
  controlPlane:
    url: https://192.168.1.10
```

- `advertiseUrl` 是控制面访问 Agent 的 4840 地址；分机部署必须写节点管理地址。
- `diskDir` 是已挂载数据盘的绝对路径。
- `nqnBase` 在集群内保持一致。

同机部署（控制面与存储节点同一宿主）时，控制面运行在容器内，`advertiseUrl` 要写 `https://host.docker.internal:4840`（compose 已把它指向宿主）；`storager_ip` 在 WebUI Agents 详情设为 PXE 网段地址（如 `192.168.80.1`），供设备访问 4421；宿主基线用 `kurrent host check --role all`。

身份不在声明里，join 写入 `storager/state/agent.id`。

### 2.2 加入

```bash
kurrent join https://192.168.1.10 --token <token> --agent-id 3f2a9c1d8b7e
```

分机部署首次加入时，节点还没有控制面的信任锚，需要先把控制面服务器证书复制到节点。`<state>` 换成控制面的实际状态根（默认 `~/.local/state/kurrent`），远端路径用绝对路径：

```bash
scp admin@192.168.1.10:/home/admin/.local/state/kurrent/control-plane/state/certs/server.crt /tmp/cp-ca.crt
kurrent join https://192.168.1.10 --token <token> --agent-id 3f2a9c1d8b7e --cp-ca /tmp/cp-ca.crt
```

校验失败不写盘，重跑幂等；流程见[CLI 参考](../reference/cli.md#节点加入)。

### 2.3 验证

```bash
docker logs storager-agent 2>&1 | grep pki
docker exec kurrent-nvmet-host python /app/healthcheck.py; echo $?
```

预期看到 `pki: client cert ok`，healthcheck 返回 0。再从控制面确认节点健康：

```bash
kurrent agents list    # HEALTH=ok
```

注意 `advertiseUrl`（控制面 → 4840）与 `storager_ip`（设备 → 4421）是两个地址；数据面走独立网段时，后者在 WebUI Agents 详情设置。

存储节点防火墙：

```bash
sudo ufw allow 4840,4421/tcp && sudo ufw reload
```

## 第 3 步：固件与设备入池

这一步之前先完成 Secure Boot 预置：按 [Secure Boot 预置](../firmware/secure-boot.md#预置目标机信任库)生成离线签名密钥，把站点签名证书与 Microsoft UEFI CA 导入目标机 `db`，并用 `tools/secureboot/check-host.sh` 复核。boot CA 由 `kurrent init` 生成（`<状态根>/control-plane/state/pki/ca/boot/ca.crt`）。

### 3.1 固件

在固件仓构建签名首跳，再把产物放进主仓的 TFTP 根：

```bash
# 固件仓：boot CA 取自控制面状态根
SB_KEY=/path/to/artifact-signing.key SB_CERT=/path/to/artifact-signing.crt \
BOOT_CA_CERT=<状态根>/control-plane/state/pki/ca/boot/ca.crt \
BOOT_HTTPS_PORT=4443 ./build/build-sb.sh

# 主仓库：产物供 /boot-assets 校验，并作为 TFTP 首跳
sudo install -d -m 0755 /path/to/kurrent/tftp/secureboot
sudo install -m 0644 dist/secureboot/snponly-sb.efi dist/secureboot/manifest.json /path/to/kurrent/tftp/secureboot/
sudo install -m 0644 /path/to/kurrent/tftp/secureboot/snponly-sb.efi /path/to/kurrent/tftp/snponly.efi
cd /path/to/kurrent
eval "$(kurrent deployment show -o env)"
docker compose restart kurrent-dnsmasq
```

首跳固定为 `snponly.efi`，替换后必须重启 dnsmasq。镜像内嵌 boot CA 与 4443 端口，换 CA 或端口必须重签；控制面地址默认由 DHCP 提供。完整构建与预置见 [Secure Boot 预置](../firmware/secure-boot.md)。

```bash
api /boot-assets | jq '{firmware, secureboot}'   # secureboot.status=ok
```

### 3.2 入池

在 WebUI 的 **Devices** 页开启注册窗口，然后让设备接入 PXE 网段、以 UEFI 网络启动并开启 Secure Boot；也可以用 API：

```bash
api /settings/registration-window -X POST -H 'Content-Type: application/json' \
  -d '{"ttl_minutes": 30, "max_devices": 20}'
docker logs -f kurrent-dnsmasq 2>&1 | grep -E "DHCPACK|pxe|ipxe"
```

设备走过的链路是：DHCP → TFTP 首跳 → 4443 上报并取 nonce → TPM 创建 AK → `/devices/report` 入池并认领 AK；此后每次引导都需通过 PCR quote。

入池完成后关闭注册窗口：

```bash
api /settings/registration-window -X DELETE
```

仅在过渡或排障时临时关闭证明：`api /settings/enforcement -X PUT -H 'Content-Type: application/json' -d '{"enabled": false}'`，完成后恢复。

## 第 4 步：系统盘与首次启动

以下命令在控制面执行，沿用 1.3 的 `api`；各步也可在 WebUI 完成。

### 4.1 母盘

母盘必须是整盘镜像（GPT、ESP 与 root 分区），文件名含 `_tpl_`，放入存储节点 `diskDir`；分区镜像、压缩包或安装介质都不能直接使用。制作见[系统盘定制](./system-disk.md)。

```bash
# 构建机：qcow2 转整盘 raw（已是 raw 可跳过）
qemu-img convert -O raw master.qcow2 _tpl_ubuntu2404.img
scp _tpl_ubuntu2404.img admin@192.168.1.20:/pool1/storager_img/
```

```bash
api /masters | jq '.agents[] | {agent, masters: [.masters[] | {name, gpt}]}'
```

`gpt` 必须为 `4k`，否则引导报 `0x7f22208e`。

### 4.2 绑定 Worker

在 WebUI Devices 页绑定，或用 API 创建 Worker 并绑定设备：

```bash
api /workers -X POST -H 'Content-Type: application/json' \
  -d '{"worker_id":"worker-01","hostname":"worker-01","mac":"00:0c:29:b9:8b:2d"}'
```

### 4.3 创建系统盘

```bash
api /workers/worker-01/luns/disk -X POST -H 'Content-Type: application/json' \
  -d '{"type":"master","os":"ubuntu","os_version":"24.04","name":"_tpl_ubuntu2404.img"}'
```

放置由控制面决定；响应含 `os_tag` 与盘 NQN；首块盘自动设为默认盘。

### 4.4 切换默认盘（可选）

首块盘已自动设为默认；一台 Worker 有多块盘时再指定：

```bash
api /workers/worker-01/default-disk -X PUT -H 'Content-Type: application/json' \
  -d '{"disk":"<os_tag>"}'
```

未设置默认盘时 `/boot-vars` 返回 `menu-default=reboot`。

### 4.5 冷启动验收

给设备加电，完整链路应当是：

```text
DHCP → iPXE → /boot-vars → NVMe/TCP TLS 1.3 → GRUB → 内核 → initramfs（NBFT/NBCT）→ rootfs → 登录
```

菜单为空、数据面连不上、认证失败或报 `0x7f22208e` 时见[故障排查](./troubleshooting.md)；`0x7f22208e` 先查 `/masters` 的 `gpt` 是否为 `4k`。

## 安全检查清单

| 检查项 | 要求 |
| --- | --- |
| 网络与交换 | PXE 网段独立，控制面是唯一 DHCP/TFTP 提供者；TFTP 根只挂 `snponly.efi` 与 `boot.ipxe`；交换机启用 DHCP snooping、DAI、RA guard 与端口安全 |
| 对外端口 | 控制面 22、53/67/69、80/443/4443（4443 仅引导网段）；存储节点 4840/4421 |
| 宿主基线 | `sudo kurrent host check --strict` 无 FAIL、无 SKIP；CA 私钥、`admin.conf`、凭据与审计文件为 600 |
| 注册与证明 | 注册窗口按需开启并设额度，用后关闭；TPM 证明默认开启；无 TPM 设备不入池 |
| Secure Boot 与产物 | 目标机 `db` 预置站点签名证书与 Microsoft UEFI CA；产物来自 `build-sb.sh` 签名构建，`/boot-assets` 的 `secureboot.status` 为 `ok` |
| 证书与审计 | 人员证书按需签发、离职或泄露立即吊销；定期检查 CRL、凭据推送与审计告警 |
| 备份与固件 | 备份加密并验证恢复；固件产物校验 `SHA256SUMS` |