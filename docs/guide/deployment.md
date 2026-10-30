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
| 4443 | TCP | Secure Boot 引导入口（独立 boot CA，可选 L2 档） | 控制面服务器 |
| 4840 | TCP | 存储 Agent HTTPS（控制面 → Agent） | 存储节点 |
| 4421 | TCP | NVMe/TCP 数据面，单口 **TLS 1.3**（固件引导段与内核段共用） | 存储节点 |

nvmet-host 管理面不占 TCP：Agent 与 nvmet-host 经共享卷 unix socket `/run/kurrent/nvmet/nvmet-host.sock` 通信（mTLS，目录 0700），局域网不可达。

443 的引导链只反代 `/boot-vars`、`/devices/report`、`/devices/challenge` 三个只读端点；其余 `/devices/*` 管理接口不对外暴露。控制面管理面不发布 TCP 端口，只监听容器内 unix socket（`/run/kurrent/cp.sock`），由 nginx 经共享卷访问，容器健康检查走同一 socket。nginx 对所有反代 location 默认清空身份头，仅在 `/api/cp/` 的 mTLS 校验通过时注入 DN/issuer/serial/fingerprint。控制面交互式 API 文档（`/openapi.json`、`/docs`）默认关闭，仅调试时以 `KURRENT_CP_OPENAPI=1` 开启。

### 引导网络边界（L1 信任档）

引导链的第一跳（DHCP → TFTP `snponly.efi` → TFTP `boot.ipxe`）没有可用的密码学锚：设备固件不会验证这段内容，iPXE 也尚未拿到控制面证书。当前的信任档位是 **L1：把引导网段本身作为信任边界**，在此之上保留设备身份 TOFU（NVRAM 密钥 + 注册窗口 + 绑定）与服务端 TOFU。部署时必须满足：

- **物理/逻辑隔离**：PXE 网段为独立 VLAN 或独立物理口，不与办公网、存储数据面混跑；控制面是该网段唯一提供 DHCP 与 TFTP 的主机。
- **二层防护**：接入交换机启用 DHCP snooping（仅控制面上联口为 trusted）、Dynamic ARP Inspection、RA guard、端口安全/MAC 数量限制；有条件时用 802.1X 或 MAB 限定可接入端口。
- **入池控制**：注册窗口按需开启并设放行额度；关闭窗口后未知 MAC 不会入池。
- **吊销闭环**：`DELETE /devices/{mac}` 会在 dnsmasq 绑定文件写入 `MAC,ignore`，被吊销设备拿不到租约；重新登记（`/devices/report`）或清除条目（`DELETE /devices/revoked`）会移除该行。
- **TFTP 面最小化**：TFTP 根只挂载 `snponly.efi` 与 `boot.ipxe`，配置与菜单经 HTTPS 取回。

**残余风险（L1 明确不解决）**：同一二层内的攻击者（或与被控端口同 VLAN 的主机）仍可伪造 DHCP/ARP 抢在控制面之前应答，替换首跳二进制或指针，再用设备 NVRAM 中的密钥作为签名预言机取走 `/boot-vars` 中的 DHHC-1/PSK；控制面或 TFTP 服务本身被攻陷时同理。设备密钥只是持有者凭证，控制面认证的是"持有该密钥的请求"，不是"运行该密钥的代码"。L2（Secure Boot + 自签 iPXE + 固定信任锚）以可选档提供，见 1.8，默认不启用；L3（TPM 度量启动/远程证明）不在当前实现范围内。

## 前置条件

**Linux 服务器**（控制面与存储节点宿主，x86_64；已在 Debian 12/13 与 Ubuntu 24.04/26.04 上验证，自测可同机）：

```bash
# 1. Docker 与 Compose v2
docker --version && docker compose version

# 2. 存储节点：nvmet 宿主前置（内核模块 + configfs + tlshd 版本 + 重启持久化；幂等）
sudo bash storager/nvmeof/host-prepare.sh
#    只读诊断（无需 root，部署前排查用）：bash storager/nvmeof/host-prepare.sh --check

# 3. 存储节点：NVMe/TCP TLS 前置——内核需带 CONFIG_NVME_TCP_TLS /
#    CONFIG_NVME_TARGET_TCP_TLS / CONFIG_NVME_KEYRING，且宿主常驻 tlshd >= 1.5.0
grep -E 'CONFIG_NVME_(TCP_TLS|TARGET_TCP_TLS|KEYRING)' /boot/config-$(uname -r)
sudo apt install -y ktls-utils        # 发行版包低于 1.5.0 时见 storager/nvmeof/nvmet-host/README.md 源码安装
sudo systemctl enable --now tlshd

# 4. 存储节点：数据盘建议 btrfs 或 xfs（两者均支持 reflink/FICLONE，母盘克隆秒级完成；
#    ext4 不支持 reflink，会回退全量拷贝，克隆时间随镜像大小线性增长）
#    挂载到 /pool1（示例）：sudo mkfs.btrfs /dev/sdb && sudo mount /dev/sdb /pool1
```

