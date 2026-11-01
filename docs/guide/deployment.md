# 部署

> 本页从空白主机开始，依次完成部署准备、控制面、存储节点、固件与设备入池。部署单元是**控制面服务器 + 存储节点**；两者可分离，也可运行在同一台机器上，每台存储服务器运行一个 Agent。

部署按声明式配置执行：控制面 `control_plane/kurrent.yaml` 与节点 `storager/kurrent.yaml` 是唯一输入，`kurrent init` / `kurrent join` 负责校验与收敛，容器编排是内部细节。设备入池表示平台就绪；母盘准备、绑定、建盘与首次系统启动属于系统交付，见[系统盘定制](./system-disk.md)与[首次交付](./usage.md)。部署故障按组件定位，见[故障排查](./troubleshooting.md)；声明字段与默认值见[配置参考](../reference/configuration.md)。

## 部署准备

### 主机与角色

| 角色 | 最低要求 | 职责 |
| --- | --- | --- |
| 控制面服务器（×1） | Linux x86_64；Docker 与 Compose v2；一个 PXE 侧接口（独立网卡或 VLAN 子接口） | DHCP/TFTP、443 WebUI/API、PKI、设备台账与引导变量 |
| 存储节点（×1 起） | Linux x86_64；Docker 与 Compose v2；NVMe/TCP TLS 内核能力；`tlshd >= 1.5.0`；一块 btrfs/xfs 数据盘 | Agent（4840）与 nvmet-host（4421），承载母盘与系统盘 |
| 裸机设备（×N） | UEFI x86_64；可网络启动 | 通过 PXE 入池并启动系统盘 |

控制面与存储节点可以是同一台主机，但角色不合并：同机时仍运行一个 Agent，仍按本页顺序执行。控制面必须有一个位于 PXE 网段的地址；WebUI、API 与 Agent 回连使用管理网地址，两者不要混用。

### 网络规划

控制面需要两条可达路径：

- **管理面**：管理员浏览器与存储节点访问 443；控制面用这个地址签发 token、访问 Agent。
- **引导面**：裸机设备访问 DHCP（67/UDP）与 TFTP（69/UDP）；`spec.networking.interface` 绑定这一侧，该接口必须持有 PXE 子网内的地址。

推荐用两张网卡或 VLAN 子接口把两侧分开。单网卡同时承载管理面与引导面时，PXE 网段必须独立隔离，不能与办公网混跑。存储节点不参与 PXE，但引导客户端必须能路由到它的 **4421** 数据面端口；跨网段时在路由与防火墙上显式放通。

按实际环境替换：

- PXE 子网 `192.168.80.0/24`：`spec.networking.subnet`
- 控制面 PXE 地址 `192.168.80.1`：dnsmasq 绑定地址，须在 PXE 子网内
- DHCP 池 `192.168.80.50 ~ .100`：分配给裸机设备
- 网关 / DNS `192.168.80.2` / `223.5.5.5`：DHCP option 3 / 6
- 控制面管理地址 `192.168.1.10`：WebUI / API / `--cp-url`
- 存储节点管理地址 `192.168.1.20`：控制面访问 Agent 4840
- 存储数据面地址 `192.168.1.20`（或独立数据 IP）：客户端访问 4421，必须客户端可达
- 存储节点主机名 `storage-01`：任意；节点身份由 join 生成，与主机名解耦

**端口**：控制面服务器发布 67/69 UDP（DHCP/TFTP，host 网络）、80 TCP（自动 301 到 443，不承载内容或凭据）与 443 TCP（唯一 HTTPS 入口：WebUI、`/api/cp`、组件 enroll/renew 与引导链）。存储节点发布 4840 TCP（Agent HTTPS，控制面 → Agent）与 4421 TCP（NVMe/TCP 数据面，TLS 1.3，固件段与内核段共用）。

443 的引导链只反代 `/boot-vars`、`/devices/report`、`/devices/challenge` 三个引导端点；其余 `/devices/*` 管理接口不对外暴露。控制面管理面不发布 TCP 端口，只监听容器内 unix socket（`/run/kurrent/cp.sock`），由 nginx 经共享卷访问。nginx 对所有反代 location 默认清空身份头，仅在 `/api/cp/` 的 mTLS 校验通过时注入 DN/issuer/serial/fingerprint。控制面交互式 API 文档（`/openapi.json`、`/docs`）默认关闭，仅调试时以 `KURRENT_CP_OPENAPI=1` 开启。

