# kurrent CLI

Kurrent 控制面命令行工具。仅一个第三方依赖（`gopkg.in/yaml.v3`，其余为标准库 `net/http` + `encoding/json`），单二进制分发，覆盖集群生命周期闭环：**`config print` 生成声明模板 → `init` 控制面初始化 → `token create` 签发引导凭据 → `join` 节点侧加入**。声明式配置（`yml` 是唯一输入，CLI 只负责校验与收敛启动——配置即声明、CLI 即工具）。

## 构建

```bash
cd cli
go build -o kurrent .        # 单二进制（约 9.6 MB）；版本号发版时注入，见下方

# 发版构建（版本号注入 + 交叉编译示例）：
#   go build -ldflags "-X main.version=v0.3.0" -o kurrent .

# 可选：全局安装（此后任意目录直接用 kurrent 命令，部署指南即此形态）
sudo install -m 0755 kurrent /usr/local/bin/kurrent
# 首次在仓库内执行一次 kurrent credentials install：把管理凭据快照到 ~/.kurrent，
# 全局二进制脱离仓库也能 mTLS 认证（见下方「配置」）
```

## 配置

```bash
# 方式一（推荐）：自动发现管理凭据——仓库内任意目录可用
cd /path/to/kurrent
kurrent agents list          # 自动读 control_plane/state/admin.conf [admin 证书 443 mTLS]

# 方式一b：脱离仓库（PATH 安装的二进制 / 异地执行）——一次性安装用户级快照
kurrent credentials install  # 复制 admin.conf + 证书副本到 ~/.kurrent（自包含，可整包拷走）
kurrent agents list          # 此后任意目录可用；凭据轮换后重跑 install 刷新

# 方式二：显式指定管理凭据文件或管理 token（也是环境变量兜底项）
export KURRENT_CREDENTIALS=/path/to/admin.conf  # 默认发现 control_plane/state/admin.conf
export KURRENT_CP_URL=https://<host>/api/cp     # 无管理凭据时须显式指定（配合 token）
export KURRENT_CP_TOKEN=<admin-token>           # kurrent token create --usage admin
kurrent agents list
```

- `--credentials` 管理凭据文件（admin 客户端证书走 443 mTLS；kurrent 原生扁平 YAML）
  ——发现顺序：`--credentials` ＞ `KURRENT_CREDENTIALS` ＞ 自动发现
- 自动发现（命中返回绝对路径）：① 从当前目录**逐级上溯** `control_plane/state/admin.conf`
  （所以在仓库内任意子目录、含 `cli/` 下执行都可用）；② 再退回可执行文件旁
  `<exe 目录>/../control_plane/state/admin.conf`（仓库布局 `cli/kurrent` → 仓库根，
  覆盖从仓库外调用该二进制的情形）；③ 垫底用户级快照 `~/.kurrent/admin.conf`
  （仓库源在场时优先实时源，脱离仓库才吃快照）。都找不到时若无 `--server` 会直接报错
  并给出安装/连接指引；给出 `--server` + `--token` 才走无凭据的 Bearer 通道——管理面
  只监听 unix socket，不再提供本机回环端口
- `kurrent credentials install`：把仓库管理凭据**复制**为自包含快照 `~/.kurrent/`
  （重写证书键为 `pki/<文件名>` 相对路径 + `installed-from`/`installed-at` 溯源；
  目录 0700、文件 0600）——快照是复制而非跟随，**凭据轮换后重跑 install 刷新**；
  `credentials show` 查看生效凭据与快照状态；`credentials remove` 删除快照
- `--server` 控制面 API 根：有管理凭据时覆盖其中的 server 地址（远程管理员指向
  `https://<host>/api/cp`；凭据文件缺省 server 为 `https://127.0.0.1/api/cp`）；
  无管理凭据时没有默认值，必须显式给出并配合 `--token`
- `--token` 管理 token（`kurrent token create --usage admin` 产物；认证器链 Bearer）

