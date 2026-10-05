# 控制面 API 参考

本文档描述当前 Control Plane 已实现的 HTTP 接口、请求参数、返回结构，以及可直接复制执行的 `curl` 测试命令。

> **API 优先（API-first）设计**：Control Plane 的全部能力都以 REST API 为第一接口——Web 管理界面（WebUI）本身也只是这套 API 的一个客户端，与任何第三方系统、自动化脚本完全平等。调用准则：**一切面向控制面**——第三方集成始终调用 Control Plane API，不绕过控制面直接操作 Agent 或数据面。

Control Plane 是 Controller 节点上的常驻 HTTP 服务，负责：

- 新增 Worker
- 删除 Worker
- 查询 Worker 台账
- 查询 Agent 状态
- 维护 `dnsmasq/dhcp-hosts.conf`
- 调用 Agent 创建或删除 iSCSI LUN

它不负责 iPXE 菜单生成、不负责静态文件分发、不直接操作 `tgtadm`/`targetcli`。

---

## 1. 基本信息

### 1.1 Base URL

对外入口（唯一）：`https://<host>/api/cp`（nginx 443；80 仅 301 升级）。

本地调试（在控制面服务器上直连回环映射）：

```text
http://localhost:4839
```

> 入口使用 TOFU 自签证书（`control_plane/state/certs/server.crt`，控制面启动时自动生成），
> curl 需加 `-k`，或 `--cacert control_plane/state/certs/server.crt` 固定信任该证书本体。

如果你通过别的端口或域名暴露，请替换为实际地址。

### 1.2 环境变量

控制面不解析 `.env` 文件，全部通过容器环境变量（`os.getenv(...)`）读取。仓库根的
`docker-compose.yml` 目前只注入 `TZ`；按需追加的开关（例如开发模式
`KURRENT_CP_INSECURE=1`，仅限本地、生产严禁）直接加到该服务的 `environment:` 下，
或用 `docker compose --env-file` 传入。

### 1.3 鉴权（认证器链）

外部访问统一经 nginx 443 入口（唯一对外入口）；控制面 4839 仅绑本机回环。除
`GET /healthz` 与 iPXE 引导链端点（`/boot-vars`、`/devices/report`、`/devices/challenge`）
外，管理面接口要求认证——三种凭据**串行短试**（首个成功即止；凭据无效即 `401`，不降级）：

| 凭据 | 载体 | 说明 |
|---|---|---|
| mTLS 客户端证书 | 443 双向 TLS | `kurrent init` 签发的 admin 证书（CN=用户名、O=组）；CLI 自动发现 `control_plane/state/admin.conf` 使用（脱离仓库的二进制由 `kurrent credentials install` 装一份自包含快照到 `~/.kurrent`，发现链垫底）。nginx 校验证书链（内部 CA）后透传 DN，控制面按 CN/O 识别身份 |
| 管理 token | `Authorization: Bearer <token>` | `kurrent token create --usage admin` 签发（台账只存 sha256 摘要，TTL 内有效）；供脚本调用与 WebUI 登录 |
| 会话 Cookie | `kurrent_session` | 以证书或管理 token 经 `POST /session` 换取（HttpOnly 签名会话，TTL 7 天滑动续期）；浏览器登录态 |

授权（RBAC 最小版）：读请求（GET）只需**已认证**（不做组校验）；写请求
（POST/PUT/PATCH/DELETE）需 admin 组，否则 `403`。组件通道语义不变：`/enroll/renew` 由
nginx 强制组件证书，引导链端点走各自校验。

开发模式：`KURRENT_CP_INSECURE=1` 时放行全部请求（身份 insecure/admin），仅限本地
开发；生产严禁——不存在“未配置凭据即放行”的空放行路径。

示例：

```bash
# 本机（控制面服务器）：CLI 自动发现 admin.conf，经 443 mTLS 认证
kurrent agents list

# 脚本：管理 token（kurrent token create --usage admin）
export BASE_URL=https://<host>/api/cp
export TOKEN=<admin-token>
```

带鉴权的 curl 写法：

```bash
curl -s "$BASE_URL/workers" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 2. 状态与配置文件

当前 Control Plane 的状态与配置分工如下（容器内路径；宿主对应 `control_plane/state/` 与 `control_plane/config/`）：

| 文件 | 含义 |
|---|---|
| `config/agents.yml` | Agent 节点清单与调度角色 |
| `state/devices.yml` | 设备台账（设备池 / 绑定 / 指纹 / 公钥）——**绑定关系唯一权威**（`bound_worker_id`） |
| `state/workers.yml` | Worker 台账（计算身份 / 系统盘 / 默认启动） |
| `state/credentials.yml` | NVMe-oF 认证密钥库（DHHC-1，按 Worker 跟盘） |
| `state/credential-push.yml` | 凭据推送状态台账（逐 Agent 送达结果，对账与投影用，见 7.7） |
| `state/masters.yml` | 母盘标签登记台账（os / os_version 备注，见 6.4） |
| `state/settings.json` | 运行时设置（注册窗口 / 验签强制开关） |
| `state/operations.jsonl` | 控制面操作轨迹（含 actor） |
| `dnsmasq/dhcp-hosts.conf` | `MAC -> hostname` 绑定（**派生视图**，权威在设备台账） |

说明：

- `workers.yml` **不记录 MAC**——MAC 与绑定关系统一由设备台账持有，Worker 侧只是投影；
- compose 把整个 `dnsmasq/` 目录挂进控制面容器（控制面还要往该目录写 `dnsmasq.conf`）；
- 写 `dhcp-hosts.conf` 采用**截断写**（保持 inode），不用临时文件做 atomic replace：该文件以文件级 bind mount 进 dnsmasq 容器，换掉 inode 后容器读到的仍是旧文件；
- `dnsmasq/dhcp-hosts.conf` 一行一个绑定，格式固定为：

```text
00:0c:29:b9:8b:2d,worker-01
```

---

## 3. 接口概览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/healthz` | 健康检查 |
| `GET` | `/whoami` | 当前请求身份（认证器链结果，见 1.3） |
| `POST` | `/session` | 凭据换会话 Cookie（登录，见 1.3） |
| `DELETE` | `/session` | 登出（清除会话 Cookie，见 1.3） |
| `GET` | `/boot-vars` | iPXE 启动变量动态注入，不鉴权 |
| `GET` | `/devices/report` | iPXE 设备信息上报（不鉴权，11 字段，见 16.6） |
| `GET` | `/devices/challenge` | 一次性 nonce 签发（不鉴权，挑战-响应，见 5.2） |
| `GET` | `/devices` | 设备池列表（state 过滤，见 16.1） |
| `GET` | `/devices/{mac}` | 单设备详情（见 16.2） |
| `POST` | `/devices` | 手动注册设备入池（见 16.3） |
| `POST` | `/devices/import` | 批量导入设备清单（见 16.4） |
| `DELETE` | `/devices/{mac}` | 注销设备（吊销，见 16.5） |
| `GET` | `/settings/registration-window` | 查询注册窗口状态（见 5.1） |
| `POST` | `/settings/registration-window` | 开启注册窗口（TTL 1-60 分钟硬上限；可选放行额度 `max_devices` 1-1000 台，见 5.1） |
| `DELETE` | `/settings/registration-window` | 提前关闭注册窗口（见 5.1） |
| `GET` | `/settings/enforcement` | 查询设备身份验签强制开关（见 5.1） |
| `PUT` | `/settings/enforcement` | 切换设备身份验签强制开关（见 5.1） |
| `GET` | `/agents` | 查询 Agent 列表与能力 |
| `POST` | `/agents` | 注册新 Agent（写入 agents.yml，重复 id 返回 409） |
| `POST` | `/agents/probe` | 探测 Agent 并自动推导注册参数（预览，不写文件） |
| `PUT` | `/agents/{agent_id}` | 更新 Agent 配置（id 不可改） |
| `POST` | `/pki/tokens` | 签发集群级通用 bootstrap token（不绑节点，TTL 内可复用，见 6.5） |
| `GET` | `/pki/tokens` | 列出未过期 token 台账（无明文/hash，见 6.5） |
| `DELETE` | `/pki/tokens/{token_id}` | 吊销 token：删条目即失效（见 6.5） |
| `DELETE` | `/agents/{agent_id}` | 删除 Agent 台账（含母盘标签清理，见 6.6） |
| `GET` | `/agents/{agent_id}/luns` | 列出指定 Agent 上的 iSCSI target/LUN |
| `GET` | `/masters` | 聚合列出全部存储节点上的母盘清单（合并登记标签，供克隆选盘） |
| `PUT` | `/agents/{agent_id}/masters/{master_name}/tag` | 登记母盘标签（os / os_version 备注，见 6.4） |
| `DELETE` | `/agents/{agent_id}/masters/{master_name}/tag` | 清除母盘标签（见 6.4） |
| `POST` | `/agents/{agent_id}/luns/disk` | 在指定 Agent 上创建磁盘 LUN（母盘克隆/空白盘） |
| `POST` | `/agents/{agent_id}/luns/cd` | 在指定 Agent 上创建 CD（ISO 虚拟光驱）LUN |
| `DELETE` | `/agents/{agent_id}/luns` | 删除指定 Agent 上的 LUN/target |
| `POST` | `/agents/{agent_id}/luns/scan` | 触发指定 Agent 扫描镜像目录重建 target |
| `POST` | `/workers` | 注册 Worker 身份（hostname 绑定；`mac` 可选，传了直接绑定设备） |
| `POST` | `/workers/batch` | 批量创建 Worker（数量 + 命名规则，逐项独立，`macs` 可选直接绑定，见 7.6） |
| `POST` | `/workers/{worker_id}/luns/disk` | 给指定 Worker 创建系统盘 LUN |
| `POST` | `/workers/luns/disk/batch` | 批量给多个 Worker 创建系统盘（逐项点名节点，或 `workers` 交给控制面放置；`dry_run` 出计划） |
| `DELETE` | `/workers/{worker_id}/luns/disk/{os_tag}` | 删除 Worker 单个系统盘（保留/删除 .img 文件） |
| `PUT` | `/workers/{worker_id}/default-disk` | 设置 Worker 默认启动配置（默认盘 os_tag / 菜单项 / 超时） |
| `PUT` | `/workers/{worker_id}/mac` | 修改 Worker 的 MAC 绑定（更新 dnsmasq 绑定并 HUP 重载，审计旧/新 MAC，见 7.5） |
| `GET` | `/workers` | 列出 Worker |
| `GET` | `/workers/{worker_id}` | 查询单个 Worker |
| `GET` | `/workers/{worker_id}/status` | 查询 Worker 台账与实时状态 |
| `DELETE` | `/workers/{worker_id}` | 删除 Worker |
| `POST` | `/workers/delete/batch` | 批量删除 Worker（逐项独立，成功/失败汇总） |
| `PUT` | `/workers/{worker_id}/credential` | 设置/轮换 NVMe-oF 认证密钥（DHHC-1 自检；nvmet 建盘已自动签发，见 7.7） |
| `DELETE` | `/workers/{worker_id}/credential` | 删除 NVMe-oF 认证密钥（吊销认证，见 7.7） |
| `GET` | `/workers/{worker_id}/credential` | 查询凭据元数据（不返回明文，见 7.7） |
| `GET` | `/operations` | 读取操作日志 |

---

## 4. GET /healthz

### 说明

健康检查接口，不改状态，不需要鉴权。

### curl

```bash
curl -s "$BASE_URL/healthz"
```

### 成功返回

```json
{"status":"ok"}
```

---

## 5. GET /boot-vars

### 说明

给 iPXE 启动脚本读取 per-worker 启动变量。该接口不鉴权，只暴露受控内网启动所需变量。

> **注意**：该端点**只读**（启动变量投影，无写副作用）。注册窗口期注册（新 MAC 携公钥入设备池）已收敛到 `GET /devices/report`（见 16.6），由 `boot.ipxe.cfg` 在请求 `/boot-vars` 之前先 `chain`。

Control Plane 按以下顺序识别设备并投影启动变量：

1. `hostname` → `state/workers.yml`（hostname 或 worker_id）
2. `mac` → `state/devices.yml`（设备台账）→ `bound_worker_id` → `state/workers.yml`
3. `config/agents.yml`（默认启动盘的 Agent 数据面地址）

然后返回该 Worker 对应的 iSCSI Server、默认菜单项和菜单超时。

默认返回 iPXE 脚本片段，兼容性最好，可以直接被 iPXE `chain` 执行。加 `format=json` 时返回 JSON，方便人工调试。

### 字段来源

`/boot-vars` 返回的是 inventory 的投影：

| 返回字段 | 来源 |
|---|---|
| `base_nqn` | 默认启动盘（同选盘规则）的盘 NQN 前缀（C3 拼接：盘 NQN 由控制面按统一模板 `base:worker_id.os.<os_tag>` 生成，固件侧 `sanboot nvme://<ip>:4420/${base-nqn}:<worker>.<os>.<os-tag>` 拼装消费 = 盘记录权威值）；盘记录缺 `nqn` 时不返回（不兼容遗留、不派生）；Worker 无系统盘时不返回 |
| `base_iqn` | `workers.yml` 中该 Worker 默认启动盘（`default_disk` 对应的盘，未设时取第一块）的 `iqn` 去掉最后一个 `:` 后的前缀——盘标识权威 = NQN，IQN 由盘 NQN 派生；Worker 无系统盘时**不返回**（iPXE 沿用 `boot.ipxe.cfg` 静态默认值） |
| `storager_ip` | 默认启动盘（同上选盘规则）的 `agent` -> `agents.yml` 中该 Agent 的 `storager_ip`（数据面地址，NVMe-oF 引导与 iSCSI 安装器共用）；无系统盘时不返回 |
| `iscsi_sep` | iSCSI root **连接符**（`${storager-ip}` 与 `${base-iqn}` 之间的分隔字段，安装器 iSCSI 拼装消费），root-path 拼装由 iPXE 侧完成。**仅 stgt / LIO 后端返回**：stgt 后端为 `:::1:`（lun 占位 1），LIO 后端为 `::::`（空占位）；后端类型**实时查询 Agent `/capabilities` 的 `backend` 字段**（节点自报，能力即真相），未登记 / 查询失败 / 取值非法则默认 stgt 格式；**不由 `agents.yml` 的 `tags` 推导**（tags 只作人工标签，否则标签与实况漂移时会按过期值拼出错连接符）；**nvmet 后端不下发**（menu 安装器项 `isset ${iscsi-sep}` 守卫跳过）；无系统盘时不返回 |
| `nbft_secret` | 该 Worker 的 NVMe-oF 认证密钥（DHHC-1，`state/credentials.yml` 按 worker_id 索引，见 7.7）；**已绑定 Worker 且密钥库有条目时注入**，无密钥 / 未绑定 / 请求被拒时不返回。固件侧消费：menu 拼 `nvme://...?secret=${nbft-secret}`（C3 已启用：secret 条件化拼装，无密钥走明文连接） |
| `hostnqn` | 该 Worker 的发起端 Host NQN（`KURRENT_CP_NQN_BASE` + `:host.<worker_id>`，与 nvmet-host 登记的 host 条目一致）；**已绑定 Worker 时注入**。固件侧消费：iPXE nvmetcp 默认 hostnqn 为 `nqn.2014-08.org.ipxe:<uuid>`（无 UUID 回退 `:ipxe`），与登记值不匹配时严格模式认证必败，须以本字段覆盖（固件 0011 补丁支持 `hostnqn` 设置） |
| `os` | 默认启动盘（`default_disk` 对应的盘，未设时取第一块）的 os 名（盘记录权威）——2026-08-30 MAIN MENU 动态化：菜单只保留一个通用 OS 项（`boot-os`），固件用本变量拼 `nvme://...:${hostname}.${os}`，支持新系统无需改脚本；与盘 NQN 后缀同源。Worker 无系统盘时不返回（reboot 循环等待建盘） |
| `os_version` | 默认启动盘的版本备注（盘记录 `os_version`，`''` = 无版本）——空值不下发，menu.ipxe 的 `os-label` 拼接自适应（有版本显示 `os version`）。Worker 无系统盘时不返回 |
| `os_tag` | 默认启动盘的随机标识（盘记录 `os_tag`，12 位 hex，数据面唯一键）——固件拼盘 NQN 后缀 `${hostname}.${os}.${os-tag}`（与盘 NQN 同源同值）；Worker 无系统盘时不返回 |
| `menu_default` | 推导链：`workers.yml` 的 `default_disk`（建盘后单独设置，os_tag 引用具体盘）> `boot.menu_default`（显式配置）> `reboot`（未配置时循环重启等待）。2026-08-30 动态化：菜单 OS 项已收敛为唯一通用项 `boot-os`，OS 语义的默认值（`default_disk`，或 `boot.menu_default` 中的旧系统名）统一归一到 `boot-os`；非 OS 导航值（`menu-diag` / `menu-install` / `config` / `shell` / `reboot` / `exit`）原样返回 |
| `menu_timeout` | 已配置默认启动时：`boot.menu_timeout` > `IPXE_CP_BOOT_MENU_TIMEOUT`（默认 5000）；处于 `reboot` 循环时：固定用 `IPXE_CP_AUTO_BOOT_TIMEOUT`（默认 1）。单位均为毫秒 |

