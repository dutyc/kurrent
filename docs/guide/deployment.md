# 部署

> 本页从空白主机开始，依次完成部署准备、控制面、存储节点、固件与设备入池。部署单元是**控制面服务器 + 存储节点**；两者可分离，也可运行在同一台机器上，每台存储服务器运行一个 Agent。

部署按声明式配置执行：控制面 `control_plane/kurrent.yaml` 与节点 `storager/kurrent.yaml` 是唯一输入，`kurrent init` / `kurrent join` 负责校验与收敛，容器编排是内部细节。设备入池表示平台就绪；母盘准备、绑定、建盘与首次系统启动属于系统交付，见[系统盘定制](./system-disk.md)与[首次交付](./usage.md)。部署故障按组件定位，见[故障排查](./troubleshooting.md)；声明字段与默认值见[配置参考](../reference/configuration.md)，主机与宿主前提见[部署前置参考](../reference/host-prerequisites.md)。

## 部署准备

### 主机与角色

部署单元是控制面服务器加一台或多台存储节点；两者可同机，但角色不合并。主机最低要求、内核与 `tlshd` 版本、数据盘与宿主基线见[部署前置参考](../reference/host-prerequisites.md)。控制面必须有一个位于 PXE 网段的地址；WebUI、API 与 Agent 回连使用管理网地址，两者不要混用。

### 网络规划

控制面需要两条可达路径：

- **管理面**：管理员浏览器与存储节点访问 443；控制面用这个地址签发 token、访问 Agent。
- **引导面**：裸机设备访问 DHCP（67/UDP）与 TFTP（69/UDP）；`spec.networking.interface` 绑定这一侧，该接口必须持有 PXE 子网内的地址。

推荐用两张网卡或 VLAN 子接口把两侧分开。单网卡同时承载管理面与引导面时，PXE 网段必须独立隔离，不能与办公网混跑。引导客户端必须能路由到存储节点的 **4421** 数据面端口；跨网段时在路由与防火墙上显式放通。

按实际环境替换：

- PXE 子网 `192.168.80.0/24`：`spec.networking.subnet`
- 控制面 PXE 地址 `192.168.80.1`：dnsmasq 绑定地址，须在 PXE 子网内
- DHCP 池 `192.168.80.50 ~ .100`：分配给裸机设备
- 网关 / DNS `192.168.80.2` / `223.5.5.5`：DHCP option 3 / 6
- 控制面管理地址 `192.168.1.10`：WebUI / API / `--cp-url`
- 存储节点管理地址 `192.168.1.20`：控制面访问 Agent 4840
- 存储数据面地址 `192.168.1.20`（或独立数据 IP）：客户端访问 4421，必须客户端可达
- 存储节点主机名 `storage-01`：任意；节点身份由控制面在签发引导 token 时分配、join 写入状态文件，与主机名解耦