## 命令

```text
kurrent [--credentials PATH] [--server URL] [--token T] <group> <verb> [args]

  agents list [--no-live] [-o table|json]    列出 Agent（含存活探测）
  agents get <id>                             查看单个 Agent
  agents add <id> --base-url URL [--role disk,cd] [--storager-ip IP] [--tags a,b] [--remark TEXT] [--disabled]
                                              <id> 为机器身份（12 位小写 hex）；描述性名称用 --remark
  agents edit <id> --base-url URL [...]       更新 Agent（全量覆盖，参数同 add；不带
                                              --remark/--tags 即清空对应字段）
  agents remove <id>                          删除 Agent 台账（含母盘标签）
  agents probe --base-url URL [--agent-id ID] 探测 Agent 能力（不落盘）
  token create [--usage bootstrap|admin] [--agent-id ID] [--cp-url URL]
               [--uses N] [--scope admin,readonly,enroll] [--ttl 24h]
                                              签发凭据：
                                              bootstrap（默认）引导 token——绑定节点
                                              身份（--agent-id 为 12 位小写 hex；省略则生成），
                                              描述性名称写节点声明 metadata.remark；
                                              enroll 只接受该节点、默认一次性、
                                              60 分钟；--uses N 放开次数（1..1000，
                                              且不超过控制面 bootstrapTokenMaxUses，
                                              默认 100）；输出带地址与节点 id 的 join
                                              命令；admin 管理 token（认证器链 Bearer，
                                              供脚本与 WebUI 登录；--scope 收窄权限，
                                              --ttl 默认 24h，上限由控制面
                                              tokenMaxTtlHours 决定，默认 720h）
  token list [-o table|json]                  列出未过期 token 台账（不返回明文摘要；
                                              与 WebUI「凭据」页同源）
  token revoke <token_id>                     吊销 token：删除台账条目，
                                              立即失效、无法恢复（只传点号前的
                                              6 位 token_id，别粘贴完整 token）
  sessions list [-o table|json]               管理面会话台账（session_id = sha256(jti)）
  sessions revoke <session_id>                逐个吊销：删登记，下一请求立即失效
  sessions revoke-all                         全部下线：清空登记并递增 epoch
  cert keygen <cn> [--out DIR] [--force]      本地生成 RSA-2048 私钥与 CSR：写
                                              <DIR>/<cn>.key（0600）与 <cn>.csr，
                                              私钥永不上送（默认 DIR=.）
  cert issue <cn> --csr FILE [--days N] [--out DIR] [--force]
                                              把本地 CSR 交给 users CA 签发（用户须先在
                                              users.yml 登记），写 <DIR>/<cn>.crt
  cert list [-o table|json]                   列出 users CA 签发的证书（含吊销/过期）
  cert revoke <cn> [--serial HEX] [--reason REASON]
                                              吊销证书（--serial 只废一张，只踢绑定该
                                              证书的会话）：重签 CRL + reload nginx +
                                              踢会话，下一次 TLS 握手即拒绝
  cert components [-o table|json]             列出 agent / nvmet-host 的 nodes 域证书
                                              （含吊销/过期）
  cert revoke-component <agent_id> [--serial HEX] [--reason REASON]
                                              吊销组件证书：写 nodes CRL + reload nginx
                                              + 删除该节点的 bootstrap token
  credentials install | show | remove         管理凭据用户级快照：install 复制仓库
                                              admin.conf（含证书副本）到 ~/.kurrent，
                                              此后任意目录可运行 kurrent（凭据轮换后重跑
                                              install 刷新）；show 查看生效凭据与快照状态；
                                              remove 删除快照（仓库内凭据不受影响）
  config print init-defaults|node-defaults   输出声明模板（重定向为
                                              control_plane/kurrent.yaml 或
                                              storager/kurrent.yaml 后编辑）
  config validate [--node] [--config PATH] [--dir REPO]
  config show     [--node] [--config PATH] [--dir REPO]
                                              在组件镜像内按运行时 schema 全量校验 /
                                              输出生效声明（默认值已注入）；--node 或
                                              kind=NodeConfiguration 时校验节点声明
  config status                              运行态声明、configHash、dnsmasq.conf 与
                                              服务器证书漂移（经 443 管理面）
  init [--config PATH] [--dir PATH]          控制面初始化：全量校验声明并收敛启动；
                                              健康检查失败回滚上一份声明并非零退出
                                              （默认读 control_plane/kurrent.yaml；
                                              --config 指定其他声明文件则应用之）
  join <cp-url> [--agent-id ID] [--config PATH] [--token T] [--dir PATH]
                                              节点侧加入：
                                              声明 kurrent.yaml 缺失自动生成、已存在则读入并
                                              同步地址，收敛启动 agent（幂等可重跑）；
                                              --agent-id 写入声明 metadata.name（token 绑定
                                              的节点身份，token create 输出的命令已携带）；
                                              nvmet 后端预检宿主前置（内核模块 + configfs +
                                              tlshd >= 1.5.0），未就绪指路 nvmeof/host-prepare.sh
  workers list [-o table|json]                Worker 台账（只读）
  workers get <id>                            Worker 详情（JSON）
  workers credential show <id> [-o table|json] 查看 NVMe-oF 认证密钥与推送状态
  workers credential set <id> (--secret-file PATH | --secret-stdin | --generate)
                                              轮换认证密钥（不接受命令行明文；
                                              --generate 就地生成 32 字节随机密钥，
                                              不打印明文；固件经 /boot-vars 注入）。
                                              nvmet 建盘已自动签发，此处只用于显式轮换
  workers credential revoke <id>              吊销认证密钥（下次引导被拒绝；未送达由
                                              控制面对账自动重试）
  ops list [--limit N] [-o table|json]        操作审计日志（只读）
  version                                     输出版本号（发版构建 -ldflags 注入，如 v0.3.0）
```