查找 Worker 的规则（**hostname 优先**）：

```text
hostname -> workers.yml（hostname 或 worker_id）
hostname 未命中或未传 -> mac -> devices.yml（设备台账）-> bound_worker_id -> workers.yml
未识别且 mac 已传 ->
  - 设备在池中（pooled）-> reboot 循环（menu-default=reboot + 短超时），等待绑定
  - 设备已吊销（revoked）-> 空脚本（菜单停留）
  - 未知 MAC + 注册窗口开启 + 额度未用尽 + 带有效公钥 -> 入设备池（见下「注册窗口期注册」），返回 reboot 循环
  - 未知 MAC + 窗口关闭 / 额度用尽 / 无公钥 -> 空脚本（菜单停留；上报只记指纹不入池）
```

### 默认启动项规则

默认启动项由 `/boot-vars` 按以下顺序推导：

```text
default_disk（建盘后单独设置，见 7.3）-> boot.menu_default（显式配置）-> reboot（未配置）
```

- 推荐做法：创建系统盘后调用 `PUT /workers/{worker_id}/default-disk` 设置默认启动盘（os_tag 引用具体盘）：

```text
disk=<os_tag> -> menu_default=boot-os, os=<该盘 os>, os-version=<该盘 os_version（如有）>, os-tag=<该盘 os_tag>
```

- 2026-08-30 起 MAIN MENU 只保留一个通用 OS 项：`menu_default` 归一到 `boot-os`（`boot.menu_default` 里的旧系统名同样归一），`os` / `os-version` / `os-tag` 字段承载通用项消费的盘标识

- 也可以不设置 `default_disk`，改用 `boot.menu_default` 指定 iPXE 菜单默认项（如安装期 `menu-install`、退出 `exit`）
- 两者都没有时，`menu_default` 返回 `reboot`（短超时循环重启，等待管理员建盘 / 设置默认盘；`exit` 仅出现在显式设置时）

### 注册窗口期注册（Zero-touch Provisioning）

新设备开机时没有身份，iPXE 先 `chain` `/devices/report`（11 字段上报 + 可选 `pubkey`，见 16.6）再请求 `/boot-vars`。**注册只在窗口期**（2026-08-21 裁定）：仅当注册窗口开启、放行额度未用尽且上报携带有效 ECDSA P-256 公钥时，未知 MAC 才被收进设备池；窗口关闭后上报只更新指纹、不入池（无窗口外注册通道，代码层不可配永久）。

1. `GET /devices/report`：未知 MAC + 窗口开启 + 额度未用尽 + 带 `pubkey` → 写入 `state/devices.yml`（`state=pooled`，指纹入库，`key_hash` 填充，`source=ipxe`）
2. `GET /boot-vars`：MAC 在池中未绑定 → 返回 `menu-default=reboot` + 短超时，循环重启等待管理员绑定
3. 管理员将设备绑定到 Worker（WebUI / API，单绑 16.7、批量预览/执行 16.9/16.10）后，下次启动即按 Worker 配置正常引导

存量设备密钥认领：已入池设备在窗口期内带公钥上报即填充 `key_hash`（认领完成）；密钥不一致拒绝覆盖（吊销/重注册走删登记流程）。认领只看窗口期、不受放行额度约束。认领完成后建议开启验签强制（见 5.1）。

控制项：

| 项 | 默认 | 说明 |
|---|---:|---|
| 注册窗口 | 关闭 | 部署期用 `POST /settings/registration-window` 开启（TTL 1-60 分钟硬上限，到期自动关闭），见 5.1 |
| 放行额度 | 不限 | 开窗时可选 `max_devices`（1-1000 台），窗口期内自动入池设备数达到上限即停止放行，见 5.1 |
| 验签强制 | 关闭 | `GET/PUT /settings/enforcement`：开启后 `/boot-vars` 只向验签通过的已绑定设备下发，见 5.1 |
| `IPXE_CP_AUTO_BOOT_TIMEOUT` | `1` | reboot 循环的菜单超时（毫秒） |

注册全程有操作日志（`device.register` / `device.claim`），失败回滚台账并返回空脚本，下次请求重试，不影响 iPXE 引导。

### 防冒领（绑定即认证）

请求带 `mac` 时，Control Plane 会校验该设备**绑定到了 hostname 命中的 Worker**（`bound_worker_id`）；不符合（绑定其他 Worker / 未绑定 / 未知设备）→ **拒绝下发**：返回空脚本，不泄露启动变量。不带 `mac`（仅 hostname）的请求无法校验身份，保持兼容放行。这使设备↔Worker 绑定成为开机时的认证边界：只有绑定的设备才能拿到该 Worker 的启动配置（如 `base_nqn` / `storager_ip`）。

### Query 参数

| 参数 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `mac` | 否 | 无 | MAC 地址。后端自动剥离 `:` / `-` / `.` 后归一化，带冒号（`00:0c:29:b9:8b:2d`）与 `mac:hexraw`（`000c29b98b2d`）格式都支持 |
| `hostname` | 否 | 无 | 主机名，如 `worker-01` |
| `format` | 否 | `ipxe` | `ipxe` 或 `json` |
| `nonce` | 否 | 无 | 挑战-响应一次性 nonce（`/devices/challenge` 签发，见 5.2）；验签强制开启时缺失 → `missing_sig` 拒绝 |
| `sig` | 否 | 无 | ECDSA P-256 签名 base64(DER)，签名数据 `nonce‖mac‖hostname` 无分隔拼接（见 5.2）；验签强制开启时缺失 → `missing_sig` 拒绝。**兼容还原**：iPXE 拼 URL 不做百分号编码，base64 的 `+` 原样进入 query 后被按 form-urlencoded 解码为空格，服务端将空格还原为 `+` 再验签 |

`mac` 和 `hostname` 至少建议传一个。iPXE 端推荐两个都传：

```text
/boot-vars?mac=${mac}&hostname=${hostname}
```

> **注意**：规范上 `${mac:hexraw}` 与 `${mac}` 等价（后端统一归一化），但部分真实 iPXE 固件对 `hexraw` 修饰符展开异常（可能为空），实测必须使用带冒号的 `${mac}`——请勿改回 `hexraw`。

### iPXE 格式 curl

```bash
curl -s "$BASE_URL/boot-vars?mac=000c29b98b2d&hostname=worker-01"
```

成功返回示例：

```ipxe
#!ipxe
# boot vars for worker-01
set base-nqn nqn.2026-07.com.kurrent
set base-iqn iqn.2026-07.com.kurrent
set storager-ip 192.168.80.3
set iscsi-sep :::1:
set menu-default boot-os
set menu-timeout 5000
set os ubuntu
set nbft-secret DHHC-1:01:<base64>   # 仅 NVMe-oF 认证启用时返回（见 7.7）
set hostnqn nqn.2026-07.com.kurrent:host.worker-01
```

已注册但未配置默认启动（无系统盘 / 未设 `default_disk` / 未显式设 `boot.menu_default`）时返回：

```ipxe
#!ipxe
# boot vars for worker-01
set menu-default reboot
set menu-timeout 1
```

新 MAC（窗口期注册）与完全无法识别时，窗口关闭或无公钥则返回空脚本：

```ipxe
#!ipxe
# no per-worker boot vars found
```

### JSON 格式 curl

```bash
curl -s "$BASE_URL/boot-vars?mac=000c29b98b2d&hostname=worker-01&format=json"
```

成功返回示例：

```json
{
  "base_nqn": "nqn.2026-07.com.kurrent",
  "base_iqn": "iqn.2026-07.com.kurrent",
  "storager_ip": "192.168.80.3",
  "iscsi_sep": ":::1:",
  "menu_default": "boot-os",
  "menu_timeout": 5000,
  "os": "ubuntu",
  "nbft_secret": "DHHC-1:01:<base64>",
  "hostnqn": "nqn.2026-07.com.kurrent:host.worker-01"
}
```

已注册但未配置默认启动时返回：

```json
{
  "menu_default": "reboot",
  "menu_timeout": 1
}
```

无法识别且未注册（窗口关闭 / 无公钥）时返回：

```json
{}
```

### iPXE 接入方式

`tftp/boot.ipxe.cfg` 中，信任根链在挑战-应答握手后**经 HTTPS（nginx 443，TOFU 信任锚）**拉取该端点（见 5.2）：

```ipxe
# 阶段 3：挑战-应答（R7）——签名请求携带 nonce+sig
:challenge
chain --autofree ${https-url}devices/challenge?mac=${mac} && goto signed || goto bootvars

:signed
sign ${nonce}${mac}${hostname} || goto bootvars
chain --autofree ${https-url}boot-vars?mac=${mac}&hostname=${hostname}&nonce=${nonce}&sig=${sig} || goto vars-done
goto vars-done

# 降级路径（challenge 失败：未注册/未认领，或旧固件）：
# 验签强制关闭期间放行；强制开启后 missing_sig 拒绝
:bootvars
chain --autofree ${https-url}boot-vars?mac=${mac}&hostname=${hostname} || goto vars-done

:vars-done
isset ${iscsi-sep} || set iscsi-sep :::1:
isset ${hostname} && set initiator-iqn ${base-iqn}:${hostname} || set initiator-iqn ${base-iqn}:${mac}
```

成功后返回的 `base-nqn` / `storager-ip` 可能覆盖静态默认值，派生变量在 `:vars-done` 重建；`isset` 守卫保留 `/boot-vars` 按后端下发的连接符（stgt `:::1:` / LIO `::::`）。

`menu.ipxe` 主引导项用 NVMe-oF 拼装：`set root-path nvme://${storager-ip}:4420/${base-nqn}:${hostname}.<os>`，已注入 `nbft-secret` 时附加 `?secret=`（`isset` 条件化）；安装项保持 iSCSI 拼装（`set root-path iscsi:${storager-ip}${iscsi-sep}${base-iqn}:${hostname}.<os>`），`iscsi:` 协议头与拼装结构保持静态，仅连接符由后端投影。

### Agent 数据面地址

`/boot-vars` 返回的是 Worker 连接存储的 **数据面地址**（NVMe-oF 引导 / iSCSI 安装器共用），不是 Agent HTTP API 地址。建议在 `config/agents.yml` 里显式配置：

```yaml
agents:
  storage-lio-01:
    base_url: http://host.docker.internal:4840
    storager_ip: 192.168.80.3
```

如果没有配置 `storager_ip`，Control Plane 会退回使用 `base_url` 的 host 部分；但当 `base_url` 是 `host.docker.internal` 时，这个值不适合给物理 Worker 使用。

### 5.1 注册窗口与验签强制（/settings/registration-window、/settings/enforcement）

#### 说明

注册窗口是部署期的受控注册通道（2026-08-21 裁定取代原 `auto-register` 永久开关，旧端点已删除）：仅窗口开启期间，新设备上报（`GET /devices/report`）可携公钥自动注册入池并完成密钥认领；窗口关闭后上报只记指纹不入池（无窗口外注册通道）。TTL 硬上限 60 分钟（代码层不可配永久），到期自动关闭（懒计算，状态以查询结果为准）。可另设**放行额度**（`max_devices`，台）：窗口期内自动入池的设备数达到上限即停止放行（不主动清窗口记录），额度只约束未知设备自动入池——密钥认领与管理员手动登记/导入不受其约束；已放行数派生自设备台账，不另存计数器。全部端点需 `Authorization: Bearer`。

验签强制开关（过渡期兼容）：关闭时无密钥设备照现状放行（防冒领边界）；开启后 `/boot-vars` 对已绑定设备执行注入四条件第 4 条——无 `key_hash` → `no_key` 拒绝；缺 `nonce`/`sig` → `missing_sig` 拒绝；重放或签名无效 → 拒绝。已认领设备带无效签名在过渡期同样拒绝（伪造签名不放行）。建议全部存量设备完成密钥认领后再开启。

#### GET /settings/registration-window

查询注册窗口状态（无记录/已过期 → `open=false`）。时间维度与额度维度并列投影：

| 字段 | 类型 | 说明 |
|---|---|---|
| `open` | bool | 窗口是否开启（时间维度；额度用尽不影响该值） |
| `opened_at` | str | 开启时刻 ISO8601（未开启为 `null`） |
| `ttl_minutes` | int | 开启时配置的 TTL 分钟数（未开启为 `null`） |
| `closes_at` | str | 预计关闭时刻 ISO8601（未开启为 `null`） |
| `remaining_seconds` | int | 剩余秒数（未开启为 `0`） |
| `max_devices` | int \| null | 放行额度（台）；`null` = 不限 |
| `admitted` | int | 窗口期内已放行设备数（派生值，见下） |
| `remaining_devices` | int \| null | 剩余额度；`null` = 不限 |
| `quota_exhausted` | bool | 额度是否已用尽（用尽后新设备不再入池，窗口记录不主动清除） |

`admitted` 为**派生值**，不存计数器：窗口期内设备台账中 `source == "ipxe"` 且 `first_seen >= opened_at` 的条目数。因此管理员手动登记（`POST /devices`，`source=manual`）与清单导入不占额度，窗口开启前的存量设备（含仅认领密钥者）也不计入。

```json
{"open": true, "opened_at": "2026-08-21T10:00:00+08:00", "ttl_minutes": 30, "closes_at": "2026-08-21T10:30:00+08:00", "remaining_seconds": 1799, "max_devices": 20, "admitted": 3, "remaining_devices": 17, "quota_exhausted": false}
```

#### POST /settings/registration-window

开启注册窗口（状态码 201）。已开启 → 409（先关闭再开）；过期残留记录可直接重开（覆盖），重开后额度按新的开启时刻重新派生。写入操作日志（`settings.registration_window`）。

