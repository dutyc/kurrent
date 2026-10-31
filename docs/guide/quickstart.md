# 快速开始

本页给出最短可运行路径：在 Linux 上拉起控制面与存储节点，放入设备固件，让第一台设备具备网络启动条件。

> 单机部署只用于功能验证。生产环境应把控制面与存储节点分离，并按[部署指南](./deployment.md)完成网络、防火墙与宿主基线。

## 0. 准备

- x86_64 Linux 主机，已安装 Docker 与 Compose v2。
- 一块用于 PXE 的网卡与独立网段（示例 `192.168.80.0/24`，控制面地址 `192.168.80.2`）。
- 存储目录所在文件系统支持 reflink（btrfs 或 xfs）。
- 仓库根目录可写；后续命令都在仓库根执行。

```bash
git clone https://github.com/dutyc/kurrent
cd kurrent
```

## 1. 宿主前置

```bash
# nvmet 内核模块、configfs、tlshd 版本与重启持久化（幂等，需 root）
sudo bash storager/nvmeof/host-prepare.sh

# 只读诊断
bash storager/nvmeof/host-prepare.sh --check

# 内核需带 NVMe/TCP TLS 配置；tlshd 需 >= 1.5.0 且运行中
grep -E 'CONFIG_NVME_(TCP_TLS|TARGET_TCP_TLS|KEYRING)' /boot/config-$(uname -r)
sudo apt install -y ktls-utils
sudo systemctl enable --now tlshd

# 宿主对外暴露面检查（只读）
bash control_plane/host-baseline.sh
```

盘目录示例：

```bash
sudo mkfs.btrfs /dev/sdb
sudo mkdir -p /pool1
sudo mount /dev/sdb /pool1
```

## 2. 安装 CLI

```bash
# 预编译二进制（Linux amd64；arm64 与 Windows 见 Releases）
curl -sL -o cli/kurrent https://github.com/dutyc/kurrent/releases/download/v0.3.0/kurrent-linux-amd64
chmod +x cli/kurrent
sudo install -m 0755 cli/kurrent /usr/local/bin/kurrent

# 或源码构建
# cd cli && go build -o kurrent .
```

## 3. 控制面

生成控制面声明，修改 `spec.networking` 五键后收敛启动：

```bash
kurrent config print init-defaults > control_plane/kurrent.yaml
$EDITOR control_plane/kurrent.yaml

kurrent config validate        # 写盘前全量校验
kurrent init                   # 启动并等待健康检查；失败自动回滚
kurrent config status          # 生效声明、configHash、派生件与证书漂移
```

验证：

```bash
curl -k https://127.0.0.1/healthz
```

WebUI 在 `https://<控制面地址>/`。首次登录可用管理 token：

```bash
kurrent token create --usage admin
```

## 4. 存储节点

先在控制面签发绑定节点身份的引导凭据：

```bash
kurrent token create --agent-id node-01 --cp-url https://192.168.80.2
```

在存储节点（同机验证时即本机）执行输出中的命令：

```bash
kurrent join https://192.168.80.2 --token <bootstrap-token> --agent-id node-01
```

`kurrent join` 会生成节点声明、写入引导凭据、同步 `.env` 并启动 agent 与 nvmet-host；写盘前会先用组件镜像内的同一份 schema 校验声明。

验证：

```bash
kurrent agents list
# ID       REMARK  HEALTH  BASE_URL                         ROLE  ENABLED  TAGS
# node-01          ok      https://192.168.80.2:4840        disk  true     auto,nvmet,storage
```

> 单机且控制面以 bridge 网络运行、`HEALTH` 不是 `ok` 时，把 `storager/kurrent.yaml` 的 `spec.agent.advertiseUrl` 改为 `https://host.docker.internal:4840`，再执行一次 `kurrent join https://192.168.80.2`。

## 5. 设备固件

主仓库不包含固件二进制。从固件仓库获取与上游基线匹配的产物，放入 `tftp/`：

```bash
mkdir -p /tmp/kurrent-fw/pxe-uefi && cd /tmp/kurrent-fw
BASE=https://github.com/dutyc/kurrent-firmware/releases/latest/download
curl -sLO "$BASE/SHA256SUMS"
curl -sL -o pxe-uefi/snponly.efi "$BASE/pxe-uefi-snponly.efi"
sha256sum -c SHA256SUMS --ignore-missing

sudo install -m 0644 pxe-uefi/snponly.efi /path/to/kurrent/tftp/snponly.efi
cd /path/to/kurrent
docker compose restart kurrent-dnsmasq
```

> `tftp/snponly.efi` 是文件级挂载，替换后必须重启 dnsmasq 容器，否则容器仍持有旧 inode。
> 需要自定义网卡驱动或本地介质引导时，按[构建与产物](../firmware/build.md)自行构建。

在 WebUI「引导链」页确认固件状态为可用；接口方式：

```bash
curl -sS \
  --cert control_plane/state/pki/users/admin/client.crt \
  --key control_plane/state/pki/users/admin/client.key \
  --cacert control_plane/state/certs/server.crt \
  https://192.168.80.2/api/cp/boot-assets | jq '.firmware'
```

## 6. 第一台设备

1. 设备接入 PXE 网段并设为网络启动。
2. WebUI → **Devices** → 开启注册窗口（默认上限 60 分钟，可按声明调整）。
3. 设备出现在池中后，绑定到 Worker；没有 Worker 时先在 Workers 页创建。
4. 在 Worker 上创建系统盘：选择母盘与 `os` / `os_version`；nvmet 后端会同时签发 DHHC-1 凭据。
5. 将系统盘设为默认盘，重启设备。

完整操作与验证见[使用流程](./usage.md)。

## 常见失败

| 现象 | 排查 |
| --- | --- |
| `kurrent agents list` 非 `ok` | 控制面是否能访问 `advertiseUrl`；存储节点 4840 是否放通；`docker logs storager-agent` |
| 设备 DHCP 无应答 | `dnsmasq.conf` 的 `interface=` 是否对应真实网卡；67/69 UDP 是否放通 |
| 设备未入池 | 注册窗口是否开启；上报是否带有效公钥；`/devices/report` 日志与审计 |
| `/boot-assets` 固件能力缺失 | 使用固件仓库产物而非 stock iPXE；确认包含 HTTPS 引导、NVS 持久化、设备密钥生成、公钥导出、挑战签名、TOFU 锚定六项能力 |
| 数据面连接失败 | `tlshd` 版本与运行状态；内核 TLS 编译项；nvmet-host 日志中的 `4421` 建口结果 |

其余问题见[部署指南的故障排查](./deployment.md#故障排查)。
