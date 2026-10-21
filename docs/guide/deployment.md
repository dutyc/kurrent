# Kurrent 部署指南

> 覆盖角色：**控制面服务器 + 存储节点**（可同机自测，亦可分离部署与横向扩展——每台存储服务器一个 Agent）。以下按两角色分步；同机自测时在同一台机器顺序执行即可。

本文基于 Kurrent 当前的声明式配置形态：控制面声明 `control_plane/kurrent.yaml` 由 `kurrent config print init-defaults` 生成模板后编辑、`kurrent init` 校验并收敛启动；存储节点声明 `storager/kurrent.yaml` 由 `kurrent config print node-defaults` 生成模板后编辑、`kurrent join` 加入。yml 是唯一输入，CLI 只负责校验与收敛，容器编排是内部细节。

## 拓扑与端口规划

```
┌─────────────────────┐         ┌─────────────────────┐
│  控制面服务器          │         │  存储节点             │
│  kurrent-dnsmasq    │   LAN   │  storager-agent      │
│   (DHCP/TFTP 67/69) │◄───────►│   (HTTPS 4840)       │
│  kurrent-control-   │         │  kurrent-nvmet-host  │
│   plane（本机回环）   │         │   (NVMe/TCP 4421)    │
│  kurrent-webui      │         └──────────┬──────────┘
│   (443 唯一入口)     │                    │ 存储盘 btrfs/xfs
└─────────┬───────────┘         ┌──────────▼──────────┐
          │ PXE 网段            │  裸机客户端（Devices） │
          └────────────────────►│  网络引导 → 入池       │
                               └─────────────────────┘
```

**网络规划示例**（按你的实际环境替换）：

| 项 | 值 | 说明 |
| --- | --- | --- |
| PXE 子网 | `192.168.80.0/24` | 控制面绑定的服务网段（DHCP/TFTP/引导流） |
| DHCP 池 | `192.168.80.50 ~ .100` | 分配给裸机客户端 |
| 网关 / DNS | `192.168.80.2` / `223.5.5.5` | DHCP 下发的 option 3 / 6 |
| 控制面 IP | `192.168.1.10` | 局域网管理地址（WebUI / API / 存储节点回连） |
| 存储节点 IP | `192.168.1.20` | 控制面经此访问 Agent（4840） |
| 存储节点主机名 | `storage-01` | 随意设置（节点身份由 join 自动生成随机标识，与主机名解耦） |

**端口清单**：

| 端口 | 协议 | 服务 | 位置 |
| --- | --- | --- | --- |
| 67 / 69 | UDP | DHCP / TFTP（host 网络） | 控制面服务器 |
| 80 | TCP | HTTP → 443 自动 301 升级（不承载内容/凭据） | 控制面服务器 |
| 443 | TCP | 唯一入口：HTTPS（WebUI + `/api/cp` 反代控制面 + 组件 enroll/renew + 引导链） | 控制面服务器 |
| 4839 | TCP | 控制面 API（仅本机回环：`kurrent init` 健康检查 / 调试；不对外开放） | 控制面服务器 |
| 4840 | TCP | 存储 Agent HTTPS（控制面 → Agent） | 存储节点 |
| 4841 | TCP | nvmet-host 管理服务（host 网络；仅 Agent 容器经 `host.docker.internal` 访问，不应向局域网开放） | 存储节点 |
| 4421 | TCP | NVMe/TCP 数据面，单口 **TLS 1.3**（固件引导段与内核段共用） | 存储节点 |

## 前置条件

**Linux 服务器**（控制面与存储节点宿主，x86_64；已在 Debian 12/13 与 Ubuntu 24.04/26.04 上验证，自测可同机）：

```bash
# 1. Docker 与 Compose v2
docker --version && docker compose version

# 2. 存储节点：nvmet 宿主前置（内核模块 + configfs + 重启持久化；幂等）
sudo bash storager/nvmeof/host-prepare.sh
#    只读诊断（无需 root，部署前排查用）：bash storager/nvmeof/host-prepare.sh --check

# 3. 存储节点：NVMe/TCP TLS 前置——内核需带 CONFIG_NVME_TCP_TLS /
#    CONFIG_NVME_TARGET_TCP_TLS / CONFIG_NVME_KEYRING，且宿主常驻 tlshd
grep -E 'CONFIG_NVME_(TCP_TLS|TARGET_TCP_TLS|KEYRING)' /boot/config-$(uname -r)
sudo apt install -y ktls-utils        # Debian/Ubuntu；其他发行版取对应包
sudo systemctl enable --now tlshd

# 4. 存储节点：数据盘建议 btrfs 或 xfs（两者均支持 reflink/FICLONE，母盘克隆秒级完成；
#    ext4 不支持 reflink，会回退全量拷贝，克隆时间随镜像大小线性增长）
#    挂载到 /pool1（示例）：sudo mkfs.btrfs /dev/sdb && sudo mount /dev/sdb /pool1
```