端口与监听边界见[架构总览](../architecture/index.md#端口)与[部署前置参考](../reference/host-prerequisites.md#网络与端口)。控制面管理面不发布 TCP 端口；443 的引导链只反代三个引导端点，其余 `/devices/*` 管理接口不对外暴露。控制面交互式 API 文档（`/openapi.json`、`/docs`）默认关闭，仅调试时以 `KURRENT_CP_OPENAPI=1` 开启。

### 仓库与 CLI

控制面与存储节点都需要仓库文件和 `kurrent` CLI。分机部署时在每台主机各执行一次；同机部署只执行一次。

```bash
git clone https://github.com/dutyc/kurrent
cd kurrent

# CLI 免本地编译：下载 release 单二进制（v0.3.0 起随 Release 发布；Linux amd64/arm64、Windows）
mkdir -p bin
BASE=https://github.com/dutyc/kurrent/releases/download/v0.3.0
curl -sL -o bin/kurrent "$BASE/kurrent-linux-amd64"
chmod +x bin/kurrent
# 全局安装；本文档后续命令直接用 kurrent
sudo install -m 0755 bin/kurrent /usr/local/bin/kurrent
kurrent version   # 校验安装
```

> 版本随 Release 更新（https://github.com/dutyc/kurrent/releases，Windows 取 `kurrent-windows-amd64.exe`）；或源码构建替代下载（`mkdir -p bin && cd cli && go build -o ../bin/kurrent .`，需 Go 1.27+）。构建产物统一放仓库根 `bin/`，不落在源码目录。

### 状态根与部署记录

运行时状态不放在仓库内。仓库只提供声明、代码与构建产物；台账、PKI、引导凭据与日志位于本机状态根，移动或改名 checkout 不改变部署身份。

- 状态根默认 `$XDG_STATE_HOME/kurrent`（未设置时为 `~/.local/state/kurrent`）；`KURRENT_STATE_DIR` 环境变量或 `--state-dir` 参数可覆盖。
- 部署记录 `~/.kurrent/deployment.yaml`（0600）保存固定项目名（`kurrent`、`kurrent-storage`）、仓库根、状态根与角色状态，`kurrent deployment show` 只读展示；`-o env` 输出手工执行 compose 所需的变量。
- 管理凭据快照位于 `~/.kurrent/`（0700），由 `kurrent init` 在首次部署时安装。

控制面与节点在状态根下的布局：

| 路径 | 内容 | 容器内挂载点 |
|---|---|---|
| `<state>/control-plane/state` | 设备/Worker 台账、PKI、证书与 `admin.conf` | `/app/state` |
| `<state>/control-plane/config/agents.yml` | Agent 台账 | `/app/config/agents.yml` |
| `<state>/control-plane/dnsmasq` | 渲染配置、DHCP 主机与租约 | `/app/dnsmasq` |
| `<state>/control-plane/bootstrap` | 一次性管理凭据交换目录 | `/app/bootstrap` |
| `<state>/storager/state` | 节点身份 `agent.id` | `/etc/kurrent/state` |
| `<state>/storager/bootstrap` | `agent.token` 与派生凭据 | `/etc/kurrent/bootstrap` |
| `<state>/storager/pki/agent-<id>`、`pki/nvmet-<id>` | 组件证书 | `/etc/kurrent/pki` |
| `<state>/storager/logs` | Agent 与 nvmet-host 日志 | `/var/log/kurrent` |
| `<state>/storager/certs/cp-ca.crt` | 控制面 TOFU 信任锚 | `/etc/kurrent/cp-ca.crt` |

同机部署共用一个状态根；分机部署时存储节点使用自己的状态根，控制面信任锚通过 join 的 `--cp-ca` 提供。同一主机只支持一个部署：容器名与 compose 项目名都是固定的，第二套部署需要独立主机。

### 主机依赖

两台主机都需要 Docker、Compose v2、`git`、`curl`、`jq` 与 `openssl`；**控制面主机另需 Node.js 18+ 与 npm**，用于构建 WebUI 静态资源。Docker 与 Compose 按发行版或 Docker 官方安装文档准备；其余工具在 Debian/Ubuntu 上可一次安装：

```bash
sudo apt install -y git curl jq openssl
docker --version && docker compose version && node --version && npm --version
```

**存储节点**额外完成数据面宿主准备；完整前提与诊断语义见[部署前置参考](../reference/host-prerequisites.md#存储节点内核与-tlshd)。先确认数据盘设备名，以下 `mkfs` 示例只对空数据盘执行，不要指向系统盘：

```bash
# 1. nvmet 宿主前置（内核模块 + configfs + tlshd 版本 + 重启持久化；幂等）
sudo bash storager/nvmeof/host-prepare.sh
#    只读诊断（无需 root，部署前排查用；在仓库根执行）：
#    kurrent host check --role storage

# 2. 确认内核 NVMe/TCP TLS 配置
grep -E 'CONFIG_NVME_(TCP_TLS|TARGET_TCP_TLS|KEYRING)' \
  /boot/config-$(uname -r)
sudo apt install -y ktls-utils
sudo systemctl enable --now tlshd

# 3. 数据盘（btrfs/xfs 支持 reflink）
sudo mkfs.btrfs /dev/sdb
sudo mkdir -p /pool1 /pool1/storager_img
sudo mount /dev/sdb /pool1
```

数据盘要写入 `/etc/fstab`（推荐用 `blkid` 取 UUID）或等价的 systemd mount，确保重启后自动挂载；否则 `diskDir` 会落到根文件系统上的空目录。

### 引导网络边界

引导链的第一跳（DHCP → TFTP `snponly.efi` → TFTP `boot.ipxe`）没有可用的密码学锚：设备固件不会验证这段内容，iPXE 也尚未拿到控制面证书。部署必须把**引导网段本身作为信任边界**：

- PXE 网段为独立 VLAN 或独立物理口，控制面是该网段唯一提供 DHCP 与 TFTP 的主机。
- 接入交换机启用 DHCP snooping（仅控制面上联口为 trusted）、Dynamic ARP Inspection、RA guard 与端口安全；有条件时用 802.1X 或 MAB 限定可接入端口。
- 注册窗口按需开启并设放行额度，用后关闭；TFTP 根只挂载 `snponly.efi` 与 `boot.ipxe`。

完整的威胁模型、残余风险与部署检查清单见[安全模型](../architecture/security.md#部署检查清单)。

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
# WebUI 静态资源（dist 不入库；必须在首次 up 前构建）
(cd webui/app && npm ci && npm run build && test -f dist/index.html)

kurrent init
```

`webui/app/dist` 是构建产物，控制面容器把它以只读方式挂载给 nginx。首次 `docker compose up` 前必须先构建：缺失的 `dist` 会被 compose 建成 root 所有的空目录，之后普通用户执行 `npm run build` 会因无写权限失败（先 `sudo rm -rf webui/app/dist` 再重建）。`kurrent init` 会预检该目录，缺失或属主不符时直接失败并给出上述修复命令。

`kurrent init` 是一次完整的首次部署事务：

1. 预检 Docker/Compose v2、仓库布局、WebUI 构建产物、状态根与 `~/.kurrent` 的属主与可写性；发现仓库内旧状态时要求 `--adopt-state`。
2. 在控制面镜像内按运行时 schema 全量校验声明；通过后应用声明。
3. 在状态根下建立目录并种下 `agents.yml`、dnsmasq 配置与租约等 compose 文件挂载点，写入部署记录（`bootstrapping`）。
4. 以固定项目名 `kurrent` 拉起编排，等待 `/healthz`。
5. 控制面把管理凭据（入口地址、admin 客户端证书及其信任锚）发布到一次性交换目录；CLI 校验逐文件 SHA-256 与证书/私钥配对后安装到 `~/.kurrent/admin.conf`，并用安装副本完成 mTLS `GET /whoami` 自检。
6. 交换包消费并删除，重启 dnsmasq 并确认其保持运行，部署记录转为 `ready`。

幂等可重跑：改声明后重跑即生效；移动 checkout 后重跑会以 `--force-recreate` 重建容器并更新记录中的仓库根，状态根与台账不变。**失败语义**：校验不过时声明不会写入；启动后未通过健康检查或 dnsmasq 未能保持运行即返回非零，并自动回滚到上一份声明、恢复旧配置运行；凭据交换或自检失败时保留交换包与 `bootstrapping` 记录，修复后重跑从该步继续。

状态根中 `control-plane/dnsmasq/dnsmasq.conf` 由控制面按 `spec.networking` 生成（声明是权威、conf 是派生物，勿手工编辑），TOFU 服务器证书与组件 PKI 同样在状态根下生成。容器以 root 写入的这些文件会把属主对齐所在目录的属主，宿主侧备份与迁移不需要 `sudo`。

### 1.3 验证服务

```bash
STATE=${KURRENT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/kurrent}
curl -k https://127.0.0.1/healthz           # {"status":"ok"}
curl -ks https://127.0.0.1/ | grep -q '<div id="root">'   # WebUI 静态资源已挂载
head -5 "$STATE/control-plane/dnsmasq/dnsmasq.conf"        # interface=... 与声明一致
kurrent config status                       # configHash + 派生件一致性
kurrent deployment show                     # 仓库根、状态根、项目名与状态
docker ps                                   # 控制面 / WebUI / dnsmasq
```

浏览器访问 `https://<控制面管理地址>/`（WebUI，唯一入口 443；HTTP 自动 301 升级）。管理 token 登录方式：`kurrent token create --usage admin` 签发后在登录页提交，换取 HttpOnly 会话 Cookie（默认 12 小时 TTL，剩余不足一半才滑动续期）。需要浏览器自动 mTLS 时，把 admin 证书转为 PKCS#12 导入系统证书库：

```bash
STATE=${KURRENT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/kurrent}
openssl pkcs12 -export -out kurrent-admin.p12 \
  -inkey "$STATE/control-plane/state/pki/users/admin/client.key" \
  -in "$STATE/control-plane/state/pki/users/admin/client.crt"
# 双击 kurrent-admin.p12 导入（Windows/macOS 系统证书库；Firefox 需单独导入）
```

**CLI 管理凭据**：`kurrent init` 已通过一次性交换安装 `~/.kurrent/admin.conf`（入口地址、admin 客户端证书与信任锚，0600），并在安装后用该副本完成 mTLS 自检。CLI 自动发现顺序为 `--credentials` ＞ `KURRENT_CREDENTIALS` ＞ 部署记录指向的 `<state>/control-plane/state/admin.conf` ＞ 当前目录逐级上溯的旧布局 `control_plane/state/admin.conf` → 可执行文件旁的仓库布局 → 用户级快照 `~/.kurrent/admin.conf`。任意目录均可直接使用；凭据轮换后重跑 `kurrent init` 或 `kurrent credentials install` 刷新快照。

一次性交换的机制：交换目录 `<state>/control-plane/bootstrap`（0700，部署用户所有）挂载为容器的 `/app/bootstrap`。`init` 先写入 `request`；控制面在 PKI 与 `admin.conf` 就绪后，把 `admin.conf` 及其引用的服务端 CA、admin 证书与私钥复制进 `pki/`，改写为包内相对路径，按 0700/0600 落盘并把属主对齐交换目录，最后写入含逐文件 SHA-256 的 `ready`，再删除 `request`。CLI 校验路径不越界、文件哈希、证书与私钥配对及 server 地址后才安装快照；mTLS 自检通过才写 `consumed` 并删除副本。交换包只是副本，权威文件始终是 `<state>/control-plane/state/admin.conf`；任一步失败都不写 `consumed`，重跑从该步继续。信任边界是同机文件系统属主——目录归部署用户，容器 root 可写，凭据不进入日志、命令行参数或环境变量。

### 1.4 防火墙与宿主基线

```bash
# 控制面服务器
sudo ufw allow 67,69/udp && sudo ufw allow 80,443/tcp && sudo ufw reload

# 认证体系成立的宿主前提：只读检查对外监听、Docker 暴露面与凭据文件权限
sudo kurrent host check
```

预期监听、文件权限与 Docker 端口收口见[部署前置参考](../reference/host-prerequisites.md#宿主基线)与[安全模型](../architecture/security.md#部署检查清单)。报告 FAIL 时先处理问题（`--strict` 另把 SKIP 计为失败）；宿主 root 或 nginx 进程被攻破等同管理面沦陷。

### 1.5 签发存储节点引导凭据

```bash
kurrent token create --agent-id 3f2a9c1d8b7e --cp-url https://192.168.1.10
# 省略 --agent-id 则由控制面分配 12 位小写 hex 新身份，命令携带返回的身份
```

`--cp-url` 指定**存储节点侧可达的控制面 HTTPS 入口**（443 `/api/cp` 反代，Agent 经此 enroll/renew；缺省按 `--server` 主机推导）。`--agent-id` 是节点身份，只接受 12 位小写十六进制，token 与它绑定；省略时由控制面分配新身份并随响应返回（显式传入用于绑定已有身份，如恢复）；昵称属于控制面台账，join 之后在 WebUI 或 `kurrent agents edit` 中设置。输出包含可直接在存储节点执行的 join 命令：

```
bootstrap token: <token>（expires ...，绑定节点 3f2a9c1d8b7e，0/1）

# 在绑定节点上执行（--agent-id 写入节点状态根下的 storager/state/agent.id）：
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
- `diskDir` 必须是宿主绝对路径，且指向已挂载的数据盘；join 在拉起容器时把它作为 `KURRENT_DISK_DIR` 注入 compose 插值，不落盘为第二份配置。盘目录不存在时先创建。
- `nqnBase` 是 Host NQN 与盘 NQN 的唯一命名域；同一集群内保持一致。

节点身份不在声明里：控制面在签发 bootstrap token 时分配身份，join 把它写入节点状态根下的 `storager/state/agent.id`，两个容器只读挂载该目录。声明因此只包含人能选择的参数，`kurrent config validate --node storager/kurrent.yaml` 在 join 之前即可执行。

### 2.2 加入集群

在存储节点的仓库目录执行 token 输出中的命令：

```bash
kurrent join https://192.168.1.10 --token <token> --agent-id 3f2a9c1d8b7e
```

`kurrent join` 完成四项操作（幂等可重跑）：

1. 校验 `storager/kurrent.yaml`：缺失时按模板创建，已存在则读入合并（手工字段保留），并同步 `spec.controlPlane.url`。
2. 在节点状态根下建立 `storager/{state,bootstrap,pki,logs,certs}`，落盘节点身份 `state/agent.id` 与绑定该身份的引导凭据 `bootstrap/agent.token`（0600）。首次执行必须提供 `--agent-id`；后续重跑可省略，但状态文件已存在时 `--agent-id` 必须与之一致。`nvmet-host.token` 由 agent enroll 按能力派生，无需手工提供。
3. 解析控制面信任锚并复制为 `certs/cp-ca.crt`：`--cp-ca` 优先，其次同机控制面状态根中的 `control-plane/state/certs/server.crt`，再次为节点已有的副本。三者都不存在时 join 失败并给出从控制面复制该文件的命令。
4. 以 `diskDir`、节点状态根与信任锚注入 compose 变量，拉起/重启 agent；nvmet 后端随后重启 nvmet-host 以消费派生凭据。backend 决定编排目录（nvmet → `storager/nvmeof`，stgt/lio → `storager/iscsi`），compose 项目名固定为 `kurrent-storage`。

写盘前，join 用 agent 镜像内同一份 schema 校验声明；未知字段、非法路径或 NQN 会立即失败，不会等到容器启动才暴露。默认 `diskDir`（`storager_img`）位于 checkout 内，移动仓库会连同盘镜像一起移动；join 会就此告警，需要固定位置时在声明中写仓库外的绝对路径。

分机部署首次 join 时，用 `--cp-ca` 指定从控制面取回的 `server.crt`：

```bash
scp admin@192.168.1.10:<state>/control-plane/state/certs/server.crt /tmp/cp-ca.crt
kurrent join https://192.168.1.10 --token <token> --agent-id 3f2a9c1d8b7e --cp-ca /tmp/cp-ca.crt
```

节点状态根默认同样为 `$XDG_STATE_HOME/kurrent`；`--state-dir` 或 `KURRENT_STATE_DIR` 可指定其他位置（如独立数据盘）。

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

`diskDir` 同时容纳两类文件：

- **母盘**一次性制作，文件名含 `_tpl_` 标记（如 `_tpl_ubuntu2404.img`），必须是 4K 原生 GPT 整盘镜像。放入 `diskDir` 后由 Agent 扫描并出现在母盘列表；母盘只作为克隆源，不导出给设备。
- **系统盘**由控制面按 Worker 从母盘 reflink 克隆，带独立的 `os_tag`、盘 NQN 与认证凭据。创建与首次启动见[首次交付](./usage.md)。

母盘的制作与校验见[系统盘定制](./system-disk.md)。新存储节点没有母盘也能 join；首次建盘前至少一台健康节点必须持有目标母盘，放置策略只会选择持有该母盘的节点。

## 第 3 步：固件与设备入池

### 3.1 安装固件产物

主仓库不包含固件二进制。设备固件由配套仓库 [kurrent-firmware](https://github.com/dutyc/kurrent-firmware) 构建；使用与当前上游基线匹配的发布产物，或按[固件构建与部署](../firmware/build-deploy.md)自行构建。

设备只支持 x86_64 UEFI 引导，传统 BIOS（Legacy/CSM）不再支持；其余设备前提见[部署前置](../reference/host-prerequisites.md#主机角色)。

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
eval "$(kurrent deployment show -o env)"   # 状态根等插值变量
docker compose restart kurrent-dnsmasq
```

主仓库当前的 PXE 首跳固定为 `snponly.efi`，不支持按设备切换。需要 `pxe-uefi/ipxe.efi` 的机器不能在同一 PXE 网段与默认首跳混用，部署前需统一首跳产物；详见[固件构建与部署](../firmware/build-deploy.md#_2-选择产物)。

在 WebUI「引导链」页确认固件状态与六项能力（HTTPS 引导、NVS 持久化、设备密钥生成、公钥导出、挑战签名、TOFU 锚定），或直接查询接口（在控制面仓库目录执行，使用 admin 证书）：

```bash
STATE=${KURRENT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/kurrent}
curl -sS \
  --cert "$STATE/control-plane/state/pki/users/admin/client.crt" \
  --key "$STATE/control-plane/state/pki/users/admin/client.key" \
  --cacert "$STATE/control-plane/state/certs/server.crt" \
  https://<控制面管理地址>/api/cp/boot-assets | jq '.firmware'
```

### 3.2 首次引导与入池

先在 WebUI → **Devices** 开启注册窗口（TTL 按 `spec.registration.windowMaxTtlMinutes` 收敛，默认 60 分钟；可选放行额度 `max_devices`），再让裸机设备接入 PXE 网段并设为 UEFI 网络启动。窗口必须在设备上报前开启；关闭后上报只更新指纹，不再入池。

```bash
# 控制面服务器：观察 DHCP/引导流
docker logs -f kurrent-dnsmasq 2>&1 | grep -E "DHCPACK|pxe|ipxe"
```

预期链路：设备 DHCP 获取地址 → TFTP 拉取 iPXE 与 `boot.ipxe` 指针 → iPXE 经 HTTPS 取回 `boot.ipxe.cfg` → 首次 `keygen` 生成 ECDSA P-256 私钥（留在 EFI NVRAM）→ `/devices/report` 上报指纹与公钥 → **设备出现在 Devices 页并完成密钥认领**。

确认设备已入池后关闭注册窗口。全部设备认领完成后，再在 Devices 页开启**验签强制**：此后的 `/boot-vars` 只接受通过挑战-应答验签的已绑定设备。

设备入池只表示平台侧与注册链路可用，此时设备还没有可启动的系统。真正启动还需要三步：存储节点上有目标发行版的母盘 → 为 Worker 克隆系统盘并设为默认盘 → 冷启动到登录提示符。母盘制作见[系统盘定制](./system-disk.md)，后两步见[首次交付](./usage.md)。

## 迁移与仓库移动

运行时状态与仓库解耦后，仓库位置只影响代码、声明与构建产物：

- **移动或改名 checkout**：把仓库移到新路径后，在新位置执行 `kurrent init --dir <新仓库根>`。命令按部署记录复用原状态根与固定项目名，以 `--force-recreate` 重建容器（挂载源仍是绝对路径），并更新记录中的仓库根；台账、PKI 与凭据不变。
- **加固存储节点**：在节点新位置执行 `kurrent join <cp-url> --dir <新仓库根>`；节点状态根、身份与凭据同样保持不变。
- **旧布局迁移**（状态仍在仓库内的早期部署）：首次 `kurrent init` 会检测 `control_plane/state`、`storager/state` 等旧路径并拒绝继续，提示带 `--adopt-state` 重跑。迁移会先停旧编排、逐文件校验复制到状态根，再启动并完成凭据自检；全部成功后才删除旧副本，失败则保留旧状态。停栈前会同时检查控制面与同机存储节点旧状态的属主；存在其他用户的文件（例如 Docker 以 root 创建）时，命令用一条 `sudo chown` 列出全部待修复路径并停止，不会复制一半。
- **状态根丢失**：部署记录存在而状态根不存在时，`kurrent init` 拒绝新建空部署，需先从备份恢复状态根，或删除记录后重新部署。

状态根的备份、恢复与迁移前停机要求见[日常运维](./operations.md#备份与恢复)。