**请求体**：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `ttl_minutes` | 是 | int | 1-60（超出返回 400） |
| `max_devices` | 否 | int \| null | 放行额度（台），1-1000（超出返回 400）；省略/`null` = 不限，仍受 TTL 约束 |

```bash
# 只限时
curl -X POST "$BASE_URL/settings/registration-window" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ttl_minutes": 30}'

# 限时 + 限台数：最多放行 20 台新设备入池
curl -X POST "$BASE_URL/settings/registration-window" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ttl_minutes": 30, "max_devices": 20}'
```

**响应**：同 GET（开启后的窗口状态）。

#### DELETE /settings/registration-window

提前关闭注册窗口（从未开启/无记录 → 409）。写入操作日志（`settings.registration_window`）。

```bash
curl -X DELETE "$BASE_URL/settings/registration-window" \
  -H "Authorization: Bearer $TOKEN"
```

**响应**：`{"open": false}`

#### GET/PUT /settings/enforcement

查询/切换设备身份验签强制开关（持久化到 `state/settings.json`，重启保留）。

**请求体（PUT）**：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `enabled` | 是 | bool | `true` = 开启强制（无密钥/无签名设备拒绝引导） |

```bash
curl -X PUT "$BASE_URL/settings/enforcement" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

**响应**：`{"enabled": bool}`（GET/PUT 相同）。PUT 写入操作日志（`settings.enforcement`）。

### 5.2 GET /devices/challenge

挑战端点（**不鉴权**）：为 `/boot-vars` 验签链路签发一次性 nonce（短 TTL、绑定 MAC、防重放，nonce 本身无秘密）。设备不存在或未认领（无 `key_hash`）→ **404**（无法走验签链路）。

**Query 参数**：

| 参数 | 必填 | 说明 |
|---|---|---|
| `mac` | 是 | MAC 地址（归一化规则同 `/boot-vars`） |

**成功返回（200，`text/plain`）**：`#!ipxe` 脚本体——iPXE 用 `chain` 直接消费，执行后 `${nonce}` 即为 64 hex 字符一次性 nonce：

```ipxe
#!ipxe
set nonce 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
```

**错误**：`400`（非法 MAC）；`404`（设备不存在/未认领）。

**挑战-响应链路（与 `/boot-vars` 配合，设备身份验签）**：

1. `GET /devices/challenge?mac=<mac>` → 执行响应脚本后取得 `${nonce}`
2. iPXE 用设备密钥对 `nonce‖mac‖hostname`（UTF-8 无分隔拼接，mac 小写冒号格式）做 ECDSA P-256 签名 → `sig`（base64(DER)）
3. `GET /boot-vars?mac=&hostname=&nonce=&sig=` → 验签通过才下发凭据（注入四条件第 4 条，见 5.1）；验签失败 → 空脚本（拒绝下发，审计 `boot_vars.credential`，reason 见 5.1）

---

## 6. GET /agents

### 说明

列出 `config/agents.yml` 里配置的 Agent。默认会实时访问 Agent 的 `/healthz` 和 `/capabilities`。

`health` 三态：`ok`（健康）；`degraded`（agent 在线但数据面未就绪——nvmet 后端为宿主服务不可达或 configfs 未就绪，具体原因见 `error` 字段）；`error`（agent 不可达或能力探测失败）。`live=false` 时不探测、无 `health` 字段。

身份分层：`id` 为机器身份（正常流程由 `kurrent join` 自动生成随机短标识——12 位 hex，与盘 os_tag 同构，enroll 自动登记；手工登记见 6.1，示例中的可读 id 仅为示意）；`remark` 为备注（人类识别用，来自节点声明 `metadata.remark`，WebUI 卡片展示、编辑表单或 6.3 可改）。

### Query 参数

| 参数 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `live` | 否 | `true` | 是否实时探测 Agent 状态与能力 |

### curl

实时探测：

```bash
curl -s "$BASE_URL/agents?live=true" \
  -H "Authorization: Bearer $TOKEN"
```

只看配置，不探测：

```bash
curl -s "$BASE_URL/agents?live=false" \
  -H "Authorization: Bearer $TOKEN"
```

### 成功返回示例

```json
[
  {
    "id": "storage-lio-01",
    "base_url": "http://10.0.0.11:4840",
    "role": {
      "disk": true,
      "cd": false
    },
    "enabled": true,
    "tags": ["storage"],
    "remark": "主存储节点",
    "health": "ok",
    "capabilities": {
      "backend": "lio",
      "fs_type": "btrfs",
      "cd": false,
      "persistent": "saveconfig (auto-load on start)",
      "base_nqn": "nqn.2026-07.com.controller",
      "clone": "reflink (FICLONE) -> shutil.copy fallback",
      "empty_disk": "truncate (sparse)"
    }
  }
]
```

---

## 6.1 POST /agents

### 说明

注册新 Agent：写入 `config/agents.yml`，注册后立即生效（建盘/挂载调度即会纳入该 Agent）。同一 `id` 重复注册返回 `409`。

**推荐流程**：先在 WebUI（或 `POST /agents/probe`，见 6.2）填写 API 地址并探测，自动获取角色 / 标签 / 数据面地址等参数，确认后调用本接口完成注册；也可直接全参数提交。

### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `id` | 是 | Agent 编号。自动转小写，规则同 worker id（字母、数字、点、下划线、短横线） |
| `base_url` | 是 | Agent 控制面 API 地址，须以 `http://` 或 `https://` 开头，末尾 `/` 自动去除 |
| `storager_ip` | 否 | 数据面地址（业务网段 IP，协议中立）。缺省时回退为 `base_url` 的主机名 |
| `role` | 否 | 角色：`disk`=可建系统盘（存储节点），`cd`=可挂载 ISO（光驱节点）；默认 `{disk: false, cd: false}` |
| `tags` | 否 | 自由标签数组（如 `storage` / `rack-a3`），**纯人工标注 / 展示用**；不参与 `/boot-vars` 连接符推导（后端类型由 `/boot-vars` 实时查 Agent `/capabilities` 判定，见 5.1） |
| `enabled` | 否 | 是否启用。`false` = **排空**：不再接任何新增盘（自动挑选 / 显式点名 / 直接对该节点建 LUN 一律拒绝），存量盘照常服务、已引导设备不受影响；默认 `true` |
| `remark` | 否 | 备注（人类识别用，展示性质）；默认空串。正常流程无需手工指定——由节点声明 `metadata.remark` 随 enroll 自动登记（见 6.5.1） |

### curl

```bash
curl -s -X POST "$BASE_URL/agents" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "storage-stgt-02",
    "base_url": "http://host.docker.internal:4840",
    "storager_ip": "192.168.1.6",
    "role": {"disk": true, "cd": false},
    "tags": ["storage"],
    "enabled": true,
    "remark": "存储节点 02"
  }'
```

### 成功返回（201）

```json
{
  "id": "storage-stgt-02",
  "base_url": "http://host.docker.internal:4840",
  "storager_ip": "192.168.1.6",
  "role": {"disk": true, "cd": false},
  "enabled": true,
  "tags": ["storage"],
  "remark": "存储节点 02"
}
```

### 错误返回

| 状态码 | 场景 |
|---|---|
| `400` | `id` 格式非法 / `base_url` 非 http(s) 开头 |
| `409` | Agent `id` 已存在 |

---

## 6.2 POST /agents/probe

### 说明

探测 Agent 并自动推导注册参数（**只读预览，不写任何文件**）：经 mTLS（控制面 CA 签发的客户端证书）调用 Agent `/healthz` + `/capabilities`，按以下规则推导：

| 参数 | 推导规则 |
|---|---|
| `role.disk` | 恒为 `true`（Agent 即存储节点） |
| `role.cd` | 取 `capabilities.cd` |
| `tags` | `["storage"]`（纯人工标签；后端类型**不写入 tags**，单独以 `backend` 字段返回，`/boot-vars` 实时查 `/capabilities` 判定） |
| `storager_ip` | 缺省回退 `base_url` 主机名 |

### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `base_url` | 是 | Agent 控制面 API 地址，须以 `http://` 或 `https://` 开头 |
| `agent_id` | 否 | 编辑场景的 Agent 标识（探测逻辑当前未使用，为前端预留） |

### curl

```bash
curl -s -X POST "$BASE_URL/agents/probe" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"base_url": "http://host.docker.internal:4840"}'
```

### 成功返回

```json
{
  "base_url": "http://host.docker.internal:4840",
  "role": {"disk": true, "cd": false},
  "tags": ["storage"],
  "storager_ip": "host.docker.internal",
  "enabled": true,
  "backend": "stgt",
  "fs_type": "btrfs",
  "base_iqn": "iqn.2026-07.com.controller",
  "clone": "reflink (FICLONE) -> shutil.copy fallback",
  "empty_disk": "truncate (sparse)",
  "persistent": "auto-scan on startup"
}
```

### 错误返回

| 状态码 | 场景 |
|---|---|
| `400` | `base_url` 非 http(s) 开头 |
| `502` | Agent 不可达（`/healthz` 失败）或 `/capabilities` 调用失败（如 mTLS 客户端证书非控制面 CA 签发） |

---

## 6.3 PUT /agents/{agent_id}

### 说明

更新已有 Agent：覆盖 `config/agents.yml` 中对应条目，保存后立即生效（建盘/挂载调度即用新配置）。`id` 不可改（走路径参数）。

适用场景：停用 / 启用节点（排空）、数据面地址迁移、备注与角色调整。**自动登记节点（tags 含 `auto`）的 `base_url` 由节点声明管理**（声明权威，见 6.5.1）：传现值可通过（WebUI 编辑保存的常规形态），传不同值返回 `409`——节点搬家请修改节点 `storager/kurrent.yaml` 的 `spec.agent.advertiseUrl` 后重新 `kurrent join`，renew 自动对齐；手工登记节点仍可在控制面改地址。

### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `base_url` | 是 | Agent 控制面 API 地址，须以 `http://` 或 `https://` 开头，末尾 `/` 自动去除；自动登记节点**不可改**（见说明） |
| `storager_ip` | 否 | 数据面地址。缺省时回退为 `base_url` 的主机名 |
| `role` | 否 | 角色：`disk`=可建系统盘，`cd`=可挂载 ISO；默认 `{disk: false, cd: false}` |
| `tags` | 否 | 自由标签数组（纯人工标注） |
| `enabled` | 否 | 是否启用。`false` = **排空**：不再接任何新增盘（一律 409），存量盘照常服务、已引导设备不受影响；默认 `true` |
| `remark` | 否 | 备注（人类识别用，展示性质）；全量覆盖语义，传空串即清除 |

### curl

```bash
curl -s -X PUT "$BASE_URL/agents/storage-stgt-02" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "base_url": "http://host.docker.internal:4840",
    "storager_ip": "192.168.1.8",
    "role": {"disk": true, "cd": false},
    "tags": ["storage"],
    "enabled": true,
    "remark": "备存储节点"
  }'
```

### 成功返回（200）

```json
{
  "id": "storage-stgt-02",
  "base_url": "http://host.docker.internal:4840",
  "storager_ip": "192.168.1.8",
  "role": {"disk": true, "cd": false},
  "enabled": true,
  "tags": ["storage"],
  "remark": "备存储节点"
}
```

### 错误返回

| 状态码 | 场景 |
|---|---|
| `400` | `base_url` 非 http(s) 开头 |
| `404` | Agent `id` 不存在 |
| `409` | 自动登记节点（tags 含 `auto`）修改 `base_url`（地址由节点声明管理，错误信息指明改法） |

> **编辑探测**：编辑场景建议先调 `POST /agents/probe`（6.2）验证地址可达再保存——探测经控制面 mTLS 客户端证书鉴权（身份绑定组件 CA），无需 token。自动登记节点的地址只读，探测按现值执行。

---

## 6.4 母盘标签（/agents/{agent_id}/masters/{master_name}/tag）

### 说明

母盘标签 = 控制面登记台账（`state/masters.yml`），os / os_version 为**备注性质**（便于人类理解，不做白名单校验）：

- 2026-08-30 起 OS 不再设合法集合（OS_ITEMS 退役）：建盘 `os` 为自由字符串，同系统多版本靠盘级 `os_tag`（12 位 hex 随机标识，数据面唯一键）区分，母盘标签只负责把母盘名 ↔ 系统/版本关联起来
- **登记不校验母盘存在性**（Agent 可离线，台账即权威）；`/masters` 聚合时对已登记条目附加 `os` / `os_version` 字段，未登记不附加
- 建盘选母盘时 WebUI 自动带出标签（备注可再改）；清除标签仅影响后续建盘

### PUT（登记 / 更新）

| 字段 | 必填 | 说明 |
|---|---:|---|
| `os` | 是 | 系统备注（自由字符串，小写归一；人类理解用，不是数据面标识） |
| `os_version` | 否 | 版本备注，默认 `''`（无版本） |

#### curl

```bash
curl -s -X PUT "$BASE_URL/agents/storage-lio-01/masters/_tpl_ubuntu_2204.img/tag" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "os": "ubuntu",
    "os_version": "22.04"
  }'
```

#### 成功返回

```json
{ "agent": "storage-lio-01", "name": "_tpl_ubuntu_2204.img", "os": "ubuntu", "os_version": "22.04" }
```

### DELETE（清除）

```bash
curl -s -X DELETE "$BASE_URL/agents/storage-lio-01/masters/_tpl_ubuntu_2204.img/tag" \
  -H "Authorization: Bearer $TOKEN"
```

返回 `{ "agent": ..., "name": ..., "removed": true|false }`（`removed=false` = 原本就没有标签）。

### 常见错误

| HTTP 状态码 | 常见原因 |
|---:|---|
| `400` | `os` 为空 / 非法字符；`os_version` 含非法字符 |
| `401` | 缺少 Token 或 Token 错误 |
| `404` | Agent 不存在 |

## 6.5 一键加入：bootstrap token 签发（集群级通用）+ enroll 自动登记

### 说明

控制面签发**集群级通用 bootstrap token**（`<6位>.<16位>`，仅存 sha256 摘要、TTL 默认 7 天）。**不绑节点**——节点身份由节点自决（join 自动生成随机短标识，人工输入归属声明 `metadata.remark` 备注）+ enroll 自动登记；TTL 内可被**多次 enroll 复用**（不消耗，不限制使用次数）。每次签发都是新 token（明文不可恢复、不可再查）；**支持吊销**（`DELETE /pki/tokens/{token_id}`，删除即失效），未吊销的 token 到期自然失效，无 409 幂等限制。

`nvmet-host` 组件凭据**不在此签发**：agent enroll 上报 `backend=nvmet` 时控制面派生（绑 agent_id）随 enroll/renew 响应下发，agent 落盘 `storager/bootstrap/nvmet-host.token` 供其引导（能力上报驱动，签发不预知后端）。

### POST /pki/tokens

请求体可选：`{"usage": "bootstrap"}`（默认，节点引导 token）或 `{"usage": "admin"}`
（管理 token，认证器链 Bearer 用；groups=admin）。

#### curl

```bash
# 节点引导 token（默认）
curl -s -X POST "$BASE_URL/pki/tokens" \
  -H "Authorization: Bearer $TOKEN"

# 管理 token（供脚本调用与 WebUI 登录；等同 admin 证书身份）
curl -s -X POST "$BASE_URL/pki/tokens" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"usage": "admin"}'
```

#### 成功返回（201）