### 仓库与 CLI

控制面与存储节点都需要仓库文件和 `kurrent` CLI。分机部署时在每台主机各执行一次；同机部署只执行一次。

```bash
git clone https://github.com/dutyc/kurrent
cd kurrent

# CLI 免本地编译：下载 release 单二进制（v0.3.0 起随 Release 发布；Linux amd64/arm64、Windows）
BASE=https://github.com/dutyc/kurrent/releases/download/v0.3.0
curl -sL -o cli/kurrent "$BASE/kurrent-linux-amd64"
chmod +x cli/kurrent
# 全局安装；本文档后续命令直接用 kurrent
sudo install -m 0755 cli/kurrent /usr/local/bin/kurrent
kurrent version   # 校验安装
```

> 版本随 Release 更新（https://github.com/dutyc/kurrent/releases，Windows 取 `kurrent-windows-amd64.exe`）；或源码构建替代下载（`cd cli && go build -o kurrent .`，需 Go 1.27+）。

### 主机依赖

两台主机都需要 Docker、Compose v2、`git`、`curl`、`jq` 与 `openssl`。Docker 与 Compose 按发行版或 Docker 官方安装文档准备；其余工具在 Debian/Ubuntu 上可一次安装：

```bash
sudo apt install -y git curl jq openssl
docker --version && docker compose version
```

**存储节点**额外完成数据面宿主准备。先确认数据盘设备名，以下 `mkfs` 示例只对空数据盘执行，不要指向系统盘：

```bash
# 1. nvmet 宿主前置（内核模块 + configfs + tlshd 版本 + 重启持久化；幂等）
sudo bash storager/nvmeof/host-prepare.sh
#    只读诊断（无需 root，部署前排查用）：
#    bash storager/nvmeof/host-prepare.sh --check

# 2. NVMe/TCP TLS：内核需带 CONFIG_NVME_TCP_TLS /
#    CONFIG_NVME_TARGET_TCP_TLS / CONFIG_NVME_KEYRING，
#    且宿主常驻 tlshd >= 1.5.0
grep -E 'CONFIG_NVME_(TCP_TLS|TARGET_TCP_TLS|KEYRING)' \
  /boot/config-$(uname -r)
# 发行版包低于 1.5.0 时见 storager/nvmeof/nvmet-host/README.md 源码安装
sudo apt install -y ktls-utils
sudo systemctl enable --now tlshd

# 3. 数据盘建议 btrfs 或 xfs（两者均支持 reflink/FICLONE，
#    母盘克隆秒级完成；ext4 不支持 reflink，会回退全量拷贝，
#    克隆时间随镜像大小线性增长）
sudo mkfs.btrfs /dev/sdb
sudo mkdir -p /pool1
sudo mount /dev/sdb /pool1
sudo mkdir -p /pool1/storager_img
```

数据盘要写入 `/etc/fstab`（推荐用 `blkid` 取 UUID）或等价的 systemd mount，确保重启后自动挂载；否则 `diskDir` 会落到根文件系统上的空目录。

> `host-prepare.sh` 与 `kurrent join` 预检 nvmet 模块、configfs 与 tlshd（**≥ 1.5.0 且服务运行中**）；内核 TLS 编译项仍需手工确认。tlshd 低于 1.5.0 会让轮换后的旧 PSK 遮蔽新 PSK（1.5.0 起进程 keyring 优先），脚本按失败处理。数据面 `4421` 建口失败时节点如实上报端口未就绪，控制面**拒绝引导**，不会回落明文。

### 引导网络边界

引导链的第一跳（DHCP → TFTP `snponly.efi` → TFTP `boot.ipxe`）没有可用的密码学锚：设备固件不会验证这段内容，iPXE 也尚未拿到控制面证书。当前把**引导网段本身作为信任边界**，在此之上保留设备身份 TOFU（NVRAM 密钥 + 注册窗口 + 绑定）与服务端 TOFU。部署必须满足：