> `host-prepare.sh` 与 `kurrent join` 预检 nvmet 模块、configfs 与 tlshd（**≥ 1.5.0 且服务运行中**）；第 3 步的内核 TLS 编译项仍需手工确认。tlshd 低于 1.5.0 会让轮换后的旧 PSK 遮蔽新 PSK（1.5.0 起进程 keyring 优先），脚本按失败处理。数据面 `4421` 建口失败时节点如实上报端口未就绪，控制面**拒绝引导**，不会回落明文。

**宿主基线（认证体系成立的前提）**：默认只读检查对外 TCP 监听、Docker 暴露面与凭据文件权限；脚本不修改系统状态。

```bash
bash control_plane/host-baseline.sh
```

- 控制面宿主只应暴露 22（运维）、53/67/69（dnsmasq）、80/443（nginx）；存储节点另有 4840/4421
- nvmet-host 管理面走共享卷 UDS（目录 0700），不产生 TCP 监听；`--fix-4841` 已不再需要
- ufw 不过滤 Docker 发布端口：需要入站限制时在 `DOCKER-USER` 链收口（脚本在 ufw 已启用但该链为空时给出 warn）
- 控制面容器不挂载 `docker.sock`（dnsmasq 自己监听 `dhcp-hostsfile`，nginx 由容器内 watcher 自行 reload）
- CA 私钥、`users.yml`、`sessions.yml`、`admin.conf`、`audit.jsonl` 应为 600
- 脚本报 FAIL 时先处理再做安全评估：宿主 root 与 nginx 进程被攻破等同管理面沦陷

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
curl -k https://127.0.0.1/healthz           # {"status":"ok"}（经 443；管理面无 TCP 端口）
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
   → 换取 HttpOnly 会话 Cookie（12 小时 TTL、剩余不足一半才滑动续期，刷新/重开浏览器保持登录；侧边栏
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
kurrent token create --agent-id node-01 --cp-url https://192.168.1.10
# 省略 --agent-id 则生成随机 12 位 hex 节点身份，命令同样携带
```

`--cp-url` 指定**存储节点侧可达的控制面 HTTPS 入口**（443 `/api/cp` 反代，Agent 经此 enroll/renew；缺省按 `--server` 主机推导）。`--agent-id` 是节点身份（= 节点声明 `metadata.name`），token 与它绑定。输出形如（含地址与节点 id、可直接粘贴的节点侧命令）：

```
bootstrap token: a1b2c3.d4e5f6a7b8c9d0e1（expires ...，绑定节点 node-01，0/1）