```json
{
  "token": "a1b2c3.0123456789abcdef",
  "expires_at": "2026-09-22T12:00:00Z",
  "usage": ["enroll"],
  "groups": []
}
```

`token` 明文仅本次返回可见（存储只留 sha256 摘要）；`usage=["enroll"]` 为节点引导
token（enroll 校验用，不绑节点、TTL 内可复用）；`usage=["admin"]` + `groups=["admin"]`
为管理 token（认证器链 Bearer 用；`kurrent token create --usage admin` 同款）。

#### 常见错误

| HTTP 状态码 | 常见原因 |
|---:|---|
| `401` | 缺少 Token 或 Token 错误 |

### GET /pki/tokens

列出**未过期** token 台账条目（可视化管理）：bootstrap 引导 token、管理 token、nvmet-host 派生 token 同表。不返回明文（本就不可恢复）与 `token_secret_hash`；`created_at` 对旧条目为空串（该字段自签发时注入，历史条目没有）。读请求只需已认证（RBAC 最小版）。

```bash
curl -s "$BASE_URL/pki/tokens" -H "Authorization: Bearer $TOKEN"
```

```json
[
  {
    "token_id": "a1b2c3",
    "usage": ["admin"],
    "groups": ["admin"],
    "component": "",
    "agent_id": "",
    "name": "admin",
    "created_at": "2026-09-27T09:00:00+00:00",
    "expires_at": "2026-10-04T09:00:00+00:00"
  }
]
```

### DELETE /pki/tokens/{token_id}

吊销 token：删除台账条目，**删除即失效**——Bearer 校验每请求重读台账（无缓存），吊销后立即 401，且无法恢复。「谁在何时吊销了哪个 token」由操作审计（`pki.token.revoke`）留痕。写请求需 admin 组。

```bash
curl -s -X DELETE "$BASE_URL/pki/tokens/a1b2c3" \
  -H "Authorization: Bearer $TOKEN"
```

成功返回 `{"revoked": "a1b2c3"}`；token 不存在返回 `404`。WebUI「凭据」页与 `kurrent token list|revoke` 为同款调用。

### 6.5.1 enroll 自动登记（POST /enroll，nginx 入口 /api/cp/enroll）

首次引导自动登记：agent 组件引导（`POST /enroll`，Bearer = 集群级通用 bootstrap token）时若 `agents.yml` 无该 id，**自动创建条目**（role_disk=True、role_cd=False、enabled=True、tags=`("auto", "storage")`；旧 agent 不上报能力时仅 `("auto",)`——**后端类型不进 tags**，它由 `/boot-vars` 实时查 `/capabilities` 判定），并把请求体 `base_url` 字段（agent 侧 `spec.agent.advertiseUrl`，控制面可达地址）与 `remark` 字段（agent 侧 `metadata.remark`，备注/人类识别用）写入台账。规则：

- token 校验：格式 / 登记存在 / 组件匹配 / 未过期；通用 token 无节点绑定，CSR CN 自决身份（防冒名由 CN 校验承担）；派生 token（nvmet-host）须 agent_id 匹配（防串用）
- `nvmet-host` 组件**不自动登记**（与 agent 共享 agent_id，须 agent 组件先行在册；否则 400，容器重启重试；token 不消耗）
- `base_url` 非空时必须为 `http://` / `https://` 开头，否则 400（不自动登记）
- 已登记 agent 再次 enroll/renew 时**按节点声明刷新** `base_url` / `remark` / `role.cd`（声明权威：节点文件说了算，节点搬家/改备注后 renew 即对齐，无需重装）；控制面属性 `enabled` / `storager_ip` / `tags` **不碰**，节点未上报的字段保留现有值
- agent 上报 `backend=nvmet` → 响应附加 `nvmet_token`（派生 nvmet-host 凭据）；stgt/lio 或旧 agent 无此字段

### 6.5.2 一键加入命令（节点侧）

声明式工作流（配置即声明、CLI 即工具）：控制面签发 token 时输出**带控制面地址的 join 命令**，存储节点粘贴执行即获得地址——节点声明 `storager/kurrent.yaml` 缺失时由 join 按模板自动生成（`metadata.name` 自动生成随机标识、地址同步进 `spec.controlPlane.url`）、已存在则读入合并，随后收敛启动 agent（幂等可重跑；仓库根以外执行加 `--dir` 指定 storager 目录）：

```bash
# 控制面（签发集群级通用 bootstrap token；--cp-url = 存储节点可达的控制面 HTTPS 入口，缺省按 --server 推导）：
kurrent token create --cp-url https://<cp-host>

# 存储节点（命令已携带地址，粘贴执行；业务键可用 kurrent config print node-defaults 预置模板编辑）：
kurrent join https://<cp-host> --token a1b2c3.0123456789abcdef
```

## 6.6 DELETE /agents/{agent_id}

### 说明

删除 Agent 台账（`config/agents.yml` 条目）并清理该 agent 的母盘标签（`state/masters.yml` 中 masters 下对应条目）。**仅清控制面记录，不拦截、不触碰节点上的证书/盘文件**。

语义（声明权威的推论）：删除是**软删除**——节点仍带声明在跑，下次 renew 会按声明自动重新登记（重新加入无需重新签发 token）；真正下线一台节点须在**节点侧**操作（停容器 / 撤声明）。附带效应：登记删除后，仍挂在该节点上的盘在节点重新登记前无法投影引导地址（登记即认知），响应以 `disks_hosted` / `affected_workers` 告知（仅提示，不拦截）。

### curl

```bash
curl -s -X DELETE "$BASE_URL/agents/storage-lio-01" \
  -H "Authorization: Bearer $TOKEN"
```

### 成功返回

```json
{ "deleted": "storage-lio-01", "master_tags_removed": false, "disks_hosted": 3, "affected_workers": ["worker-01", "worker-02"] }
```

### 常见错误

| HTTP 状态码 | 常见原因 |
|---:|---|
| `401` | 缺少 Token 或 Token 错误 |
| `404` | Agent 不存在 |

---
---

## 7. POST /workers

### 说明

注册一台 Worker 的**身份**：hostname 绑定。**存储与身份分离**——本接口不创建任何系统盘，系统盘须另调 `POST /workers/{worker_id}/luns/disk`（见 7.1）。`mac` 现为**可选**：

- 不传 `mac` → 纯空转 Worker（仅 hostname 绑定，不授权任何设备），后续可用 `POST /devices/{mac}/bind`（16.7）绑定设备
- 传 `mac` → 设备须已在设备池中（`state=pooled`），校验后直接绑定（一对一授权）；设备池外或已绑定 → `409`——**先注册，后绑定**

Control Plane 会：

1. 校验 `worker_id`、`hostname`；传了 `mac` 时校验 `mac`
2. 写入 `state/workers.yml`（`disks` 为空数组，`state=registered`）
3. 传了 `mac` 时绑定设备（写 `state/devices.yml` 的 `bound_worker_id` + 写 `dnsmasq/dhcp-hosts.conf`）
4. 通过 Docker 向 `ipxe-dnsmasq` 容器发送 HUP：

```bash
docker exec ipxe-dnsmasq killall -HUP dnsmasq
```

5. 如指定 `windows_iso`，额外调用 Agent 创建 CD target（安装期光驱，与系统盘无关）

### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `worker_id` | 是 | Worker 编号。会自动转为小写。允许字母、数字、点、下划线、短横线 |
| `mac` | 否 | Worker 网卡 MAC 地址，格式如 `00:0c:29:b9:8b:2d`。传入时设备须已在设备池中（见 16 节），本调用同时完成绑定 |
| `hostname` | 否 | 主机名。不传时默认等于 `worker_id` |
| `arch` | 否 | 架构。不传时默认 `x86_64` |
| `windows_iso` | 否 | Windows 安装期 ISO 文件名。传入即在注册时额外创建安装光驱 target |
| `boot` | 否 | iPXE 菜单默认项与超时配置；不传则由 `/boot-vars` 按默认启动盘和全局默认值推导。与 7.3 `default-disk` 端点写的是同一组台账字段，后设覆盖先设 |

### `boot` 字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `menu_default` | 否 | iPXE 主菜单默认项（菜单超时后自动选中启动），合法值见 7.3 合法值表；不区分大小写，如 `menu-install`、`reboot`、`exit` |
| `menu_timeout` | 否 | iPXE 菜单超时，单位毫秒，如 `5000`；传 `0` 表示菜单无限等待、永不自动选择 |

不传 `boot` 时：

- `menu_default` 默认使用 `default_disk`（建盘后单独设置，见 7.3）；未设置时默认 `reboot`（循环重启等待配置，见 5 节）；
- `menu_timeout` 已配置默认启动时默认使用 `IPXE_CP_BOOT_MENU_TIMEOUT`（当前 `5000`）；处于 `reboot` 循环时固定用 `IPXE_CP_AUTO_BOOT_TIMEOUT`（当前 `1` 毫秒，见 5 节）。

因此大多数 Worker 不需要传 `boot`。例如：

```json
{
  "worker_id": "worker-01",
  "mac": "00:0c:29:b9:8b:2d"
}
```

注册后 Worker 还没有系统盘，`/boot-vars` 会返回 `menu-default reboot` + 1 毫秒超时——Worker 进入快速重启循环，等待管理员建盘/配置默认启动系统：

```ipxe
set menu-default reboot
set menu-timeout 1
```

创建系统盘后，调用 `PUT /workers/{worker_id}/default-disk`（见 7.3）设置默认启动盘（os_tag 引用具体盘），`menu-default` 随即切换为通用 OS 项 `boot-os`。

只有要覆盖菜单行为时才传 `boot`：

```json
{
  "worker_id": "worker-01",
  "mac": "00:0c:29:b9:8b:2d",
  "boot": {
    "menu_default": "exit",
    "menu_timeout": 0
  }
}
```

Windows 安装期如果希望默认进入安装菜单，可以这样传：

```json
{
  "worker_id": "worker-win-build",
  "mac": "00:0c:29:b9:8b:11",
  "windows_iso": "Win11_24H2.iso",
  "boot": {
    "menu_default": "menu-install",
    "menu_timeout": 3000
  }
}
```

### PUT /workers/{worker_id}/mac（换绑映射）

修改 Worker 的 MAC 绑定（hostname 不变），内部映射为**设备换绑**：新 MAC 须在设备池中（`state=pooled`），绑定到本 Worker；旧设备（若绑定本 Worker）解绑回池。审计同时记录 `device.bind`（新）+ `device.unbind`（旧）与兼容事件 `worker.mac.update`。

**请求体**：`{"mac": "00:0c:29:b9:8b:2d"}`

**409**：新 MAC 池外 / 已吊销 / 已绑定其他 Worker；旧设备绑定到意外 Worker。

**幂等**：重复设置同一 MAC 返回 `changed=false`。

## 7.1 POST /workers/{worker_id}/luns/disk

### 说明

给指定 Worker 创建系统盘 LUN。系统盘按 (os, os_version) 分类，一个 Worker 可挂多个系统的盘（同一系统同一版本至多一块，不同版本可并存——同系统多版本靠盘级 `os_tag` 随机标识区分）。Control Plane 会：

1. 校验 Worker 存在且尚未挂载该 (os, os_version) 的盘（已存在时返回 `409`）
2. 生成盘级随机标识 `os_tag`（12 位 hex，数据面唯一键）——决定盘 NQN 后缀与文件名
3. 选择存储 Agent（`disk_agent` 指定；不传则交给放置策略：候选须启用且数据面就绪，`type=master` 时还要求该节点本地存在这块母盘——无可用候选返回 `503`，`detail.excluded` 逐节点说明原因）
4. 拼接盘 NQN（`base-nqn:worker-id.os.<os_tag>`）与 backing filename（`worker-id.os.<os_tag>.img`）
5. 调用 Agent 创建磁盘 target（母盘克隆或空白盘）
6. 更新 `state/workers.yml` 中该 Worker 的 `disks` 台账（追加到数组），首次建盘时 `state` 由 `registered` 转为 `ready`
7. **建盘即签发**：盘落在 nvmet 后端节点时，若该 Worker 尚无认证密钥，控制面就地生成 DHHC-1 密钥并推送（审计 `credential.issue`，见 7.7）——已有密钥不轮换（按 Worker 跟盘，多盘共用一把）；非 nvmet 后端不签发（DHHC-1 是 NVMe-oF 专属，iSCSI 走 CHAP）

端点位于 `/luns/` 命名空间下，为将来数据盘（`/luns/data`）预留；多盘场景下，默认启动哪个盘由 `PUT /workers/{worker_id}/default-disk` 的 `disk`（os_tag）决定。

### Path 参数

| 参数 | 必填 | 说明 |
|---|---:|---|
| `worker_id` | 是 | Worker 编号 |

### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `type` | 是 | `master` 或 `empty` |
| `name` | 条件必填 | 当 `type=master` 时必填。表示母盘文件名 |
| `size` | 条件必填 | 当 `type=empty` 时必填。表示空白盘大小，如 `40G` |
| `os` | 是 | 系统备注（人类理解用，决定盘 NQN/文件名中的 os 段）。自由字符串，小写归一，**不做白名单校验**（2026-08-30 OS_ITEMS 退役） |
| `os_version` | 否 | 版本备注（`''` = 无版本，默认）；同一 (os, os_version) 至多一块，不同版本可并存 |
| `disk_agent` | 否 | 指定存储 Agent；不传时由控制面放置（候选含数据面就绪与母盘本地存在性校验） |

### 7.1.1 从母盘克隆

#### curl

```bash
curl -s -X POST "$BASE_URL/workers/worker-01/luns/disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "master",
    "os": "ubuntu",
    "os_version": "22.04",
    "name": "_tpl_ubuntu_2204.img"
  }'
```

### 7.1.2 创建空白盘

#### curl

```bash
curl -s -X POST "$BASE_URL/workers/worker-00/luns/disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "empty",
    "os": "ubuntu",
    "size": "40G"
  }'
```

### 成功返回示例（master 克隆）

```json
{
  "hostname": "worker-01",
  "arch": "x86_64",
  "state": "ready",
  "disks": [
    {
      "agent": "storage-lio-01",
      "nqn": "nqn.2026-07.com.controller:worker-01.ubuntu.0d26b6f33a89",  # NVMe-oF 数据面标识（权威，NQN 不用 IQN 定义；后缀带 os_tag）
      "iqn": "iqn.2026-07.com.controller:worker-01.ubuntu.0d26b6f33a89",  # iSCSI 数据面标识（由 NQN 派生）
      "filename": "worker-01.ubuntu.0d26b6f33a89.img",
      "backing": "/home/iscsi_img/worker-01.ubuntu.0d26b6f33a89.img",
      "os": "ubuntu",
      "os_version": "22.04",
      "os_tag": "0d26b6f33a89",
      "source": {
        "type": "master",
        "name": "_tpl_ubuntu_2204.img"
      }
    }
  ],
  "cd": null,
  "worker_id": "worker-01",
  "mac": "00:0c:29:b9:8b:2d"
}
```

### 成功返回示例（empty 空白盘）