> `host-prepare.sh` 只预检 nvmet 模块与 configfs；上面第 3 步的 TLS 条件与 tlshd 目前需手工确认。数据面 `4421` 建口失败时节点如实上报端口未就绪，控制面**拒绝引导**，不会回落明文。

## 第 1 步：控制面服务器

### 1.1 准备仓库与安装 kurrent CLI

```bash
git clone https://github.com/dutyc/kurrent && cd kurrent

# CLI 免本地编译：下载 release 单二进制（v0.3.0 起随 Release 发布；Linux amd64/arm64、Windows）
curl -sL -o cli/kurrent https://github.com/dutyc/kurrent/releases/download/v0.3.0/kurrent-linux-amd64
chmod +x cli/kurrent
sudo install -m 0755 cli/kurrent /usr/local/bin/kurrent   # 全局安装（本文档后续命令直接用 kurrent）
kurrent version   # 校验安装
```

> 版本随 Release 更新（https://github.com/dutyc/kurrent/releases，Windows 取 `kurrent-windows-amd64.exe`）；或源码构建替代下载（`cd cli && go build -o kurrent .`，需 Go 1.27+）。

### 1.2 生成控制面声明配置

声明式配置即 `control_plane/kurrent.yaml`（**yml 是唯一输入**，CLI 只负责校验与启动）。先生成带注释模板，再按环境编辑：

```bash
kurrent config print init-defaults > control_plane/kurrent.yaml   # 生成模板
vim control_plane/kurrent.yaml        # 修改 spec.networking 五键（必填：绑定网卡/服务网段/DHCP 池/网关/DNS）
```

`spec.networking` 五键是部署环境事实，必须显式填写（模板含示例值与注释）；其余块（组件 PKI 策略、TOFU 证书、引导行为等）带默认值，可不动。

### 1.3 初始化并拉起控制面

一条命令完成校验与收敛启动（配置即声明、CLI 即工具；容器编排为内部细节）：

```bash
kurrent init        # 校验 kurrent.yaml → 启动/重启控制面 → 等待 /healthz → 重启 dnsmasq 加载新 conf
```

控制面启动时自动完成：按 `spec.networking` 生成 `dnsmasq/dnsmasq.conf`（yml 是权威、conf 是派生物，勿手工编辑）、生成 TOFU 服务器证书与组件 PKI。`kurrent init` 幂等可重跑：改声明后重跑即生效。

### 1.4 验证

```bash
curl http://127.0.0.1:4839/healthz          # {"status":"ok"}
head -5 dnsmasq/dnsmasq.conf                # interface=ens33 ...（已按 yml 生成）
```

浏览器访问 `https://<控制面IP>/`（WebUI，唯一入口 443；HTTP 自动 301 升级）。

**CLI 管理凭据**：`kurrent init` 已自举 `control_plane/state/admin.conf`（kurrent
原生扁平 YAML：入口地址 + admin 客户端证书及其信任锚路径，0600）。CLI 自动发现顺序
`--credentials` ＞ `KURRENT_CREDENTIALS` ＞ 自动发现（当前目录逐级上溯
`control_plane/state/admin.conf` → 可执行文件旁的仓库布局 → 用户级快照
`~/.kurrent/admin.conf`）——仓库目录下开箱即用（admin 证书走 443 mTLS）；脱离仓库
运行时先执行一次 `kurrent credentials install` 生成快照（凭据轮换后重跑刷新），
`--server` 可覆盖文件中的入口地址。认证体系（认证器链 / 会话 / RBAC）详见 API 文档
1.3。

**WebUI 登录方式**（二选一）：

1. **管理 token（登录页）**：`kurrent token create --usage admin` 签发后在登录页粘贴
   → 换取 HttpOnly 会话 Cookie（TTL 7 天滑动续期，刷新/重开浏览器保持登录；侧边栏
   底部可登出）。