- **物理/逻辑隔离**：PXE 网段为独立 VLAN 或独立物理口，不与办公网、存储数据面混跑；控制面是该网段唯一提供 DHCP 与 TFTP 的主机。
- **二层防护**：接入交换机启用 DHCP snooping（仅控制面上联口为 trusted）、Dynamic ARP Inspection、RA guard、端口安全/MAC 数量限制；有条件时用 802.1X 或 MAB 限定可接入端口。
- **入池控制**：注册窗口按需开启并设放行额度；关闭窗口后未知 MAC 不会入池。
- **吊销闭环**：`DELETE /devices/{mac}` 会在 dnsmasq 绑定文件写入 `MAC,ignore`，被吊销设备拿不到租约；重新登记（`/devices/report`）或清除条目（`DELETE /devices/revoked`）会移除该行。
- **TFTP 面最小化**：TFTP 根只挂载 `snponly.efi` 与 `boot.ipxe`，配置与菜单经 HTTPS 取回。

完整的威胁模型、残余风险与部署检查清单见[安全模型](../architecture/security.md)。

## 第 1 步：控制面服务器

### 1.1 声明 PXE 网络

控制面声明 `control_plane/kurrent.yaml` 是唯一输入。先生成带注释模板，再填写 `spec.networking` 五键：

```bash
kurrent config print init-defaults > control_plane/kurrent.yaml
vim control_plane/kurrent.yaml
```

最小可启动内容如下，其余块保留模板默认值：

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

- `interface` 填**面向 PXE 的接口**，不是管理网接口；同机多网卡时尤其容易填错。
- `dhcpRange` 是 `start,end` 单个字符串，不是两个数组元素。
- 服务器证书默认 SAN 只有 `127.0.0.1` 与 `localhost`。用管理网 IP 访问 WebUI 或让 curl 校验证书时，在首次 `kurrent init` 前把它加入 `spec.serverCert.san`：

```yaml
  serverCert:
    san: "IP:192.168.1.10,IP:192.168.80.1,IP:127.0.0.1,DNS:localhost"
```

编辑后先按运行时 schema 全量校验，再查看注入默认值后的生效声明：

```bash
# 未知字段、跨字段语义、策略上限都在写盘前失败
kurrent config validate
kurrent config show                # 输出生效声明
```

### 1.2 初始化并拉起控制面

```bash
kurrent init
```

`kurrent init` 依次完成：构建校验镜像 → 全量校验 → 应用声明 → 启动/重启 → 等待 `/healthz` → 核验 dnsmasq 保持运行。启动时控制面按 `spec.networking` 生成 `dnsmasq/dnsmasq.conf`（yml 是权威、conf 是派生物，勿手工编辑），并生成 TOFU 服务器证书与组件 PKI。

命令幂等可重跑：改声明后重跑即生效。**失败语义**：校验不过时声明不会写入；启动后控制面未通过健康检查（含启动期派生件失败）或 dnsmasq 未能保持运行即返回非零，并自动回滚到上一份声明、恢复旧配置运行。

### 1.3 验证服务

```bash
curl -k https://127.0.0.1/healthz           # {"status":"ok"}
head -5 dnsmasq/dnsmasq.conf                # interface=... 与声明一致
kurrent config status                       # configHash + 派生件一致性
docker ps                                   # 控制面 / WebUI / dnsmasq
```

浏览器访问 `https://<控制面管理地址>/`（WebUI，唯一入口 443；HTTP 自动 301 升级）。管理 token 登录方式：`kurrent token create --usage admin` 签发后在登录页提交，换取 HttpOnly 会话 Cookie（默认 12 小时 TTL，剩余不足一半才滑动续期）。需要浏览器自动 mTLS 时，把 admin 证书转为 PKCS#12 导入系统证书库：

```bash
openssl pkcs12 -export -out kurrent-admin.p12 \
  -inkey control_plane/state/pki/users/admin/client.key \
  -in control_plane/state/pki/users/admin/client.crt
# 双击 kurrent-admin.p12 导入（Windows/macOS 系统证书库；Firefox 需单独导入）
```