```json
{
  "hostname": "worker-00",
  "arch": "x86_64",
  "state": "ready",
  "disks": [
    {
      "agent": "storage-lio-01",
      "iqn": "iqn.2026-07.com.controller:worker-00.ubuntu.5f1c2a3b4d5e",
      "filename": "worker-00.ubuntu.5f1c2a3b4d5e.img",
      "backing": "/home/iscsi_img/worker-00.ubuntu.5f1c2a3b4d5e.img",
      "os": "ubuntu",
      "os_version": "",
      "os_tag": "5f1c2a3b4d5e",
      "source": {
        "type": "empty",
        "size": "40G"
      }
    }
  ],
  "cd": null,
  "worker_id": "worker-00",
  "mac": "00:0c:29:b9:8b:00"
}
```

### 7.1.3 批量创建系统盘（POST /workers/luns/disk/batch）

批量部署场景：同一套盘参数应用到多个 Worker。**定位方式二选一**（同时给 → `400`）：

- `targets`：**逐项点名**存储节点（显式覆盖，原有调用方式）；
- `workers` + `placement`：只声明要给哪些 Worker 建盘，**存储节点由控制面放置**。

放置（`workers` 方式）的语义——放置是系统决策，调用方给意图与约束：

- **候选节点** = 磁盘角色 ∧ 启用（非排空）∧ 有 `base_url` ∧ **数据面就绪**（`healthz.backend_ready` 非 `false`）∧ 在 `placement.candidates` 内（留空 = 不限制）∧（`type=master` 时）**母盘在该节点本地存在**——克隆在节点本地完成，节点没有这块母盘就不可能建成；
- **分配** = 现有盘数最少优先（均摊），同负载按注册顺序；
- `placement.pins` 逐 Worker 钉死（显式覆盖）：被钉的节点**不要求在 `candidates` 内**，但须自身可用，否则 `503`；
- 无任何可用候选 → `503`，`detail.excluded` 逐节点给出被排除的原因（角色不符 / 排空 / 不可达 / 数据面未就绪 / 缺母盘）。

`dry_run=true` 只返回计划（`plan` / `warnings`）不建盘。**预览与执行共用同一份放置实现**，所以 `plan` 里的落点即随后执行的结果（逐行 `action` 为 `create` / `skip` / `error`）。注意 `targets` 方式的计划**不探测节点**（只按台账判断 create/skip/error），`workers` 方式的候选则在放置阶段逐个探过。

与单盘一致：`master` 走母盘克隆、`empty` 建空白盘；同一 (os, os_version) 至多一块，已存在则**自动跳过**（不算失败）。**创建成功的 Worker 自动将 `default_disk` 设为本次批量盘的 `os_tag`**——批量部署直接进入默认启动，无需再调 `PUT /workers/{worker_id}/default-disk`（单盘接口不自动设置）。落在 nvmet 后端节点的 Worker 同样**建盘即签发**认证密钥（各 Worker 密钥互不相同，一次到位，见 7.1 步骤 7 与 7.7）。逐项独立执行，单项失败不影响其余，返回 `succeeded` / `skipped` / `failed` 汇总；目标 Agent 不存在或已停用（排空）的项进入 `failed`（`agent not found` / `agent ... disabled`），其余项照常。

#### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `type` | 是 | `master` 或 `empty` |
| `os` | 是 | 系统备注（同一批次所有 Worker 相同，决定盘 NQN/文件名中的 os 段；自由字符串，不做白名单校验） |
| `os_version` | 否 | 版本备注（`''` = 无版本，默认） |
| `name` | 条件必填 | 当 `type=master` 时必填。表示母盘文件名 |
| `size` | 条件必填 | 当 `type=empty` 时必填。表示空白盘大小，如 `40G` |
| `targets` | 二选一 | 数组，每项 `{worker_id, agent}`：逐项指名存储节点 |
| `workers` | 二选一 | Worker 编号数组：存储节点交给放置策略 |
| `placement` | 否 | 仅对 `workers` 生效。`candidates`（数组，限定自动放置的候选节点，空 = 全部）；`pins`（对象 `{worker_id: agent}`，逐项钉死） |
| `dry_run` | 否 | `true` 只返回计划不建盘（默认 `false`） |

#### curl（控制面放置）

```bash
curl -s -X POST "$BASE_URL/workers/luns/disk/batch" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "master",
    "os": "ubuntu",
    "name": "_tpl_ubuntu_2204.img",
    "workers": ["worker-01", "worker-02", "worker-03"],
    "placement": { "candidates": ["storage-lio-01", "storage-lio-02"] },
    "dry_run": true
  }'
```

#### 计划返回（dry_run）

```json
{
  "dry_run": true,
  "plan": [
    { "worker_id": "worker-01", "agent": "storage-lio-01", "action": "create" },
    { "worker_id": "worker-02", "agent": "storage-lio-02", "action": "create" },
    { "worker_id": "worker-03", "agent": "storage-lio-01", "action": "skip",
      "reason": "already has a ubuntu system disk" }
  ],
  "warnings": [
    { "agent": "storage-stgt-01", "reason": "does not have master _tpl_ubuntu_2204.img" }
  ]
}
```

确认后把同一份请求去掉 `dry_run` 再发一次即可（落点与计划一致）。

#### curl（逐项点名，原有方式）

```bash
curl -s -X POST "$BASE_URL/workers/luns/disk/batch" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "master",
    "os": "ubuntu",
    "name": "_tpl_ubuntu_2204.img",
    "targets": [
      { "worker_id": "worker-01", "agent": "storage-lio-01" },
      { "worker_id": "worker-02", "agent": "storage-lio-01" },
      { "worker_id": "worker-03", "agent": "storage-stgt-01" }
    ]
  }'
```

#### 返回示例

```json
{
  "succeeded": [
    { "worker_id": "worker-01", "agent": "storage-lio-01", "iqn": "iqn.2026-07.com.controller:worker-01.ubuntu.0d26b6f33a89" },
    { "worker_id": "worker-03", "agent": "storage-stgt-01", "iqn": "iqn.2026-07.com.controller:worker-03.ubuntu.a1b2c3d4e5f6" }
  ],
  "skipped": [
    { "worker_id": "worker-02", "reason": "already has a ubuntu 22.04 system disk" }
  ],
  "failed": [
    { "worker_id": "worker-04", "agent": "storage-lio-01", "error": "worker not found: worker-04" }
  ]
}
```

---

## 7.2 Windows 安装期：身份注册 + ISO + 系统盘

Windows 安装流程分两步：先注册身份（可顺带指定安装介质 ISO），再创建系统盘。

### 7.2.1 身份注册（带 ISO）

```bash
curl -s -X POST "$BASE_URL/workers" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "worker_id": "worker-win-build",
    "mac": "00:0c:29:b9:8b:11",
    "windows_iso": "Win11_24H2.iso"
  }'
```

注册后返回 `state=installing`（存在 CD target），`disks` 为空数组。

### 7.2.2 创建系统盘

```bash
curl -s -X POST "$BASE_URL/workers/worker-win-build/luns/disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "empty",
    "os": "windows",
    "size": "80G"
  }'
```

创建后返回：

```json
{
  "hostname": "worker-win-build",
  "arch": "x86_64",
  "state": "installing",
  "disks": [
    {
      "agent": "storage-lio-01",
      "iqn": "iqn.2026-07.com.controller:worker-win-build.windows.9b8a7c6d5e4f",
      "filename": "worker-win-build.windows.9b8a7c6d5e4f.img",
      "backing": "/home/iscsi_img/worker-win-build.windows.9b8a7c6d5e4f.img",
      "os": "windows",
      "os_version": "",
      "os_tag": "9b8a7c6d5e4f",
      "source": {
        "type": "empty",
        "size": "80G"
      }
    }
  ],
  "cd": {
    "agent": "controller-stgt",
    "iqn": "iqn.2026-07.com.controller:worker-win-build.windows.iso",
    "iso": "Win11_24H2.iso",
    "backing": "/home/iscsi_img/Win11_24H2.iso"
  },
  "worker_id": "worker-win-build",
  "mac": "00:0c:29:b9:8b:11"
}
```

安装期结束后，CD target 随 Worker 删除流程清理。

### 常见错误

| HTTP 状态码 | 常见原因 |
|---:|---|
| `400` | 参数格式错误；`os` 为空或含非法字符；`type=master` 却没传 `name`；`type=empty` 却没传 `size` |
| `401` | 缺少 Token 或 Token 错误 |
| `404` | 创建系统盘时 Worker 不存在；`disk_agent` 指定的 Agent 不存在 |
| `409` | `worker_id` 已存在；`hostname` 已存在；MAC 已绑定；Worker 已有该 (os, os_version) 系统盘（同版本重复创建）；Agent 上 IQN 已存在；backing 文件已存在；目标 Agent 已停用（排空：`agent ... disabled: not accepting new disks`） |
| `500` | dnsmasq reload 失败；写文件失败；其他未预期错误 |
| `503` | Agent 不可达；docker.sock 不可用 |

---

## 7.3 PUT /workers/{worker_id}/default-disk

### 说明

**「默认启动盘」是干什么的**：一台 Worker 可以挂多块系统盘（同一系统同一版本至多一块，如 `ubuntu 22.04` + `windows`，同系统不同版本可并存）。每次开机，iPXE 菜单在超时后会自动选中某一项启动——本端点配置的默认启动盘决定自动选中哪一项，同时决定 `/boot-vars` 投影哪块盘的连接信息（`base_nqn` / `storager_ip` 取默认启动盘，见 5 节）。不设置时菜单自动选 `reboot`，配合 1 毫秒超时循环重启，等待管理员完成配置，避免静默进错系统。

**注意**：`disk` 不是系统名，而是盘级随机标识 `os_tag`（建盘 7.1 时生成，12 位 hex，数据面唯一键）——同系统多版本共存时，只有 os_tag 能精确定位要默认启动的**具体盘**。

`/boot-vars` 的 `menu_default` 推导链：

```text
default_disk（本端点 disk 字段，优先）-> boot.menu_default（本端点 menu_default 字段）-> reboot（未配置，循环重启等待）
```

请求体三个字段可单独或组合传，至少传一个；传 `null`（或空字符串）表示清除对应项。可重复调用，后设覆盖先设——与注册时传入的 `boot`（见 7.0）写的是同一组台账字段。

要求：

- 设置 `disk`：Worker 必须已有该 `os_tag` 对应的系统盘（`POST /workers/{worker_id}/luns/disk` 创建的某块盘），否则返回 `400` 并列出当前系统盘；多盘模型下用 `os_tag` 精确匹配要默认启动的盘
- 设置 `menu_default`：值必须为 `menu.ipxe` 主菜单的 item ID（严格校验，防止 iPXE `choose --default` 落空）；OS 语义值（旧系统名）统一归一到通用 OS 项 `boot-os`
- 设置 `menu_timeout`：非负整数；清除后恢复默认 `IPXE_CP_BOOT_MENU_TIMEOUT`

### Path 参数

| 参数 | 必填 | 说明 |
|---|---:|---|
| `worker_id` | 是 | Worker 编号 |

### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `disk` | 否 | 默认启动盘的 `os_tag`（12 位 hex，精确引用具体盘），须与该 Worker 已挂盘一致；传 `null` 清除 |
| `menu_default` | 否 | iPXE 主菜单默认项（菜单超时后自动选中），见下方合法值表；不区分大小写（自动转小写）；传 `null` 清除 |
| `menu_timeout` | 否 | 菜单超时毫秒数，非负整数；传 `0` 表示菜单无限等待、永不自动选择（等人工按键）；传 `null` 清除，恢复默认 `IPXE_CP_BOOT_MENU_TIMEOUT`（当前 `5000`） |

### `menu_default` 合法值（menu.ipxe 主菜单 item ID）

| 类别 | 合法值 |
|---|---|
| 通用 OS 项 | `boot-os`（由默认盘配置推导，不在此手工设置；OS 语义旧值自动归一到此项） |
| 工具 / 安装 | `menu-diag` `menu-install` |
| 高级 | `config` `shell` `reboot` `exit` |

### 示例：设置默认启动盘

```bash
curl -s -X PUT "$BASE_URL/workers/worker-01/default-disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "disk": "0d26b6f33a89"
  }'
```

### 示例：设置菜单默认项与超时

```bash
curl -s -X PUT "$BASE_URL/workers/worker-win-build/default-disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "menu_default": "menu-install",
    "menu_timeout": 3000
  }'
```

### 示例：清除默认启动盘

```bash
curl -s -X PUT "$BASE_URL/workers/worker-01/default-disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "disk": null
  }'
```

### 成功返回

返回该 Worker 的完整台账（含 `default_disk`、`boot.menu_default`、`boot.menu_timeout` 等已设置字段）。

### 常见错误

| HTTP 状态码 | 常见原因 |
|---:|---|
| `400` | 三个字段都没传；`disk` 与该 Worker 已挂盘不一致；`menu_default` 不在合法值表；`menu_timeout` 为负数 |
| `401` | 缺少 Token 或 Token 错误 |
| `404` | Worker 不存在 |
| `409` | 设置 `disk` 时 Worker 还没有系统盘 |

---

## 7.4 DELETE /workers/{worker_id}/luns/disk/{os_tag}

### 说明

删除指定 Worker 的单个系统盘（按盘级随机标识 `os_tag`，12 位 hex）。Control Plane 会：

1. 校验 Worker 存在且已挂载该 `os_tag` 对应的盘（不存在时返回 `404`）
2. 调用该盘所在 Agent 删除 iSCSI target
3. 从 `state/workers.yml` 的 `disks` 数组中移除该盘记录
4. 联动清理：被删盘若为默认启动盘（`default_disk` 等于该盘 `os_tag`），一并清除 `default_disk` 与同名（同 os）的 `boot.menu_default`（防止 iPXE 启动到已删除的系统盘）
5. 删完最后一块盘时 `state` 由 `ready` 回退 `registered`（等待重新建盘）

### Query 参数

| 参数 | 默认 | 说明 |
|---|---:|---|
| `delete_file` | `false` | 是否同时删除 backing `.img` 文件。`false` 仅删除 target（.img 保留，可重新挂载） |
| `ignore_missing_target` | `false` | 目标在 Agent 上已不存在时是否忽略 404，继续完成台账删除；盘所在 Agent 的登记已删除时同义（ignore=false 报 404 `agent not registered`，ignore=true 视为已缺失） |

### 示例：删除系统盘但保留 .img

```bash
curl -s -X DELETE "$BASE_URL/workers/worker-01/luns/disk/0d26b6f33a89" \
  -H "Authorization: Bearer $TOKEN"
```

### 示例：删除系统盘并同时删除 .img 文件

```bash
curl -s -X DELETE "$BASE_URL/workers/worker-01/luns/disk/0d26b6f33a89?delete_file=true" \
  -H "Authorization: Bearer $TOKEN"
```

### 成功返回

返回该 Worker 的完整台账（`disks` 已不含被删系统盘；若为默认盘，`default_disk`/`boot.menu_default` 已被清除；无盘时 `state=registered`）。

### 常见错误

| HTTP 状态码 | 常见原因 |
|---:|---|
| `400` | `os_tag` 非法（非 12 位 hex） |
| `401` | 缺少 Token 或 Token 错误 |
| `404` | Worker 不存在，或该 Worker 没有此 `os_tag` 对应的盘 |

---

## 7.5 PUT /workers/{worker_id}/mac

### 说明

修改指定 Worker 的 **MAC 地址绑定**（hostname 不变）：Control Plane 会：