# 在绑定节点上执行（--agent-id 写入节点声明 metadata.name）：
kurrent join https://192.168.1.10 --token a1b2c3.d4e5f6a7b8c9d0e1 --agent-id node-01
# 加入后验证：kurrent agents list（或 WebUI「Agent 列表」health=ok）
```

> token **绑定节点身份且默认一次性**：enroll 只接受该 `agent_id`，使用额度在签发前原子占用（签发失败退回），TTL 默认 60 分钟、上限 24 小时（`spec.pki.bootstrapTokenTtlMinutes`）；批量引导用 `kurrent token create --agent-id ... --uses N`（上限 100），过期或被吊销后重新签发。**nvmet-host 组件凭据无需签发**——agent enroll 上报 `backend=nvmet` 时控制面自动派生下发。

### 1.6 组件面证书吊销（F-11）

组件通道（443 enroll、4840 入站、控制面出站、agent → nvmet-host UDS）在 TLS 链校验之外做应用层吊销校验：

- 控制面生成两张组件 CRL：`state/pki/nginx/nodes.crl`（nodes CA，校验同伴组件）与 `control.crl`（control CA，校验控制面客户端）；nginx 另用 `clients.crl` 管管理面握手。
- 组件用自身 client cert 从 `/api/cp/enroll/crl` 取表，默认每 60 秒刷新（`KURRENT_COMPONENT_CRL_REFRESH_SECONDS`）；控制面出站与 agent → nvmet-host 在每次建连时加载 `nodes.crl` 并检查 `nextUpdate`。
- **fail-closed**：CRL 缺失或过期时组件/出站连接直接拒绝，不降级放行；`nodes.crl` / `control.crl` 的 `nextUpdate` 为 30 天，容忍控制面长时间不可达。
- 吊销入口：`DELETE /api/cp/pki/components/{agent_id}`（nodes 域）与 `DELETE /api/cp/pki/control/{cn}`（control 域），均可加 `serial` 精确到一张。
- **升级顺序**：先升级并重启控制面（生成 CRL、开放分发端点），再重建组件容器；旧组件不认识 `/enroll/crl` 时会保留原有链校验行为，不会因此拒绝启动。
- 生效时间 = 控制面写入 CRL 后的下一次组件刷新（≤60 秒），无需重启组件。
- **启动自检**：被吊销的证书无法完成 `/enroll/crl` 的 mTLS，组件改为在启动时从公开只读入口 `GET /crl/nodes.crl`（无认证、仅 GET/HEAD）取表，用本地 nodes CA 校验签名后比对 client / serving 序列号，命中即用引导凭据重新 enroll（`KURRENT_PKI_SELF_CHECK=0` 可关闭）。
- **管理入口**：WebUI「证书」页按 users / nodes / control 三个域展示到期与状态，可吊销人员与节点证书；节点证书异常时同一行提供「恢复」。

**验证**：

```bash
bash tests/deploy/probe_edge.sh      # 含组件 CRL 分发、公开 nodes CRL、control CA 台账与人类证书 403 断言
docker logs storager-agent 2>&1 | grep 'crl:'          # initial fetch ok（revoked 计数 / next_update）
docker exec kurrent-nvmet-host python /app/healthcheck.py; echo $?   # 0
```

### 1.7 组件证书吊销后的恢复

吊销会同时删除该节点的 bootstrap token，因此恢复必须重新签发引导凭据；节点上的旧证书
**不需要手工删除**。

1. 控制面：WebUI「证书」→「存储节点」→「恢复」，或
   `kurrent token create --agent-id <id> --cp-url https://<cp-host>`。WebUI 预填控制面
   地址，签发后显示一次性 token 与 join 命令（明文仅本次可见）。
2. 节点：执行输出的 `kurrent join <cp-url> --token <token> --agent-id <id>`。
3. 启动自检：agent 发现本地 client / serving 序列号命中 nodes CRL，用新 token 重新
   enroll 并覆盖本地证书；随后 join 等待派生凭据写出并自动重启 nvmet-host。

验证：

```bash
kurrent agents list                       # 节点 health=ok
docker logs storager-agent 2>&1 | grep 'pki:'   # 重新 enroll / 自检记录
docker logs kurrent-nvmet-host 2>&1 | grep 'pki:'
```

### 1.8 Secure Boot 引导档（可选，L2）

L1 仍是默认行为。L2 增加两个独立锚：UEFI `db` 验证第一段 EFI 代码，固件内嵌的 boot CA 验证引导 HTTPS。控制面启动时自动生成（与组件三分根分离）：

- `state/pki/ca/boot/ca.crt|ca.key`：boot CA，10 年，只签引导端点证书。
- `state/certs/boot-server.crt|boot-server.key`：4443 叶证书，1 年，SAN 复用 `spec.serverCert.san`；控制面每次启动检查剩余有效期，不足 30 天重签（不换 boot CA，固件无需重刷）。

nginx 的 4443 server 只反代 `/boot-vars`、`/devices/report`、`/devices/challenge`，并静态提供 `/file/`（Secure Boot 脚本已全部嵌入签名固件，4443 不暴露 `/tftp/`）；443 的证书与 TOFU 行为完全不变。宿主端口用 `KURRENT_BOOT_HTTPS_PORT` 调整，必须与固件构建的 `BOOT_HTTPS_PORT` 一致——端口烘在签名镜像里，`/boot-assets` 的 `secureboot.port_matches` 核对这一点。

一次性预置流程：

