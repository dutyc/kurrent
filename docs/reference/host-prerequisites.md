# 部署前置

本页汇总部署前的主机、网络与宿主前提。部署步骤本身见[部署](../guide/deployment.md)；端口表的权威定义在[架构总览](../architecture/index.md#端口)；安全边界与检查清单见[安全模型](../architecture/security.md#部署检查清单)。

## 主机角色

| 角色 | 最低要求 | 职责 |
| --- | --- | --- |
| 控制面服务器（×1） | Linux x86_64；Docker 与 Compose v2；Node.js 18+ 与 npm（构建 WebUI）；一个 PXE 侧接口（独立网卡或 VLAN 子接口） | DHCP/TFTP、443 WebUI/API、PKI、设备台账与引导变量 |
| 存储节点（×1 起） | Linux x86_64；Docker 与 Compose v2；NVMe/TCP TLS 内核能力；`tlshd >= 1.5.0`；一块 btrfs/xfs 数据盘 | Agent（4840）与 nvmet-host（4421），承载母盘与系统盘 |
| 裸机设备（×N） | UEFI x86_64；支持 UEFI 网络启动 | 通过 PXE 入池并启动系统盘 |

控制面与存储节点可以是同一台主机，但角色不合并：同机时仍运行一个 Agent，仍按部署页顺序执行。控制面必须有一个位于 PXE 网段的地址；WebUI、API 与 Agent 回连使用管理网地址，两者不要混用。

裸机设备只支持 UEFI 引导。传统 BIOS（Legacy/CSM）引导不再支持：构建产物只有 UEFI 形态，设备身份、EFI 变量持久化与 NBFT/NBCT 交接都依赖 UEFI 提供的接口。

## 网络与端口

控制面需要两条可达路径：

- **管理面**：管理员浏览器与存储节点访问 443；控制面用这个地址签发 token、访问 Agent。
- **引导面**：裸机设备访问 DHCP（67/UDP）与 TFTP（69/UDP）；`spec.networking.interface` 绑定这一侧，该接口必须持有 PXE 子网内的地址。

推荐用两张网卡或 VLAN 子接口把两侧分开。单网卡同时承载管理面与引导面时，PXE 网段必须独立隔离，不能与办公网混跑。引导客户端必须能路由到存储节点的 4421 数据面端口；跨网段时在路由与防火墙上显式放通。

完整端口表与组件职责见[架构总览](../architecture/index.md#端口)。需要记住的边界：

- 控制面发布 53 UDP/TCP（DNS）、67/69 UDP（DHCP/TFTP）、80 TCP（仅 301 到 443）与 443 TCP；443 是唯一 HTTPS 入口。
- 控制面管理面不发布 TCP 端口，只监听容器内 unix socket，由 nginx 经共享卷访问。
- 443 的引导链只反代 `/boot-vars`、`/devices/report`、`/devices/challenge` 三个端点；其余 `/devices/*` 管理接口不对外暴露。
- 存储节点发布 4840 TCP（Agent HTTPS，控制面 → Agent）与 4421 TCP（NVMe/TCP 数据面，TLS 1.3，固件段与内核段共用）。
- nvmet-host 管理面走共享卷 UDS，不占 TCP。

## 主机依赖

两台主机都需要 Docker、Compose v2、`git`、`curl`、`jq` 与 `openssl`；控制面主机另需 Node.js 18+ 与 npm 构建 WebUI。Docker 与 Compose 按发行版或 Docker 官方安装文档准备；其余工具在 Debian/Ubuntu 上可一次安装：

```bash
sudo apt install -y git curl jq openssl
docker --version && docker compose version && node --version && npm --version
```

`webui/app/dist` 是构建产物，不入库；控制面容器把宿主目录只读挂载到 nginx。首次启动前在仓库根执行 `(cd webui/app && npm ci && npm run build)`，并确认 `dist/index.html` 存在。若先启动过 compose，缺失的 `dist` 会被建成 root 所有的空目录，构建前需 `sudo rm -rf webui/app/dist`。

## 存储节点内核与 tlshd

数据面 NVMe/TCP TLS 依赖宿主内核与 `tlshd`：

- 内核需带 `CONFIG_NVME_TCP_TLS`、`CONFIG_NVME_TARGET_TCP_TLS` 与 `CONFIG_NVME_KEYRING`。
- `tlshd >= 1.5.0` 且服务运行中；低于 1.5.0 会让轮换后的旧 PSK 遮蔽新 PSK，脚本按失败处理。
- 发行版包低于 1.5.0 时，按 `storager/nvmeof/nvmet-host/README.md` 源码安装。
- 数据面 4421 建口失败时，节点如实上报端口未就绪，控制面拒绝引导，不回落明文。

`host-prepare.sh` 需 root，幂等完成内核模块加载、configfs 挂载与重启持久化；只读诊断用 `kurrent host check --role storage`，无需 root。部署命令见[部署](../guide/deployment.md#主机依赖)。

## 数据盘

母盘克隆依赖 reflink：

- btrfs 或 xfs 支持 reflink/FICLONE，克隆接近秒级。
- ext4 不支持 reflink，会回退全量拷贝，克隆时间随镜像大小线性增长。
- 数据盘要写入 `/etc/fstab`（推荐用 `blkid` 取 UUID）或等价的 systemd mount，确保重启后自动挂载；否则 `diskDir` 会落到根文件系统上的空目录。

## 宿主基线

控制面宿主只应暴露 22（运维）、53/67/69（dnsmasq）、80/443（nginx）；存储节点另有 4840/4421。

- nvmet-host 管理面走共享卷 UDS，不产生 TCP 监听。
- 控制面容器不挂载 `docker.sock`。
- CA 私钥、`users.yml`、`sessions.yml`、`admin.conf`、`audit.jsonl` 应为 600。
- ufw 不过滤 Docker 发布端口；需要入站限制时在 `DOCKER-USER` 链收口。
- 宿主 root 或 nginx 进程被攻破等同管理面沦陷。

执行 `sudo kurrent host check` 做只读检查（分机部署时用 `--role control|storage`，同机用 `all`）；报告 FAIL 时先处理问题，`--strict` 把 SKIP 也计为失败。完整检查项见[安全模型](../architecture/security.md#部署检查清单)与[日常运维](../guide/operations.md#安全基线)。

## 相关页面

- [部署](../guide/deployment.md)
- [架构总览](../architecture/index.md#端口)
- [安全模型](../architecture/security.md#部署检查清单)
- [配置参考](./configuration.md)
- [日常运维](../guide/operations.md#安全基线)