1. 校验 Worker 存在（不存在时返回 `404`）
2. 校验新 MAC 格式（非法返回 `400`）；**新 MAC 已被其他 hostname 占用时返回 `409`**（不写入，防止一 MAC 多 Worker）
3. 更新 `dnsmasq/dhcp-hosts.conf` 中该 hostname 的绑定并 HUP 重载 dnsmasq（保持文件 inode 不变，文件级 bind mount 下重载立即可见）
4. **审计记录 `worker.mac.update`（含 `old_mac` / `new_mac` / `changed` / `client`）**——即修改历史，可通过 `GET /operations` 查询；新 MAC 与旧 MAC 相同时 `changed=false`，不触发重载

> **注意**：`workers.yml` 台账不存 MAC，MAC 唯一权威在 `dnsmasq/dhcp-hosts.conf`，本端点直接改绑定文件；改完后 Worker 需重新获取 DHCP 租约（重启网卡 / 重新 PXE 启动）才会使用新 MAC 对应身份。

### Body 参数

| 参数 | 必填 | 说明 |
|---|---:|---|
| `mac` | 是 | 新 MAC 地址，格式 `XX:XX:XX:XX:XX:XX`（大小写均可，统一规范化） |

### 示例

```bash
curl -s -X PUT "$BASE_URL/workers/worker-01/mac" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mac": "00:0c:29:b9:8b:01"}'
```

### 成功返回

返回该 Worker 的完整台账（`mac` 字段为更新后的实时反查结果）。

### 常见错误

| HTTP 状态码 | 常见原因 |
|---:|---|
| `400` | `mac` 格式非法 |
| `401` | 缺少 Token 或 Token 错误 |
| `404` | Worker 不存在，或该 hostname 在 dnsmasq 中无绑定 |
| `409` | 新 MAC 已被其他 hostname 占用 |
| `500` | dnsmasq 重载失败（绑定文件已更新） |

---

## 7.6 POST /workers/batch

### 说明

批量创建 Worker（**逐项独立**，单项失败不影响其余；**幂等**，重复执行不产生重复 Worker）。按 `name_prefix` + 序号生成 `worker_id`（`worker-01`、`worker-02` …，序号从 `01` 起，位宽随 `count` 自适应——`count=100` 时生成 `worker-001` … `worker-100`）。

- 不传 `macs` → 全部为**纯空转** Worker（仅 hostname 绑定，不授权任何设备），后续可用 `POST /devices/{mac}/bind`（16.7）绑定
- 传 `macs`（须与 `count` 等长）→ 逐项校验设备池并直接绑定（语义同 7 节传 `mac`：设备须 `state=pooled`，池外 / 已绑定 → 该项 `failed` 且**该项不创建**，可修正后重试）

不支持 `windows_iso`（安装期 ISO 请逐个走 7 节）。

### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `count` | 是 | 创建数量，1–100 |
| `name_prefix` | 否 | Worker 编号前缀，默认 `worker-`。生成的 `worker_id` 须合法（允许字母、数字、点、下划线、短横线），非法前缀整批返回 `400` |
| `macs` | 否 | MAC 地址数组（格式 `00:0c:29:b9:8b:2d`），提供时长度必须等于 `count`，逐项校验并直接绑定 |
| `arch` | 否 | 架构。不传时默认 `x86_64` |
| `boot` | 否 | iPXE 菜单默认项与超时配置，字段同 7 节 |

### 幂等与失败分类

- `succeeded`：本次创建成功（传了 `macs` 时含绑定——绑定成功才创建该项）
- `skipped`：`worker_id` 已存在（重复执行同一请求的结果）
- `failed`：设备池外 / 已吊销 / 已绑定 / MAC 非法 / hostname 冲突等——该项不创建，其余项不受影响；修正后可重试

### 示例

```bash
curl -s -X POST "$BASE_URL/workers/batch" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "count": 3,
    "name_prefix": "worker-",
    "macs": ["00:0c:29:b9:8b:01", "00:0c:29:b9:8b:02", "00:0c:29:b9:8b:03"]
  }'
```

### 成功返回（200）

```json
{
  "succeeded": [
    {"worker_id": "worker-01", "hostname": "worker-01", "mac": "00:0c:29:b9:8b:01"}
  ],
  "skipped": [],
  "failed": [
    {
      "worker_id": "worker-02",
      "hostname": "worker-02",
      "mac": "00:0c:29:b9:8b:02",
      "error": "device already bound to worker-01: 00:0c:29:b9:8b:02"
    }
  ]
}
```

### 常见错误

| HTTP 状态码 | 常见原因 |
|---:|---|
| `400` | `name_prefix` 为空 / 生成的 `worker_id` 非法 / `macs` 长度不等于 `count` |
| `401` | 缺少 Token 或 Token 错误 |
| `422` | `count` 缺失 / 不在 1–100 范围 |

---

## 7.7 NVMe-oF 认证凭据（/workers/{worker_id}/credential）

### 说明

NVMe-oF 连接认证密钥库（按 Worker 跟盘裁定 2026-08-22）：密钥按 `worker_id` 索引，绑定到该
Worker 的设备共享同一密钥（换绑即换钥）。密钥为 **DHHC-1** 格式（蓝图 2.1 契约）：
`DHHC-1:<两位数字类型>:<base64(密钥 + CRC32 小端 4 字节)>`，密钥体 32 字节（SHA-256）或 64 字节（SHA-512）。

**签发时机：建盘即签发（2026-10-05）**——在 nvmet 后端节点上创建系统盘时（7.1 步骤 7，单盘与批量同），
控制面为尚无密钥的 Worker 就地生成 DHHC-1 密钥并推送（审计 `credential.issue`）：子系统与它配套的
认证密钥同时就位，批量建盘 N 台即 N 份密钥一次到位，无需逐台手工设置。已有密钥的 Worker 建盘**不轮换**
（按 Worker 跟盘，多盘共用一把）。非 nvmet 后端（stgt / lio）不签发——DHHC-1 是 NVMe-oF 专属。
因此本节的手工端点用途收窄为**显式轮换 / 吊销**（吊销即明确「不用认证」，其条目被删，不会被后续建盘复活）。

凭据写入后控制面会**推送期望状态给持有该 Worker 系统盘的 Agent**（审计 `credential.push`，不记密钥本体）：
推送体含明文密钥。
Agent 转调存储节点 nvmet 宿主服务登记 hosts（子系统 NQN = 盘 NQN——盘标识权威 = NQN，IQN 由 NQN 派生
（`iqn.` + nqn[4:]），不作子系统 NQN 使用；NVMe Base Spec §7.9 要求 `nqn.` 前缀。**Host NQN =
worker 派生** `nqn.<…>:host.<worker_id>`，base 取盘 NQN 前缀 = 节点侧 `spec.agent.nqnBase`），
由 `/boot-vars` 注入固件覆盖其内置默认值（固件默认按 SMBIOS UUID 派生，不覆盖必认证失败）。

**推送对账（2026-10-05）**：每次推送的逐 Agent 结果记录在 `state/credential-push.yml`
（键 = `worker_id/agent_id`；含所推密钥的审计哈希，不含明文）。控制面对账线程周期（默认 60s，
`KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL` 秒，0 = 禁用）比对「期望 × 记录」：密钥哈希一致且
已送达（ok / n-a）跳过，否则幂等重放——覆盖推送失败未重试、进程崩溃缝隙（台账已改未推送）与
吊销未送达三类静默缺口。无密钥且无推送历史的对**不推送**、不记台账、不记审计（避免 Agent 离线时
出现「同步失败」的虚假故障）——nvmet 建盘已自动签发，实际只剩非 nvmet 后端的盘（DHHC-1 不适用，
推送只会得 `n/a`）；一旦有过推送记录，之后的吊销即使未送达也持续重放。Agent 登记被删（登记删除语义）→ 记录剪除，重新登记后自动重推。
对账重放仅在状态或密钥变化时记审计（去噪）；用户动作触发的推送照常每次记录。
推送结果：`ok` 已送达（Agent 缓存与宿主服务均已同步）；`failed` 未送达（Agent 不可达 / 宿主
同步失败，对账持续重试）；`n/a` 目标 Agent 非 nvmet 后端（凭据不适用，非故障）；`pending` 尚无
记录或记录为旧值（待对账重放）；`none` 无同步目标（无 NVMe-oF 盘、凭据从未设置无推送历史，或盘所在 Agent 未登记）。

触发推送的操作：**建盘即签发**（nvmet 后端，见上）、设置/删除凭据、设备绑定/解绑/换绑（`PUT /workers/{worker_id}/mac`）、删盘（含批量）、删除 Worker。

静态存储边界（2026-10-05 裁定）：密钥明文按链路必要范围落盘三处——控制面 `state/credentials.yml`、
Agent 容器凭据缓存（0600）、存储节点 configfs（内核 hosts 矩阵 `dhchap_key`）——**不引入应用层加密**，
边界责任归部署方（磁盘/宿主持久化加密、root 访问边界）；密钥经 mTLS 组件通道传输，不进入浏览器
存储与服务端日志。

### PUT /workers/{worker_id}/credential

设置或更新密钥（幂等：重复设置同值 `updated_at` 不变，但仍触发一次推送——补推通道）。

```bash
curl -X PUT "$BASE_URL/workers/worker-01/credential" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"secret": "DHHC-1:01:<base64>"}'
```

成功返回（`push` = 逐 Agent 送达状态投影，见上；未送达由对账自动重试）：

```json
{
  "worker_id": "worker-01",
  "exists": true,
  "secret_hash": "sha256:1a2b3c4d5e6f",
  "created_at": "2026-08-22T08:00:00+00:00",
  "updated_at": "2026-08-22T08:00:00+00:00",
  "push": {
    "result": "ok",
    "updated_at": "2026-10-05T15:30:00+08:00",
    "agents": [
      {"agent_id": "storage-nvmet-01", "result": "ok", "updated_at": "2026-10-05T15:30:00+08:00"}
    ]
  }
}
```

| HTTP 状态码 | 常见原因 |
|---:|---|
| `422` | DHHC-1 自检失败（前缀/类型位数/base64/长度 36/68/CRC 终值任一不符） |
| `404` | Worker 不存在 |

### DELETE /workers/{worker_id}/credential

删除密钥（吊销该 Worker 名下全部绑定设备的认证资格，下次启动回落明文连接）：

```bash
curl -X DELETE "$BASE_URL/workers/worker-01/credential" -H "Authorization: Bearer $TOKEN"
```

成功返回 `{"worker_id": "worker-01", "deleted": true, "push": {...}}`（`push.result=ok` 表示
吊销已送达；未送达会被对账重放，不会静默滞留）；Worker 或条目不存在 → `404`。

### GET /workers/{worker_id}/credential

查询凭据元数据与推送状态（**不返回明文**，`secret_hash` 只给前缀便于轮换比对）：

```bash
curl -s "$BASE_URL/workers/worker-01/credential" -H "Authorization: Bearer $TOKEN"
```

```json
{
  "worker_id": "worker-01",
  "exists": true,
  "secret_hash": "sha256:1a2b3c4d5e6f",
  "created_at": "2026-08-22T08:00:00+00:00",
  "updated_at": "2026-08-22T08:00:00+00:00",
  "push": {
    "result": "ok",
    "updated_at": "2026-10-05T15:30:00+08:00",
    "agents": [
      {"agent_id": "storage-nvmet-01", "result": "ok", "updated_at": "2026-10-05T15:30:00+08:00"}
    ]
  }
}
```

无条目时 `exists=false` 且无 `secret_hash`，`push` 投影仍返回（吊销送达状态可见）。
CLI：`kurrent workers credential show|set|revoke <worker_id>`；WebUI：Worker 详情独立「NVMe-oF 凭据」页（该 Worker 有 NVMe-oF 盘时才出现）。

---

## 8. GET /workers

### 说明

列出当前所有 Worker 台账。返回结果中的 `mac` 字段来自 `dnsmasq/dhcp-hosts.conf` 的实时反查。

### curl

```bash
curl -s "$BASE_URL/workers" \
  -H "Authorization: Bearer $TOKEN"
```

### 成功返回示例

```json
[
  {
    "hostname": "worker-00",
    "arch": "x86_64",
    "state": "ready",
    "disks": [
      {
        "agent": "storage-lio-01",
        "iqn": "iqn.2026-07.com.controller:worker-00.ubuntu",
        "filename": "worker-00.ubuntu.img",
        "backing": "/home/iscsi_img/worker-00.ubuntu.img",
        "os": "ubuntu",
        "source": {
          "type": "empty",
          "size": "40G"
        }
      }
    ],
    "cd": null,
    "worker_id": "worker-00",
    "mac": "00:0c:29:b9:8b:00"
  }
]
```

---

## 9. GET /workers/{worker_id}

### 说明

查询单个 Worker 的台账记录。

### Path 参数

| 参数 | 必填 | 说明 |
|---|---:|---|
| `worker_id` | 是 | Worker 编号 |

### curl

```bash
curl -s "$BASE_URL/workers/worker-01" \
  -H "Authorization: Bearer $TOKEN"
```

### 成功返回

返回结构与 `POST /workers` 成功结果一致。

---

## 10. GET /workers/{worker_id}/status

### 说明

查询 Worker 的台账信息，并实时检查：

- `dnsmasq/dhcp-hosts.conf` 中是否存在 hostname 对应的 MAC
- Agent 上对应的 disk target 是否存在
- Agent 上对应的 cd target 是否存在

### Path 参数

| 参数 | 必填 | 说明 |
|---|---:|---|
| `worker_id` | 是 | Worker 编号 |

### curl

```bash
curl -s "$BASE_URL/workers/worker-01/status" \
  -H "Authorization: Bearer $TOKEN"
```

### 成功返回示例

```json
{
  "worker": {
    "hostname": "worker-01",
    "arch": "x86_64",
    "state": "ready",
    "disks": [
      {
        "agent": "storage-lio-01",
        "iqn": "iqn.2026-07.com.controller:worker-01.ubuntu",
        "filename": "worker-01.ubuntu.img",
        "backing": "/home/iscsi_img/worker-01.ubuntu.img",
        "os": "ubuntu",
        "source": {
          "type": "master",
          "name": "_tpl_ubuntu_2204.img"
        }
      }
    ],
    "cd": null,
    "worker_id": "worker-01",
    "mac": "00:0c:29:b9:8b:2d"
  },
  "actual": {
    "dnsmasq": {
      "hostname": "worker-01",
      "mac": "00:0c:29:b9:8b:2d"
    },
    "disks": [
      {
        "os": "ubuntu",
        "exists": true,
        "target": {
          "iqn": "iqn.2026-07.com.controller:worker-01.ubuntu",
          "luns": [
            {
              "backing": "/home/iscsi_img/worker-01.ubuntu.img"
            }
          ]
        }
      }
    ],
    "cd": null
  }
}
```

---

## 11. DELETE /workers/{worker_id}

### 说明

删除 Worker。Control Plane 会：

1. 从 `workers.yml` 找到该 Worker 的 disk/cd 台账
2. **吊销凭据推送**：向持有其盘的已登记 Agent 推送吊销（Agent 摘除 hosts 登记并清缓存）——删除前唯一一次凭据推送
3. **联动解绑设备**：所有 `bound_worker_id` 为该 Worker 的设备回池（`state=pooled`、`bound_worker_id=null`，设备**不吊销**）——先解绑落盘，解绑失败则中止删除
4. 如果存在 cd target，先删 cd
5. 再删 disk target
6. 从 `workers.yml` 删除该 Worker
7. **清空该 Worker 的凭据痕迹**：删掉 `state/credentials.yml` 的密钥条目与该 Worker 的推送台账（审计 `credential.forget`）——Worker 已不存在，若留下孤儿条目它既不可见（`GET /credential` 对不存在的 Worker 返回 404）也不可删，且同 id 的 Worker 重建时会**静默继承旧密钥**并把旧钥推给新机器的盘
8. 从 `dnsmasq/dhcp-hosts.conf` 删除 `mac,hostname` 这一行
9. HUP `ipxe-dnsmasq`