2. **管理证书（自动认证）**：将 admin 证书转为 PKCS#12 导入系统证书库，浏览器访问
   时自动完成 mTLS 认证、无需登录：

   ```bash
   openssl pkcs12 -export -out kurrent-admin.p12 \
     -inkey control_plane/state/pki/users/admin/client.key \
     -in control_plane/state/pki/users/admin/client.crt
   # 双击 kurrent-admin.p12 导入（Windows/macOS 系统证书库；Firefox 需单独导入）
   ```

**防火墙**（控制面服务器）：

```bash
sudo ufw allow 67,69/udp && sudo ufw allow 80,443/tcp && sudo ufw reload
```

### 1.5 签发存储节点引导凭据

```bash
kurrent token create --cp-url https://192.168.1.10
```

`--cp-url` 指定**存储节点侧可达的控制面 HTTPS 入口**（443 `/api/cp` 反代，Agent 经此 enroll/renew；缺省按 `--server` 主机推导）。输出形如（含带地址、可直接粘贴的节点侧命令）：

```
bootstrap token: a1b2c3.d4e5f6a7b8c9d0e1（expires ...，TTL 内可被多次 enroll 复用）

# 存储节点上执行（命令已携带控制面地址，节点执行即自动生成/更新声明）：
kurrent join https://192.168.1.10 --token a1b2c3.d4e5f6a7b8c9d0e1
# 加入后验证：kurrent agents list（或 WebUI「Agent 列表」health=ok）
```

> token 是**集群级通用引导凭据**：不绑节点、TTL 内可被多次 enroll 复用，任意存储节点可用同一 token 加入；有效期为 7 天（`spec.pki.bootstrapTokenTtlDays`），过期重新签发。**nvmet-host 组件凭据无需签发**——agent enroll 上报 `backend=nvmet` 时控制面自动派生下发。

## 第 2 步：存储节点

### 2.1 准备

```bash
git clone https://github.com/dutyc/kurrent
cd kurrent
mkdir -p storager_img        # 盘映像目录（kurrent join 默认；自定义路径见 2.3）
curl -sL -o cli/kurrent https://github.com/dutyc/kurrent/releases/download/v0.3.0/kurrent-linux-amd64 && chmod +x cli/kurrent
sudo install -m 0755 cli/kurrent /usr/local/bin/kurrent   # 按 1.1 安装 CLI（或从控制面机器拷贝已装二进制）
```

### 2.2 声明节点配置并加入集群

节点声明式配置为 `storager/kurrent.yaml`（yml 即声明）。**节点无需预编辑任何文件**——签发命令已携带控制面地址，粘贴执行即可：

```bash
cd /path/to/kurrent
kurrent join https://192.168.1.10 --token a1b2c3.d4e5f6a7b8c9d0e1
```

`kurrent join` 完成三件事（幂等可重跑）：
1. 生成/更新 `storager/kurrent.yaml`（缺失时按模板自动生成：`metadata.name` 自动生成随机短标识、地址同步进 `spec.controlPlane.url`；已存在则读入合并——非 forbid 式，手工编辑保留。人工输入只需写 `metadata.remark` 备注——随首次 enroll 登记进控制面台账，WebUI 卡片展示、编辑表单可改）
2. 写入通用引导凭据 `storager/bootstrap/agent.token`（`--token` 给则写入；不给则要求文件已就位。`nvmet-host.token` 由 agent enroll 按能力派生自动生成，无需手工提供）
3. 同步 `.env` 插值键（组件 PKI 宿主路径 `KURRENT_AGENT_PKI_HOST` 等）并拉起/重启 agent 容器（backend 决定编排：nvmet → `storager/nvmeof`，stgt/lio → `storager/iscsi`）

预置声明（可选）：自定义 backend/diskDir/advertiseUrl 等业务键时，先 `kurrent config print node-defaults > storager/kurrent.yaml` 生成带注释模板并按环境编辑，再执行 join（声明读入合并，手工字段保留）。

**分离部署关键一步**：agent 的广告地址默认推导为 `https://<cp-host>:4840`（同机形态），控制面与存储节点分机时控制面必须经**存储节点局域网 IP** 访问 Agent——编辑声明文件覆盖它（声明配置层覆盖）：

```bash
# 编辑 storager/kurrent.yaml：spec.agent.advertiseUrl → https://192.168.1.20:4840
# 改完重跑：kurrent join <cp-url> --token <token>（收敛：重启 agent 使新声明生效）
```

### 2.3 拉起存储数据面