位置参数与 `--key value` / `--key=value` / `--flag` 可任意混排（如 `agents add 0123456789ab --base-url https://x`）。
所有命令支持 `-h`/`--help`；未知标志、缺值、重复标志与多余位置参数均报用法错误。
退出码：`0` 成功/帮助，`1` 运行失败，`2` 用法错误。值以 `-` 开头时用 `--key=value` 或以 `--` 分隔位置参数。

## 控制面初始化

声明文件 `control_plane/kurrent.yaml` 即用户维护的权威配置（yml 是唯一输入）：

```bash
# 1) 生成带注释模板（与 kurrent.yaml.example 同源），编辑 spec.networking 五键（必填）
./kurrent config print init-defaults > control_plane/kurrent.yaml
$EDITOR control_plane/kurrent.yaml

# 2) 写盘前全量校验（组件镜像内与运行时同一 schema；未知字段/跨字段语义/策略上限）
./kurrent config validate
./kurrent config show        # 可选：查看注入默认值后的生效声明

# 3) 一条命令到运行态：构建校验镜像 → 全量校验 → 应用声明 → 启动/重启 → 等待 /healthz
#    → 重启 dnsmasq 并核验其保持运行
./kurrent init
#    → 幂等可重跑：改声明后重跑即生效；--config 指向其他路径 = 应用该声明为控制面配置
#    → 校验失败不写盘；启动失败自动回滚上一份声明并恢复运行，退出码非零

# 4) 运行态核对：生效声明、configHash、dnsmasq.conf 与证书漂移
./kurrent config status
```

**更新声明**：编辑 `control_plane/kurrent.yaml` → `config validate` → `init`。`serverCert` 的 SAN/天数变化默认只报告漂移（`config status`），设 `rotateOnChange: true` 才重签（TOFU 指纹变化，已入池设备需重新信任）。

## 一键加入流程