**CLI 管理凭据**：`kurrent init` 已自举 `control_plane/state/admin.conf`（入口地址 + admin 客户端证书及其信任锚，0600）。CLI 自动发现顺序为 `--credentials` ＞ `KURRENT_CREDENTIALS` ＞ 当前目录逐级上溯 `control_plane/state/admin.conf` → 可执行文件旁的仓库布局 → 用户级快照 `~/.kurrent/admin.conf`。在仓库目录内无需额外配置；脱离仓库运行时先执行一次 `kurrent credentials install`（凭据轮换后重跑刷新）。

### 1.4 防火墙与宿主基线

```bash
# 控制面服务器
sudo ufw allow 67,69/udp && sudo ufw allow 80,443/tcp && sudo ufw reload

# 认证体系成立的宿主前提：默认只读检查对外监听、Docker 暴露面与凭据文件权限
bash control_plane/host-baseline.sh
```

- 控制面宿主只应暴露 22（运维）、53/67/69（dnsmasq）、80/443（nginx）；存储节点另有 4840/4421。
- ufw 不过滤 Docker 发布端口：需要入站限制时在 `DOCKER-USER` 链收口。
- 控制面容器不挂载 `docker.sock`。
- CA 私钥、`users.yml`、`sessions.yml`、`admin.conf`、`audit.jsonl` 应为 600。
- 脚本报告 FAIL 时先处理问题；宿主 root 或 nginx 进程被攻破等同管理面沦陷。

### 1.5 签发存储节点引导凭据

```bash
kurrent token create --agent-id 3f2a9c1d8b7e --cp-url https://192.168.1.10
# 省略 --agent-id 则自动生成 12 位小写 hex 节点身份，命令同样携带
```

`--cp-url` 指定**存储节点侧可达的控制面 HTTPS 入口**（443 `/api/cp` 反代，Agent 经此 enroll/renew；缺省按 `--server` 主机推导）。`--agent-id` 是节点身份（= 节点声明 `metadata.name`），只接受 12 位小写十六进制，token 与它绑定；描述性名称写 `metadata.remark`。输出包含可直接在存储节点执行的 join 命令：

```
bootstrap token: <token>（expires ...，绑定节点 3f2a9c1d8b7e，0/1）

# 在绑定节点上执行（--agent-id 写入节点声明 metadata.name）：
kurrent join https://192.168.1.10 --token <token> --agent-id 3f2a9c1d8b7e
```

> token **绑定节点身份且默认一次性**：enroll 只接受该 `agent_id`，使用额度在签发前原子占用（签发失败退回），TTL 默认 60 分钟（`spec.pki.bootstrapTokenTtlMinutes`）、上限 24 小时（`spec.pki.bootstrapTokenMaxTtlMinutes`）；批量引导用 `kurrent token create --agent-id ... --uses N`（1..1000，且不超过 `spec.pki.bootstrapTokenMaxUses`，默认 100），过期或被吊销后重新签发。**nvmet-host 组件凭据无需签发**——agent enroll 上报 `backend=nvmet` 时控制面自动派生下发。

### 1.6 后续修改声明

```bash
vim control_plane/kurrent.yaml
# 写盘前全量校验；失败不改动任何文件
kurrent config validate
# 应用并收敛；失败自动回滚上一份声明
kurrent init
# 生效声明、configHash、派生件与证书漂移
kurrent config status
```

服务器证书的 SAN/天数变化默认**只报告漂移**（`derived.serverCert.drift`），不改变 TOFU 指纹；确认要让设备重新信任新证书后，设 `spec.serverCert.rotateOnChange: true` 再执行 `kurrent init`。策略块与后台覆盖参数的完整说明见[配置参考](../reference/configuration.md)与[日常运维](./operations.md)。