1. 离线生成产物签名密钥：`tools/secureboot/keygen-offline.sh ~/offline/kurrent-sb`（私钥 0600，脚本拒绝写入仓库或容器）。
2. 启动控制面，导出 `state/pki/ca/boot/ca.crt`；按固件仓 `docs/secureboot.md` 用 `SB_KEY` / `SB_CERT` / `BOOT_CA_CERT` 构建 `snponly-sb.efi`。
3. 把 `dist/secureboot/snponly-sb.efi` 与 `manifest.json` 放到宿主 `tftp/secureboot/`，`/boot-assets` 的 `secureboot` 段应变为 `ok`。
4. 预置 `db`（二选一）：
   - **USB/BIOS（受支持路径，无需 BMC）**：`tools/secureboot/export-usb.sh <usb> <artifact-signing.crt> <snponly-sb.efi> <boot-ca.crt>` 生成导入包（只含公开证书），目标机 Key Management 导入 `db-cert.cer`。
   - **Redfish（可选，需 BMC 与厂商支持）**：`KURRENT_BMC_USER=... KURRENT_BMC_PASSWORD=... tools/secureboot/enroll-redfish.sh --bmc <bmc> --cert <artifact-signing.crt> --enable-secureboot`。脚本先读 Secure Boot 与 `db`、确认已有 Microsoft UEFI CA，再幂等导入证书并读回指纹；`--dry-run` 只读，`--credential-file` 接受 0600 文件。退出码 0 成功/跳过、1 前置不满足、2 导入后无法校验、3 厂商不支持或传输失败、4 写入失败。该工具仅完成协议级验证（模拟 BMC），厂商实现须逐型号确认；不支持时改用 USB/BIOS，不要静默降级。
5. 目标机：关 CSM、开 Secure Boot；用 `tools/secureboot/check-host.sh <artifact-signing.crt>` 复核 Secure Boot 状态与 `db`，然后从本地介质或 HTTP Boot 启动签名镜像。

金盘侧还需发行版签名加载器：本项目 Ubuntu 26.04 金盘的 ESP 使用 `BOOTX64.EFI`（Microsoft UEFI CA 2011 签名的 shim）→ `grubx64.efi`（Canonical 签名的 GRUB）→ Canonical 签名的内核。目标机 `db` 必须包含 Microsoft UEFI CA 2011（多数服务器默认具备）；若金盘仍是未签名的 GRUB，固件会在 `sanboot` 之后拒绝引导。

WebUI「引导链」在存在 SB 产物时多一行 Secure Boot，无产物时该行隐藏。边界：Secure Boot 只覆盖第一段 iPXE；`sanboot` 之后的加载器（shim/GRUB/内核）由发行版自身的签名链决定，金盘若未安装发行版签名的加载器，固件会拒绝继续引导。boot CA 轮换等于固件重刷；不做 TPM、防回滚与 BMC 防护；部分 PXE 固件不校验下载的 NBP，优先使用本地介质。

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

节点声明式配置为 `storager/kurrent.yaml`（yml 即声明）。**节点无需预编辑任何文件**——签发命令已携带控制面地址与节点 id，粘贴执行即可：

```bash
cd /path/to/kurrent
kurrent join https://192.168.1.10 --token a1b2c3.d4e5f6a7b8c9d0e1 --agent-id node-01
```

`kurrent join` 完成三件事（幂等可重跑）：
1. 生成/更新 `storager/kurrent.yaml`（缺失时按模板自动生成：`metadata.name` = `--agent-id`、地址同步进 `spec.controlPlane.url`；已存在则读入合并——非 forbid 式，手工编辑保留；已声明身份与 `--agent-id` 不一致即拒绝。人工输入只需写 `metadata.remark` 备注——随首次 enroll 登记进控制面台账，WebUI 卡片展示、编辑表单可改）
2. 写入绑定身份的引导凭据 `storager/bootstrap/agent.token`（`--token` 给则写入；不给则要求文件已就位。`nvmet-host.token` 由 agent enroll 按能力派生自动生成，无需手工提供）
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

> nvmet-host 管理面走共享卷 UDS（`/run/kurrent/nvmet/nvmet-host.sock`，目录 0700），不占 TCP、局域网不可达；`4421` 是唯一数据面端口，按需限制来源网段。

### 2.4 验证