```bash
# 1) 控制面：签发绑定节点身份的 bootstrap token；输出带地址与节点 id 的 join 命令
./kurrent token create --agent-id 3f2a9c1d8b7e --cp-url https://192.168.1.10
#    → kurrent join https://192.168.1.10 --token a1b2c3.0123456789abcdef --agent-id 3f2a9c1d8b7e
#    （省略 --agent-id 则生成随机 12 位小写 hex 身份，join 命令同样携带；
#      描述性名称写入 metadata.remark）

# 2) 节点（存储服务器上，仓库根目录执行——命令已携带地址与节点 id，粘贴即用）：
./kurrent join https://192.168.1.10 --token a1b2c3.0123456789abcdef --agent-id 3f2a9c1d8b7e
#    → storager/kurrent.yaml 缺失时按模板自动生成（url=命令地址、name=自动生成的
#      随机标识、backend nvmet、advertiseUrl 推导、diskDir/nqnBase 默认），已存在则
#      读入合并并同步地址（非 forbid：手工编辑保留；yml 即 kurrent.yaml 后续可编辑）
#    → bootstrap/agent.token（0600）+ .env 插值键 + docker compose 收敛启动 agent
#    → nvmet 后端先预检宿主前置（nvmet/nvmet-tcp 模块 + configfs 挂载）；未就绪报错
#      并指路 sudo bash storager/nvmeof/host-prepare.sh（幂等，含重启持久化）
#    仓库根以外执行需 --dir 指定 storager 目录

# 3) 验证：
./kurrent agents list
```

预置声明（可选）：`kurrent config print node-defaults > storager/kurrent.yaml` 生成带注释模板后编辑（改 backend/diskDir/advertiseUrl 等），再执行 join——join 读入并更新，手工字段保留。模板 `metadata.name` 留空（join 写入身份）；单独校验用 `kurrent config validate --node`（仓库内执行，使用 storager-agent 镜像）。

要点：

- bootstrap token 为**绑定节点身份**的引导凭据（`<6位>.<16位>`，控制面只存 sha256 摘要）：签发时指定 `--agent-id`（= 节点声明 `metadata.name`；只接受 12 位小写 hex，与盘 os_tag 同构；省略则生成），enroll 只接受同一身份；描述性名称归属 `metadata.remark` 备注；控制面地址由 join 命令携带（同步进声明 `spec.controlPlane.url`）
- token **默认一次性**（TTL 60 分钟、上限 24 小时）：使用额度在签发前原子占用、签发失败退回；批量引导用 `--uses N`（1..1000，且不超过控制面 `bootstrapTokenMaxUses`，默认 100）显式放开。每次 `token create` 都是新签，旧 token 到期自然失效、也可随时 `token revoke <token_id>` 吊销（删条目即失效，Bearer 校验无缓存）；token 不进 yml（引导凭据在独立文件 `storager/bootstrap/agent.token`）
- 管理 token 用 `--scope`（`admin` 默认 / `readonly` 只读 / `enroll` 仅签发引导 token）与 `--ttl`（默认 24h；上限由控制面 `tokenMaxTtlHours` 决定，默认 720h）收窄权限与寿命；台账记录 `last_used_at`，列表与 WebUI 可对照使用情况
- `nvmet-host` 组件凭据**不手工签发**：agent enroll 上报 `backend=nvmet` 时控制面派生随响应下发，agent 落盘 `storager/bootstrap/nvmet-host.token` 供其引导（能力上报驱动，签发不预知后端）
- nvmet 后端 join 在 agent 收敛启动后等待上述派生凭据重新写出（默认 60 秒），再自动重启 `nvmet-host`。组件证书被吊销后，签发新 bootstrap token（`kurrent token create` 或 WebUI「证书」→「恢复」）并重跑 join 即完成恢复——旧证书无需手工删除
- nvmet 后端节点**宿主前置**：内核 nvmet target 无法容器化（4421 数据面在宿主），`kurrent join` 预检内核模块（nvmet / nvmet-tcp）与 configfs 挂载——未就绪即报错并指路 `sudo bash storager/nvmeof/host-prepare.sh`（幂等，含重启持久化：模块写 `/etc/modules-load.d`、configfs 写 `/etc/fstab`；`--check` 只读诊断）；声明 `spec.agent.diskDir` 盘目录 join 自动创建兜底
- enroll 自动登记时携带 `base_url`（声明 `spec.agent.advertiseUrl`，默认推导 `https://<cp-host>:4840`）、`remark`（声明 `metadata.remark`，备注/人类识别用）与能力标签；特殊场景（NAT 等）编辑声明覆盖