组件证书的吊销、CRL 生效与节点恢复不在部署主线内，见[日常运维](./operations.md#组件证书)。

## 第 2 步：存储节点

### 2.1 声明节点配置（join 前）

节点声明 `storager/kurrent.yaml` 决定数据面地址、盘目录与 NQN 命名域。**先声明再 join**，不要等 join 生成默认值后再改：分机部署时默认的 `advertiseUrl` 指向控制面主机，控制面会因此访问不到 Agent。

```bash
kurrent config print node-defaults > storager/kurrent.yaml
vim storager/kurrent.yaml
```

需要填写的键：

```yaml
apiVersion: kurrent.io/v1
kind: NodeConfiguration
metadata:
  name: ""
  remark: 生产存储
spec:
  agent:
    backend: nvmet
    advertiseUrl: https://192.168.1.20:4840
    diskDir: /pool1/storager_img
    nqnBase: nqn.2026-07.com.kurrent
  controlPlane:
    url: https://192.168.1.10
```

- `advertiseUrl` 是**控制面 → Agent 4840** 的地址。分机部署必须写存储节点管理地址；同机部署可保留默认，控制面以 bridge 网络运行且无法回连宿主时改为 `https://host.docker.internal:4840`。
- `diskDir` 必须是宿主绝对路径，且指向已挂载的数据盘；join 会把它同步为 `.env` 的 `KURRENT_DISK_DIR`，作为 compose 挂载源。盘目录不存在时先创建。
- `metadata.name` 是机器身份（12 位小写十六进制），由 join 从 `--agent-id` 写入；描述性名称写 `metadata.remark`。模板留空时不要先跑 `kurrent config validate --node`：身份尚未写入会校验失败；join 合并身份后会用同一份 schema 全量校验。join 完成后再用 `--node` 校验后续修改。
- `nqnBase` 是 Host NQN 与盘 NQN 的唯一命名域；同一集群内保持一致。

### 2.2 加入集群

在存储节点的仓库目录执行 token 输出中的命令：

```bash
kurrent join https://192.168.1.10 --token <token> --agent-id 3f2a9c1d8b7e
```

`kurrent join` 完成三项操作（幂等可重跑）：

1. 生成/更新 `storager/kurrent.yaml`：缺失时按模板创建，已存在则读入合并（手工字段保留）；写入 `metadata.name`，同步 `spec.controlPlane.url`。已声明身份与 `--agent-id` 不一致即拒绝。
2. 写入绑定身份的引导凭据 `storager/bootstrap/agent.token`。`nvmet-host.token` 由 agent enroll 按能力派生，无需手工提供。
3. 同步 `.env` 插值键并拉起/重启 agent 容器；backend 决定编排目录（nvmet → `storager/nvmeof`，stgt/lio → `storager/iscsi`）。

写盘前，join 用 agent 镜像内同一份 schema 校验合并后的声明；未知字段、非法路径或 NQN 会立即失败，不会等到容器启动才暴露。

### 2.3 验证存储节点

```bash
docker ps    # storager-agent / kurrent-nvmet-host

docker logs storager-agent 2>&1 | grep pki
docker logs kurrent-nvmet-host 2>&1 | grep -i "数据面端口就绪"
docker exec kurrent-nvmet-host python /app/healthcheck.py; echo $?
```

预期：agent 打印 `pki: client cert ok`，nvmet-host 打印 `4421 tsas=tls1.3`，healthcheck 返回 0（UDS + mTLS + configfs）。stgt/lio 后端查看对应容器。

回到控制面服务器确认登记与健康：

```bash
kurrent agents list
# ID             REMARK       HEALTH  BASE_URL
# 0d26b6f33a89   storage-01   ok      https://192.168.1.20:4840
```

输出还包含 ROLE、ENABLED 与 TAGS；`HEALTH=ok` 是控制面到 Agent 的实时探测结果。

**两个地址不要混用**：`advertiseUrl` 是控制面访问 Agent 的 4840 地址；`storager_ip` 是写入 `/boot-vars`、供引导客户端访问 4421 的数据面地址。数据面使用独立网段时，在 WebUI Agents 详情中设置 `storager_ip`，确保客户端可达。

**防火墙**（存储节点）：

```bash
sudo ufw allow 4840,4421/tcp && sudo ufw reload
```

> nvmet-host 管理面走共享卷 UDS（`/run/kurrent/nvmet/nvmet-host.sock`，目录 0700），不占 TCP、局域网不可达；`4421` 是唯一数据面端口，按需限制来源网段。引导客户端所在网段必须能访问它。

### 2.4 母盘与系统盘

存储节点本身不通过网络引导，它只存放并导出 Worker 的盘。`diskDir` 同时容纳两类文件：

- **母盘**一次性制作，文件名含 `_tpl_` 标记（如 `_tpl_ubuntu2404.img`），必须是 4K 原生 GPT 整盘镜像。放入 `diskDir` 后由 Agent 扫描并出现在母盘列表；母盘只作为克隆源，不导出给设备。
- **系统盘**由控制面按 Worker 从母盘 reflink 克隆，带独立的 `os_tag`、盘 NQN 与认证凭据。创建与首次启动见[首次交付](./usage.md)。

母盘的制作与校验见[系统盘定制](./system-disk.md)。新存储节点没有母盘也能 join；首次建盘前至少一台健康节点必须持有目标母盘，放置策略只会选择持有该母盘的节点。

## 第 3 步：固件与设备入池

### 3.1 安装固件产物

主仓库不包含固件二进制。设备固件由配套仓库 [kurrent-firmware](https://github.com/dutyc/kurrent-firmware) 构建；使用与当前上游基线匹配的发布产物，或按[固件构建与部署](../firmware/build-deploy.md)自行构建。

```bash
mkdir -p /tmp/kurrent-fw/pxe-uefi && cd /tmp/kurrent-fw
BASE=https://github.com/dutyc/kurrent-firmware/releases/latest/download
curl -sLO "$BASE/SHA256SUMS"
curl -sL -o pxe-uefi/snponly.efi "$BASE/pxe-uefi-snponly.efi"
sha256sum -c SHA256SUMS --ignore-missing

# 放回主仓库的 TFTP 根；替换后重启 dnsmasq 使文件级挂载指向新 inode
sudo install -m 0644 pxe-uefi/snponly.efi \
  /path/to/kurrent/tftp/snponly.efi
cd /path/to/kurrent
docker compose restart kurrent-dnsmasq
```

主仓库当前的 PXE 首跳固定为 `snponly.efi`，不支持按设备切换。需要 `pxe-uefi/ipxe.efi` 的机器不能在同一 PXE 网段与默认首跳混用，部署前需统一首跳产物；详见[固件构建与部署](../firmware/build-deploy.md#_2-选择产物)。

在 WebUI「引导链」页确认固件状态与六项能力（HTTPS 引导、NVS 持久化、设备密钥生成、公钥导出、挑战签名、TOFU 锚定），或直接查询接口（在控制面仓库目录执行，使用 admin 证书）：

```bash
curl -sS \
  --cert control_plane/state/pki/users/admin/client.crt \
  --key control_plane/state/pki/users/admin/client.key \
  --cacert control_plane/state/certs/server.crt \
  https://<控制面管理地址>/api/cp/boot-assets | jq '.firmware'
```

### 3.2 首次引导与入池

先在 WebUI → **Devices** 开启注册窗口（TTL 按 `spec.registration.windowMaxTtlMinutes` 收敛，默认 60 分钟；可选放行额度 `max_devices`），再让裸机设备接入 PXE 网段并设为网络启动。窗口必须在设备上报前开启；关闭后上报只更新指纹，不再入池。

```bash
# 控制面服务器：观察 DHCP/引导流
docker logs -f kurrent-dnsmasq 2>&1 | grep -E "DHCPACK|pxe|ipxe"
```

预期链路：设备 DHCP 获取地址 → TFTP 拉取 iPXE 与 `boot.ipxe` 指针 → iPXE 经 HTTPS 取回 `boot.ipxe.cfg` → 首次 `keygen` 生成 ECDSA P-256 私钥（留在 EFI NVRAM）→ `/devices/report` 上报指纹与公钥 → **设备出现在 Devices 页并完成密钥认领**。

确认设备已入池后关闭注册窗口。全部设备认领完成后，再在 Devices 页开启**验签强制**：此后的 `/boot-vars` 只接受通过挑战-应答验签的已绑定设备。

设备入池只表示平台侧与注册链路可用，此时设备还没有可启动的系统。真正启动还需要三步：存储节点上有目标发行版的母盘 → 为 Worker 克隆系统盘并设为默认盘 → 冷启动到登录提示符。母盘制作见[系统盘定制](./system-disk.md)，后两步见[首次交付](./usage.md)。