```bash
docker logs storager-agent 2>&1 | grep pki     # pki: client cert ok (cn=agent-0d26b6f33a89)
docker logs kurrent-nvmet-host 2>&1 | grep -i "数据面端口就绪"   # 4421 tsas=tls1.3
docker exec kurrent-nvmet-host python /app/healthcheck.py; echo $?   # 0（UDS + mTLS + configfs）
ss -H -lnt | grep ':4841 ' || echo "4841 无 TCP 监听（预期）"
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

预期链路：客户端 DHCP 获取地址 → TFTP 拉取 iPXE 与 `boot.ipxe` 指针 → iPXE 经 HTTPS 拉取 `boot.ipxe.cfg` 及后续脚本 → 设备身份上报与挑战-应答验签 → **设备自动登记入池**（WebUI → Devices 可见）。设备绑定、系统盘创建与 Worker 交付见[使用流程](./usage)。

> TFTP 根只挂载 `snponly.efi` 与 `boot.ipxe` 两个文件；`boot.ipxe.cfg` / `menu.ipxe` 只经 HTTPS 提供。更新 `snponly.efi` 后需重建/重启 `kurrent-dnsmasq` 容器（文件级挂载固定 inode）。

## 故障排查

| 症状 | 排查 |
| --- | --- |
| `agents list` HEALTH 非 ok | 控制面能否访问 `spec.agent.advertiseUrl`（存储节点 4840 端口/防火墙）；存储节点 `docker logs storager-agent`（证书轮换失败多与 PKI 目录权限有关） |
| join 报 token 无效/已过期 | token 已过期（默认 60 分钟）、已被使用（默认一次性）、从未签发，或节点 id 与 token 绑定不一致——控制面重新 `kurrent token create --agent-id <节点 id>` 后重跑 `kurrent join --token <新token> --agent-id <节点 id>` |
| DHCP 无应答 | `dnsmasq.conf` 的 `interface=` 是否对应真实网卡（`ip a` 核对）；防火墙 67/69 UDP |
| Agent 容器反复重启 | `docker logs storager-agent`；多为控制面不可达（`controlPlane.url` 443 未放通）或引导凭据文件缺失（`kurrent join --token <token>` 重写 agent.token；nvmet-host.token 可删后重启 agent 容器触发重新派生） |
| 控制面 503 / enroll 失败 | nginx 443 与 `/api/cp` 反代是否正常；控制面 `curl -k https://<cp-ip>/api/cp/healthz` |
| 数据面端口未就绪 / 引导被拒 | 存储节点宿主是否带 NVMe/TCP TLS 内核配置、`tlshd >= 1.5.0` 是否运行（`bash storager/nvmeof/host-prepare.sh --check`）；`docker logs kurrent-nvmet-host` 查 `4421` 建口日志；`/capabilities` 的 `port.ready` 应为 true |
| 引导报 `Error 0x7f22208e`（`Boot from SAN device ... failed`） | 系统盘 GPT 扇区布局与 nvmet 导出块大小不一致：nvmet file backend 按底层 FS 块（典型 4096 B）导出，512 字节扇区 GPT 会被 EDK2 视为无分区。核对 `GET /masters` 的 `gpt` 字段（须为 `4k`），并按 nvmet-host README「盘文件要求」重建 4K 原生 GPT |

## 附录：声明配置速查

**`control_plane/kurrent.yaml`**（`kurrent config print init-defaults` 生成模板后编辑，`kurrent init` 校验启动）：`spec.networking` 五键（interface/subnet/dhcpRange/gateway/dns）+ `pki`/`serverCert`/`boot`/`agentTimeoutSec` 默认块——控制面权威文件，dnsmasq.conf 由其派生。控制面容器不挂载 `docker.sock`：dhcp 绑定文件由 dnsmasq 自己监听，nginx 的 CRL 重载由容器内 watcher 自己完成（控制面经共享卷哨兵确认）。

**`storager/kurrent.yaml`**（`kurrent config print node-defaults` 生成模板后编辑，`kurrent join` 校验收敛）：`spec.agent`（backend/advertiseUrl/diskDir/nqnBase）+ `spec.controlPlane.url`——节点权威文件，NQN 命名域以本文件的 `nqnBase` 为唯一来源（控制面经 capabilities 上报发现，Host NQN 与盘 NQN 自动同域）。

两者均为运行时文件（不入库），模板见对应 `kurrent.yaml.example` 或 `kurrent config print`。