## NVMe-oF 认证凭据

密钥在 **nvmet 节点上建系统盘时由控制面自动签发**（单盘与批量同一语义，批量 N 台一次到位），
无需逐台手工设置。以下命令用于**显式轮换 / 吊销**：

```bash
# 轮换：密钥就地生成（32 字节随机 + CRC32，DHHC-1 契约），不打印明文
kurrent workers credential set worker-01 --generate
#    → 输出逐 Agent 推送状态：已送达 / 部分生效（节点 TLS 未就绪） / 未送达（对账线程自动重试）

# 已有密钥轮换：从文件或 stdin 读取（不接受 --secret 命令行明文——会进进程列表与 shell 历史）
kurrent workers credential set worker-01 --secret-file /secure/dhhc1.key   # 文件须 0600
printf '%s\n' "$KEY" | kurrent workers credential set worker-01 --secret-stdin

kurrent workers credential show worker-01       # 密钥哈希前缀 + 推送状态（-o json 原始返回）
kurrent workers credential revoke worker-01     # 吊销（轮换 = 再 set 一次即可）
```

- 密钥明文只存在于控制面密钥库与推送链路（Agent 缓存 → configfs），CLI/WebUI 不保存、不打印
- 推送结果落状态台账（`state/credential-push.yml`），控制面对账线程（默认 60s）重放未送达项：
  轮换失败不会出现「boot-vars 新钥、configfs 旧钥」的引导失配，吊销失败不会旧钥滞留
- 凭据 opt-in 已随「建盘即签发」收窄：nvmet 建盘必已签发密钥，无密钥不推送的分支实际只剩
  非 nvmet 后端的盘（DHHC-1 不适用，推送只会得 `n/a`）——Agent 离线时不出现虚假「同步失败」
- 静态存储口径（2026-10-05 裁定）：四处明文（控制面 `credentials.yml`、Agent 缓存（含派生的 TLS PSK，均 0600）、
  存储节点 configfs `dhchap_key`、存储节点内核 `.nvme` keyring）不引入应用层加密，边界责任在部署方
  （磁盘/宿主持久化加密、root 访问边界）
- 数据面加密默认开启（NVMe/TCP TLS 1.3，存储节点单口 `4421`）：PSK 由认证密钥派生、随 `/boot-vars`
  下发（`nbft_tls_psk`/`nbft_tls_port`），iPXE 固件引导段与内核段共用本口、全程加密；
  `nbft-tls-*` 不满足条件（未绑定 / 无密钥 / 非 nvmet / 数据面端口未就绪）时整个引导变量不下发，
  控制面拒绝引导并记审计——无明文回退

## 输出

- 默认表格（`-o table`）；`-o json` 输出原始 JSON（便于 jq 管道）
- 空字段以 `—` 占位
- 帮助：每个命令支持 `-h`/`--help`；`kurrent help [group [verb]]` 查看同一份文本
- 退出码：`0` 成功/帮助；`1` 运行失败；`2` 用法错误（未知/重复/缺值标志、非法参数）

## 测试与回归

```bash
go vet ./... && go build -o kurrent .
# 全量回归（仓库根）：
.venv-linux/bin/python -m pytest tests/ -q
```