`POST /workers/delete/batch`（11.1）同样联动解绑并清理凭据痕迹。

### Path 参数

| 参数 | 必填 | 说明 |
|---|---:|---|
| `worker_id` | 是 | 要删除的 Worker 编号 |

### Query 参数

| 参数 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `delete_disk` | 否 | `false` | 是否连 backing `.img` 文件一起删除 |
| `ignore_missing_target` | 否 | `false` | 删除时若 Agent 返回 `404 iqn not found`，是否忽略继续执行；盘所在 Agent 的登记已删除时同义（ignore=false 报 404 `agent not registered`，ignore=true 视为已缺失，凭据推送跳过并记审计） |

### curl

只删 target，保留 `.img`：

```bash
curl -s -X DELETE "$BASE_URL/workers/worker-01?delete_disk=false" \
  -H "Authorization: Bearer $TOKEN"
```

连 `.img` 一起删：

```bash
curl -s -X DELETE "$BASE_URL/workers/worker-01?delete_disk=true" \
  -H "Authorization: Bearer $TOKEN"
```

忽略 Agent 上 target 已不存在的情况：

```bash
curl -s -X DELETE "$BASE_URL/workers/worker-01?delete_disk=true&ignore_missing_target=true" \
  -H "Authorization: Bearer $TOKEN"
```

### 成功返回示例

```json
{
  "deleted": "worker-01",
  "delete_disk": false,
  "dnsmasq_removed": true
}
```

---

## 11.1 POST /workers/delete/batch

### 说明

批量删除 Worker。每项独立执行，**单项失败不影响其余**，返回 `succeeded` / `failed` 汇总；每个 Worker 的处理与 11 节单删一致（删 CD/系统盘 target → 移台账 → 移除 dnsmasq 绑定），全部成功项统一保存台账并**只 reload 一次** dnsmasq。不存在的 Worker 计入 `failed`（`worker not found`）。

### 请求体字段

| 字段 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `worker_ids` | 是 | — | 要删除的 Worker 编号数组 |
| `delete_disk` | 否 | `false` | 是否连 backing `.img` 文件一起删除 |
| `ignore_missing_target` | 否 | `false` | 删除时若 Agent 返回 `404 iqn not found`，是否忽略继续执行；盘所在 Agent 的登记已删除时同义（ignore=false 报 404 `agent not registered`，ignore=true 视为已缺失，凭据推送跳过并记审计） |

### curl

```bash
curl -s -X POST "$BASE_URL/workers/delete/batch" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "worker_ids": ["worker-01", "worker-02"],
    "delete_disk": false,
    "ignore_missing_target": true
  }'
```

### 成功返回示例

```json
{
  "succeeded": [
    {"worker_id": "worker-01", "hostname": "worker-01"}
  ],
  "failed": [
    {"worker_id": "worker-03", "error": "worker not found: worker-03"}
  ]
}
```

---

## 12. GET /operations

### 说明

读取 Control Plane 的操作流水。这个文件是 `state/operations.jsonl` 的增量查询接口。

### Query 参数

| 参数 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `since` | 否 | `0` | 只返回 `id > since` 的记录 |
| `limit` | 否 | `1000` | 最多返回多少条 |
| `mac` | 否 | — | 仅返回该设备（MAC）的操作（规范化后按 `mac` 字段过滤），用于设备绑定记录查看 |

### curl

从头读取：

```bash
curl -s "$BASE_URL/operations" \
  -H "Authorization: Bearer $TOKEN"
```

增量读取：

```bash
curl -s "$BASE_URL/operations?since=10&limit=100" \
  -H "Authorization: Bearer $TOKEN"
```

### 成功返回示例

```json
{
  "next_cursor": 5,
  "entries": [
    {
      "id": 1,
      "ts": "2026-07-27T14:20:00+00:00",
      "op": "create_worker",
      "status": "started",
      "worker_id": "worker-01",
      "client": "172.18.0.1"
    },
    {
      "id": 2,
      "ts": "2026-07-27T14:20:01+00:00",
      "op": "agent.create_disk",
      "status": "ok",
      "worker_id": "worker-01",
      "agent": "storage-lio-01",
      "iqn": "iqn.2026-07.com.controller:worker-01.ubuntu"
    }
  ]
}
```

---

## 13. 典型测试顺序

建议这样验一遍：

### 13.1 查服务存活

```bash
curl -s "$BASE_URL/healthz"
```

### 13.2 查启动变量投影

```bash
curl -s "$BASE_URL/boot-vars?mac=000c29b98b2d&hostname=worker-01"
curl -s "$BASE_URL/boot-vars?mac=000c29b98b2d&hostname=worker-01&format=json"
```

### 13.3 查 Agent 配置和能力

注意：`config/agents.yml` 是由 Control Plane 容器内部读取的。Agent 如果和 Control Plane 在同一台宿主机上，不能写 `http://localhost:4840`，因为容器里的 `localhost` 指向 Control Plane 容器自己。

默认 compose 已配置：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

因此同宿主机上的 Agent 建议写：

```yaml
base_url: http://host.docker.internal:4840
```

```bash
curl -s "$BASE_URL/agents?live=true" \
  -H "Authorization: Bearer $TOKEN"
```

### 13.4 注册 Worker 身份（hostname + MAC 绑定）

```bash
curl -s -X POST "$BASE_URL/workers" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "worker_id": "worker-00",
    "mac": "00:0c:29:b9:8b:00"
  }'
```

此时 Worker 已绑定 MAC，但还没有系统盘（`state=registered`，`disks` 为空数组）。

### 13.5 给 Worker-00 创建系统盘（空白盘）

```bash
curl -s -X POST "$BASE_URL/workers/worker-00/luns/disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "empty",
    "os": "ubuntu",
    "size": "40G"
  }'
```

系统盘创建完成后 `state` 由 `registered` 转为 `ready`。

### 13.6 设置默认启动配置

```bash
# disk = 13.5 建盘返回的 os_tag（12 位 hex 随机标识，精确引用具体盘）
curl -s -X PUT "$BASE_URL/workers/worker-00/default-disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "disk": "0d26b6f33a89"
  }'
```

此时 `/boot-vars` 的 `menu-default` 返回 `boot-os`（2026-08-30 MAIN MENU 动态化：OS 项已收敛为唯一通用项）。菜单项与超时的设置示例见 7.3。

### 13.7 查询 Worker 台账

```bash
curl -s "$BASE_URL/workers/worker-00" \
  -H "Authorization: Bearer $TOKEN"
```

### 13.8 查询实时状态

```bash
curl -s "$BASE_URL/workers/worker-00/status" \
  -H "Authorization: Bearer $TOKEN"
```

### 13.9 删除 Worker，但保留空白盘文件

```bash
curl -s -X DELETE "$BASE_URL/workers/worker-00?delete_disk=false" \
  -H "Authorization: Bearer $TOKEN"
```

这一步正好适合“空白盘制作完成后，人工改名为母盘”的工作流。

---

## 14. Agent iSCSI LUN/target 管理

### 说明

Control Plane 可以直接管理任意 Agent 上的 iSCSI target/LUN。请求经 Control Plane 转发到 Agent（mTLS，控制面组件客户端证书绑定内部 CA），因此调用方只需持有 Control Plane Token，无需直接接触 Agent。

与 Worker 生命周期接口（`POST /workers`、`DELETE /workers/{worker_id}`）的区别：

- Worker 接口面向**台账**：自动拼接 IQN、写 `state/workers.yml`、写 dnsmasq 绑定；
- LUN 管理接口面向**数据面直管**：不写任何台账，直接操作 Agent 上的 target，适合母盘管理、手工排障、ISO 临时挂载等场景。

所有接口都需要鉴权（见 1.3 认证器链）。Agent 不存在时返回 `404 agent not found`；Agent 不可达时返回 `503`；Agent 侧的业务校验错误（如 IQN 前缀不匹配、文件已存在、IQN 已存在）会透传其状态码与 `detail`：

```json
{"agent": "storage-lio-01", "error": "iqn base mismatch: ..."}
```

### 14.1 GET /agents/{agent_id}/luns

列出指定 Agent 上的全部 iSCSI target/LUN。返回结构由 Agent 后端决定（stgt 带 `tid` 字段，LIO 为 `targetcli` 解析结果），Control Plane 原样透传。

#### Path 参数

| 参数 | 必填 | 说明 |
|---|---:|---|
| `agent_id` | 是 | Agent 标识，对应 `config/agents.yml` 的 key |

#### curl

```bash
curl -s "$BASE_URL/agents/storage-lio-01/luns" \
  -H "Authorization: Bearer $TOKEN"
```

#### 成功返回示例

```json
[
  {
    "iqn": "iqn.2026-07.com.controller:worker-01.ubuntu",
    "luns": [
      {
        "backing": "/home/iscsi_img/worker-01.ubuntu.img"
      }
    ]
  }
]
```

### 14.2 POST /agents/{agent_id}/luns/disk

在指定 Agent 上创建磁盘 LUN。传 `master` 走母盘克隆（优先 btrfs / ZFS(≥2.2) reflink 秒级），传 `size` 建空白盘（sparse）。Agent 未配置 `role.disk` 时返回 `400 agent ... not configured for disk role`；已停用（排空）时返回 `409 agent ... disabled: not accepting new LUNs`；nvmet 数据面下母盘为 512 字节扇区布局的 GPT 时返回 `400`（并给出 4K 重建指引，见 15 节 `gpt` 字段）。

#### Path 参数

| 参数 | 必填 | 说明 |
|---|---:|---|
| `agent_id` | 是 | Agent 标识 |

#### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `iqn` | 是 | target IQN（盘 NQN 的派生形态），必须以该 Agent 的 IQN base（源自 `IPXE_NQN_BASE`）为前缀 |
| `filename` | 否 | backing 文件名；不传时由 Agent 按 IQN 自动生成 |
| `master` | 条件必填 | 母盘文件名（存在 `DISK_DIR` 下），与 `size` 二选一 |
| `size` | 条件必填 | 空白盘大小，如 `40G`，与 `master` 二选一 |

#### curl

```bash
# 从母盘克隆
curl -s -X POST "$BASE_URL/agents/storage-lio-01/luns/disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "iqn": "iqn.2026-07.com.controller:worker-02.ubuntu",
    "master": "_tpl_ubuntu_2204.img"
  }'

# 建空白盘
curl -s -X POST "$BASE_URL/agents/storage-lio-01/luns/disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "iqn": "iqn.2026-07.com.controller:worker-02.ubuntu",
    "filename": "worker-02.ubuntu.img",
    "size": "40G"
  }'
```

#### 成功返回示例

```json
{
  "iqn": "iqn.2026-07.com.controller:worker-02.ubuntu",
  "backing": "/home/iscsi_img/worker-02.ubuntu.img"
}
```

### 14.3 POST /agents/{agent_id}/luns/cd

在指定 Agent 上创建 CD（ISO 虚拟光驱）LUN。仅 `role.cd` 为 true 的 Agent 支持；未配置 cd 角色（如 LIO）时返回 `400 agent ... not configured for cd role`，后端能力限制由 Agent 透传；已停用（排空）时返回 `409 agent ... disabled: not accepting new LUNs`。

#### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `iso` | 是 | ISO 文件名（存在于 `DISK_DIR` 下） |
| `iqn` | 否 | target IQN；不传时由 Agent 按派生 IQN base（源自 `IPXE_NQN_BASE`）:iso文件名 自动生成 |

#### curl

```bash
curl -s -X POST "$BASE_URL/agents/controller-stgt/luns/cd" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "iso": "Win11_24H2.iso"
  }'
```

### 14.4 DELETE /agents/{agent_id}/luns

删除指定 Agent 上的一个 LUN/target。

#### Query 参数

| 参数 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `iqn` | 是 | 无 | 要删除的 target IQN |
| `delete_file` | 否 | `false` | 是否连 backing 文件（`.img`/`.iso`）一起删 |
| `ignore_missing` | 否 | `false` | Agent 返回 `404 iqn not found` 时是否忽略并视为成功 |

#### curl

```bash
# 只删 target，保留 backing 文件
curl -s -X DELETE "$BASE_URL/agents/storage-lio-01/luns?iqn=iqn.2026-07.com.controller:worker-02.ubuntu" \
  -H "Authorization: Bearer $TOKEN"

# 连 backing 文件一起删，target 已不存在也继续
curl -s -X DELETE "$BASE_URL/agents/storage-lio-01/luns?iqn=iqn.2026-07.com.controller:worker-02.ubuntu&delete_file=true&ignore_missing=true" \
  -H "Authorization: Bearer $TOKEN"
```

#### 成功返回示例

```json
{
  "deleted": "iqn.2026-07.com.controller:worker-02.ubuntu",
  "delete_file": false
}
```

忽略缺失时返回：

```json
{
  "deleted": "iqn.2026-07.com.controller:worker-02.ubuntu",
  "delete_file": true,
  "ignored_missing": true
}
```

### 14.5 POST /agents/{agent_id}/luns/scan

触发 Agent 扫描镜像目录，为缺失的 `.img`/`.iso` 文件重建 target（文件即真相）。stgt 后端返回重建结果；LIO 后端因 `saveconfig` 持久化，通常全部跳过。

#### curl

```bash
curl -s -X POST "$BASE_URL/agents/storage-lio-01/luns/scan" \
  -H "Authorization: Bearer $TOKEN"
```

#### 成功返回示例

```json
{
  "created": [
    {
      "iqn": "iqn.2026-07.com.controller:worker-02.ubuntu",
      "cd": false
    }
  ],
  "skipped": []
}
```

---

## 15. GET /masters（母盘清单）

### 说明

聚合列出全部**启用磁盘角色**（`enabled=true` 且 `role.disk=true`）Agent 上的母盘清单。母盘由存储节点 Agent 的后台扫描线程周期扫描（默认每 30 秒），识别 `DISK_DIR` 下文件名含 `_tpl_` 标记（大小写不敏感）的镜像文件（如 `_tpl_ubuntu_2204.img`）。

供 WebUI 母盘克隆下拉列表选盘。**与创建 Worker 的 API 无联动**——纯只读查询，不改任何状态，不写台账。

母盘清单项带 `gpt` 字段（GPT 扇区布局探测：`4k` / `512` / `none`）。nvmet 数据面下 **`512` 布局母盘不可克隆**——file backend 按底层 FS 块（典型 4K）导出逻辑块，512 字节扇区 GPT 被发起端按 4K 解释、EDK2 找不到 ESP（实测引导失败）；Agent 建盘时直接拒绝（见 14.2），前端据此标注。

### 失败容错

- 单台 Agent 不可达或鉴权失败：该节点返回 `error` 字段并记审计 `master.list`（failed），**不阻塞整体**；
- 全部节点失败：整体返回 `502`；
- 部分成功 / 无可用节点：返回 `200`（无节点时 `agents` 为空数组）。

### curl

```bash
curl -s "$BASE_URL/masters" \
  -H "Authorization: Bearer $TOKEN"
```

### 成功返回示例