`kurrent join` 已收敛启动 agent（backend 决定编排：nvmet → `storager/nvmeof`，stgt/lio → `storager/iscsi`）：

```bash
docker ps    # 预期：storager-agent / kurrent-nvmet-host（或 stgt/lio 对应容器）
```

> 自定义盘目录：编辑 `storager/kurrent.yaml` 的 `spec.agent.diskDir`（宿主路径，如 `/pool1/iscsi_img`）后重跑 `kurrent join --token <token>`（幂等同步 `.env` 的 `KURRENT_DISK_DIR` 并重启 agent 生效）。

**防火墙**（存储节点）：`sudo ufw allow 4840,4421/tcp && sudo ufw reload`

> `4841` 是 nvmet-host 的宿主本地管理口（host 网络，Agent 容器经 `host.docker.internal` 访问），不要向局域网开放；`4421` 是唯一数据面端口，按需限制来源网段。

### 2.4 验证

```bash
docker logs storager-agent 2>&1 | grep pki     # pki: client cert ok (cn=agent-0d26b6f33a89)
docker logs kurrent-nvmet-host 2>&1 | grep -i "数据面端口就绪"   # 4421 tsas=tls1.3
# 回到控制面服务器：
kurrent agents list
# ID             REMARK       HEALTH  BASE_URL                       ROLE  ENABLED  TAGS
# 0d26b6f33a89   storage-01   ok      https://192.168.1.20:4840      disk  true     auto,nvmet,storage
```

## 第 3 步：最小端到端验证

裸机客户端接入 PXE 网段并设为网络启动：

```bash
# 控制面服务器：观察 DHCP/引导流
docker logs -f kurrent-dnsmasq 2>&1 | grep -E "DHCPACK|pxe|ipxe"
```

预期链路：客户端 DHCP 获取地址 → TFTP 拉取引导固件 → iPXE 经 HTTPS 拉取引导脚本 → 设备身份上报与挑战-应答验签 → **设备自动登记入池**（WebUI → Devices 可见）。设备绑定、系统盘创建与 Worker 交付见[使用流程](./usage)。

## 故障排查

| 症状 | 排查 |
| --- | --- |
| `agents list` HEALTH 非 ok | 控制面能否访问 `spec.agent.advertiseUrl`（存储节点 4840 端口/防火墙）；存储节点 `docker logs storager-agent`（证书轮换失败多与 PKI 目录权限有关） |
| join 报 token 无效/已过期 | token TTL（7 天）过期或从未签发——控制面重新 `kurrent token create` 后重跑 `kurrent join --token <新token>`（token 不绑节点，不区分节点/组件） |
| DHCP 无应答 | `dnsmasq.conf` 的 `interface=` 是否对应真实网卡（`ip a` 核对）；防火墙 67/69 UDP |
| Agent 容器反复重启 | `docker logs storager-agent`；多为控制面不可达（`controlPlane.url` 443 未放通）或引导凭据文件缺失（`kurrent join --token <token>` 重写 agent.token；nvmet-host.token 可删后重启 agent 容器触发重新派生） |
| 控制面 503 / enroll 失败 | nginx 443 与 `/api/cp` 反代是否正常；控制面 `curl -k https://<cp-ip>/api/cp/healthz` |
| 数据面端口未就绪 / 引导被拒 | 存储节点宿主是否带 NVMe/TCP TLS 内核配置、`tlshd` 是否运行；`docker logs kurrent-nvmet-host` 查 `4421` 建口日志；`/capabilities` 的 `port.ready` 应为 true |

## 附录：声明配置速查

**`control_plane/kurrent.yaml`**（`kurrent config print init-defaults` 生成模板后编辑，`kurrent init` 校验启动）：`spec.networking` 五键（interface/subnet/dhcpRange/gateway/dns）+ `pki`/`serverCert`/`boot`/`agentTimeoutSec`/`dnsmasq.reload` 默认块——控制面权威文件，dnsmasq.conf 由其派生。

**`storager/kurrent.yaml`**（`kurrent config print node-defaults` 生成模板后编辑，`kurrent join` 校验收敛）：`spec.agent`（backend/advertiseUrl/diskDir/nqnBase）+ `spec.controlPlane.url`——节点权威文件，NQN 命名域以本文件的 `nqnBase` 为唯一来源（控制面经 capabilities 上报发现，Host NQN 与盘 NQN 自动同域）。

两者均为运行时文件（不入库），模板见对应 `kurrent.yaml.example` 或 `kurrent config print`。