```json
{
  "agents": [
    {
      "agent": "storage-lio-01",
      "storager_ip": "192.168.80.3",
      "masters": [
        {"name": "_tpl_ubuntu_2204.img", "size": 10737418240, "mtime": 1785643200, "gpt": "4k"},
        {"name": "_tpl_debian_12.img", "size": 8589934592, "mtime": 1785729600, "gpt": "4k"}
      ]
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `agents` | 数组，每项对应一个启用磁盘角色的 Agent |
| `agents[].agent` | Agent 编号（`config/agents.yml` 的 key） |
| `agents[].storager_ip` | 数据面地址（与 `/boot-vars` 相同的回退规则） |
| `agents[].masters` | 母盘数组，每项 `{name, size, mtime, gpt}`：文件名 / 字节大小 / 修改时间戳 / GPT 扇区布局（`4k` / `512` / `none`） |
| `agents[].error` | 该节点查询失败时的错误详情（成功节点无此字段） |

---

## 16. 设备池（设备台账）

设备台账（`state/devices.yml`）是三层实体模型（设备 / Worker / 系统盘）的底层实体：窗口期注册与手动导入只入设备池，**注册 ≠ 授权**——设备需绑定到 Worker 后才有身份。绑定关系权威在设备侧（`bound_worker_id`），Worker 侧只投影不存储。

**绑定语义（P2）**：设备 ↔ Worker 严格**一对一**。绑定走 `POST /devices/{mac}/bind`（16.7）；`force=true` 原子换绑（预校验 → 新绑定落盘 → 旧绑定清除 → 失败回滚）。解绑（`DELETE /devices/{mac}/bind`，16.8）后设备回池，Worker 的系统盘保留。删除 Worker 联动解绑（11 节）。`POST /workers` 传 `mac` 也直接完成绑定（7 节）。

**readiness 投影**：Worker 响应（列表 / 详情 / 状态）按台账派生两个字段：

- `bound_device`：绑定到该 Worker 的设备 MAC（无则 `null`）
- `readiness`：`ready`（绑定设备且有系统盘）/ `partial`（绑定设备**或**有系统盘）/ `idle`（两者皆无）

### 设备状态

| 状态 | 说明 |
|---|---|
| `pooled` | 在设备池中，未绑定（自动入池 / 手动注册 / 批量导入） |
| `bound` | 已绑定到 Worker（一对一），绑定关系见 `bound_worker_id` |
| `revoked` | 已注销（吊销），不再接受上报，不可重新注册 |

### 记录结构（示例）

```yaml
devices:
  "00:0c:29:b9:8b:2d":
    mac: 00:0c:29:b9:8b:2d
    uuid: "4c4c4544-..."          # SMBIOS UUID（双因子，可选）
    state: pooled                 # pooled | bound | revoked
    bound_worker_id: null         # 绑定关系权威在此，worker 侧只投影
    key_hash: null                # 安全蓝图阶段填充，本期留空
    source: ipxe                  # ipxe（自动入池）| manual（手动录入/导入）
    fingerprint:                  # 申报性质，设备上报更新
      manufacturer: ASUSTeK COMPUTER INC.
      product: ROG Zephyrus G15
      serial: "..."
      cpumodel: "Intel(R) Core(TM) Ultra 7 155H"
      mem_total: 32768            # 归一化十进制（兼容 0x hex 上报）
      mem_type: DDR5
      mem_speed: 5600
      chip: RTL8125
      busid: "0110ec8125"
    first_seen: 2026-08-15T10:00:00+08:00
    last_seen: 2026-08-15T12:00:00+08:00
```

### 16.1 GET /devices

设备池列表，`state` 过滤（`all` / `pooled` / `bound` / `revoked`，默认 `all`）。

**curl**：

```bash
curl "$BASE_URL/devices?state=pooled" \
  -H "Authorization: Bearer $TOKEN"
```

**成功返回**：数组，每项为一条设备记录（见上「记录结构」）。

### 16.2 GET /devices/{mac}

单设备详情（绑定 Worker、指纹、首/末次上报）。`mac` 用带冒号格式（`00:0c:29:b9:8b:2d`）。

**404**：设备不存在。

### 16.3 POST /devices

手动注册设备：MAC（+可选 UUID/型号/序列号）入池。

**请求体**：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `mac` | 是 | str | MAC 地址 |
| `uuid` | 否 | str | SMBIOS UUID（双因子，可选） |
| `manufacturer` / `product` / `serial` | 否 | str | 申报信息，仅作台账初始值，设备上报后以申报值为准更新 |

**成功返回（201）**：设备记录（`state=pooled`，`source=manual`）。

**409**：设备已存在（含已吊销——吊销设备不可重新注册）。

### 16.4 POST /devices/import

批量导入设备清单（MAC 清单预导入）：逐项独立，重复跳过，非法/吊销计 `failed`。

**请求体**：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `entries` | 是 | array | 清单数组，每项同 16.3 请求体（`mac` 必填） |

**成功返回**：

| 字段 | 说明 |
|---|---|
| `created` | 本次新增入池的 MAC 列表 |
| `skipped` | 已存在（pooled/bound）跳过项及原因 |
| `failed` | 非法 MAC / 已吊销项及原因 |

### 16.5 DELETE /devices/{mac}

注销设备（吊销）：`pooled` → `revoked`，设备保留在台账（审计保留）。

**409**：设备已绑定 Worker（须先解绑）或已吊销。

### 16.6 GET /devices/report

iPXE 设备信息上报入口（**不鉴权**，由 `boot.ipxe.cfg` 在请求 `/boot-vars` 之前先 `chain`）：更新指纹 + `last_seen`；未知 MAC 仅在注册窗口开启、放行额度未用尽且带有效公钥时入池。**返回空脚本体**（`#!ipxe\n`，`chain` 可直接消费，无脚本副作用；空 body 会被部分 iPXE 报 EOF，故统一返回合法空脚本）。

| 参数 | 必填 | 说明 |
|---|---|---|
| `mac` | 是 | MAC，支持带冒号（`${mac}`）与 hex 无分隔（`${netX/mac}`）格式 |
| `uuid` / `manufacturer` / `product` / `serial` / `cpumodel` / `mem-type` / `chip` / `busid` | 否 | 字符串字段，空值容忍 |
| `mem-total` / `mem-speed` | 否 | 整数，兼容 `0x` hex 与十进制，归一化十进制存储 |
| `pubkey` | 否 | ECDSA P-256 公钥（130 hex 未压缩点），注册/认领用；无参数或非法 → 忽略 |

行为：

- 已注册设备：更新指纹（非空字段覆盖）+ `last_seen`，`state` 不变；窗口开启且带有效公钥时填充 `key_hash`（密钥认领，不受放行额度约束），密钥不一致 → 拒绝覆盖（仅审计 `device.claim rejected`）
- 吊销设备：忽略（不更新、不复活）
- 未知 MAC + 注册窗口开启 + 额度未用尽 + 带有效公钥：入池（`state=pooled`，`key_hash` 填充，`source=ipxe`）
- 未知 MAC + 窗口关闭 / 额度用尽 / 无公钥：只更新指纹不入池（注册只在窗口期且受放行额度约束，见 5.1）

**curl**（模拟 iPXE 上报；引导链走**裸路径**，不经 `/api/cp` 前缀，且无鉴权）：

```bash
curl "https://<host>/devices/report?mac=000c29b98b2d&uuid=4c4c4544-...&manufacturer=ASUSTeK%20COMPUTER%20INC.&product=ROG%20Zephyrus%20G15&cpumodel=Intel(R)%20Core(TM)%20Ultra%207%20155H&mem-total=0x8000&mem-type=DDR5&mem-speed=5600&chip=RTL8125&busid=0110ec8125"
```

### 16.7 POST /devices/{mac}/bind

绑定设备到 Worker（一对一授权）。设备或 Worker 已绑定时默认 **409**；`force=true` 执行**原子换绑**：预校验 → 新绑定落盘 → 旧绑定清除（旧设备回池）→ 失败时恢复台账快照并尽力恢复 dnsmasq（见 17 节「实现边界」）。幂等：同一设备重复绑定同一 Worker 返回 `200` 且不改动。

| Query 参数 | 必填 | 默认 | 说明 |
|---|---:|---|---|
| `worker_id` | 是 | — | 目标 Worker |
| `force` | 否 | `false` | 设备或 Worker 已绑定时原子换绑 |

**404**：设备或 Worker 不存在。**409**：设备已吊销 / 已绑定（未传 `force`）/ Worker 已绑定（未传 `force`）/ dnsmasq 冲突。

`force=true` 换绑场景：

- 设备已绑 `worker-01`，换到 `worker-02` → 设备迁移，`worker-01` 变空转（无设备）
- `worker-02` 原绑定的其他设备 → 回池
- 设备已绑 `worker-02` 且 `worker-02` 绑定该设备 → 幂等成功

审计记录 `device.bind`（含 `old_worker_id` / `old_device_mac`，即换绑历史）。

**curl**：

```bash
curl -X POST "$BASE_URL/devices/00:0c:29:b9:8b:2d/bind?worker_id=worker-01&force=false" \
  -H "Authorization: Bearer $TOKEN"
```

**成功返回（200）**：设备记录（`state=bound`，`bound_worker_id=worker-01`）。

### 16.8 DELETE /devices/{mac}/bind

解绑设备：回池（`state=pooled`、`bound_worker_id=null`），移除 dnsmasq 绑定并重载；Worker 的系统盘保留（其 `readiness` 降级为 `partial`/`idle`）。

**409**：设备未绑定。**404**：设备不存在。

**curl**：

```bash
curl -X DELETE "$BASE_URL/devices/00:0c:29:b9:8b:2d/bind" \
  -H "Authorization: Bearer $TOKEN"
```

### 16.9 POST /devices/bind/batch/preview

批量绑定**预览**（只读，不写任何东西）：把清单配成配对表。

**请求体**：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `mode` | 否 | `manifest`（默认）：用 `pairs`；`sequential`：`macs[i]` ↔ `worker_ids[i]` 按下标配对（长度不等 → `400`） |
| `pairs` | 否 | 数组，每项 `{mac, worker_id, manufacturer?, product?, serial?, uuid?}`；可选字段为**申报比对列**，与设备上报指纹比对（见下） |
| `macs` / `worker_ids` | 否 | `mode=sequential` 使用 |

逐项归类（独立判定，不整批拒绝）：

- `matched`：设备在池未绑定 + Worker 存在 + Worker 未绑定（含 `device_state`、`worker_state`、`fingerprint_mismatch`）
- `conflicts`：设备已绑定 / Worker 已绑定 / 清单内重复 MAC / Worker 不存在
- `not_found`：设备池外（`device not in pool`）、已吊销、MAC 非法

`fingerprint_mismatch` 一致时为 `null`，否则 `{"fields": ["serial", ...]}`——申报值与上报值（两者均非空）不符的列。申报性质，不阻断绑定。

**成功返回**：`{matched: [...], conflicts: [...], not_found: [...], summary: {total, ok, conflict, not_found}}`。

### 16.10 POST /devices/bind/batch

批量绑定**执行**（幂等，逐项独立，单项失败不影响其余）。请求体同 16.9。归类：

- `succeeded`：本次绑定成功（申报值与上报值不符时带 `fingerprint_mismatch` 标记）
- `skipped`：已绑定（同 Worker）/ 设备已绑定其他 Worker / Worker 已绑定 / 清单内重复 MAC
- `failed`：设备不存在（池外——先经 16.3/16.4 入池）/ MAC 非法

**审计**：除 `device.bind.batch` 汇总外，每个 `succeeded` 项另逐条记录 `device.bind`（`mac` / `worker_id`），保证设备绑定历史（`GET /operations?mac=`）完整；`skipped` / `failed` 仅在汇总计数。

**成功返回**：`{succeeded: [...], skipped: [...], failed: [...]}`。已完成的清单重跑全部 `skipped`。

**curl**：

```bash
curl -X POST "$BASE_URL/devices/bind/batch" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"mode":"manifest","pairs":[{"mac":"00:0c:29:b9:8b:2d","worker_id":"worker-01"}]}'
```

---

## 17. 当前实现边界

当前版本已经支持：

- Worker 身份注册（hostname 绑定；`mac` 可选，传了直接绑定设备）
- 批量创建 Worker（数量 + 命名规则，`macs` 可选直接绑定，`POST /workers/batch`，7.6 节）
- 设备台账（自动入池 / 手动注册 / 批量导入 / 注销吊销，`/devices` 系列端点）
- 设备↔Worker 一对一绑定（绑定 / force 换绑 / 解绑 / 批量绑定预览+执行，16.7–16.10）
- Worker `mac` 换绑映射（`PUT /workers/{worker_id}/mac`，7 节）
- 删除 Worker 联动解绑（11 / 11.1 节）
- boot-vars 防冒领（绑定即认证，5 节）
- iPXE 设备信息上报（11 字段指纹，`GET /devices/report`，窗口期注册只入池不建 Worker）
- Worker 系统盘创建（`POST /workers/{worker_id}/luns/disk`）
- Worker 默认启动配置设置（默认盘 / 菜单项 / 超时，`PUT /workers/{worker_id}/default-disk`）
- Worker 删除
- Agent 选择
- Agent LUN 直管（列出 / 创建磁盘 / 创建 CD / 删除 / 扫描）
- 母盘清单查询（`GET /masters`，存储节点后台周期扫描缓存）
- Windows ISO 特例
- dnsmasq 主机名绑定
- Worker 与操作轨迹查询
- 多系统盘（一个 Worker 可挂载多个系统盘：同一 (os, os_version) 至多一块、不同版本可并存，盘级 `os_tag` 区分，`default_disk` 决定默认启动）
- 母盘标签登记（os / os_version 备注，`PUT/DELETE /agents/{agent_id}/masters/{master_name}/tag`，6.4 节；`/masters` 聚合合并展示）
- NVMe-oF 认证凭据库（DHHC-1，按 Worker 跟盘，7.7 节；/boot-vars 注入 `nbft_secret`，5 节）
- 凭据签发与推送驱动（nvmet 节点建盘即自动签发密钥、凭据设置/吊销、绑定/解绑/换绑、删盘、删 Worker 时推送 Agent，Agent 转调 nvmet 宿主服务同步 hosts 矩阵）+ 推送对账（状态台账 + 周期幂等重放未送达项，push 投影可见，7.7 节）

当前版本还没有做：

- 自动 IP 管理
- 自动母盘生命周期管理
- 定时 reconcile
- 数据盘挂载（`/luns/data` 命名空间已预留）
- 文件存储无真正事务：回滚 = 重写；换绑清除旧绑定失败且恢复也失败时，靠审计定位手动处理（16.7）
---

### 各组件使用以下端口：

#### 控制面宿主
- dnsmasq: `53`（DNS 默认监听）、`67`（DHCP）、`69`（TFTP）
- nginx: `80`（仅 301 升级到 443）、`443`（唯一对外入口：WebUI / 管理面 API / 引导链 / 文件下载）
- Control_Plane: `4839`（仅绑 `127.0.0.1`，供 `kurrent init` 健康检查与调试，不在请求路径上）

#### 存储节点
- Agent: `4840`（mTLS，控制面调用）
- nvmet-host: `4841`（mTLS，Agent 经 `host.docker.internal:4841` 调用）
- NVMe-oF 数据面: `4420`（nvmet 端口，无 TLS；认证走 DH-HMAC-CHAP）
- LIO / stgt（iSCSI 数据面）: `3260`
