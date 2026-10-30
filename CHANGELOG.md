# 更新记录 (CHANGELOG)

本文件记录 Kurrent（周流）项目的功能变更、接口调整与缺陷修复。

## 记录规范

- 每次代码 / 配置变更完成后，在对应日期区块追加条目（新增 / 变更 / 修复）
- **新增**：新功能、新端点、新配置项
- **变更**：行为调整、接口变更、数据模型调整
- **修复**：缺陷修复
- 记录行为与接口层面的变化；注释、措辞等无行为影响的调整不单列
- 涉及多个模块的改动，按模块分条列出；接口变更同时需同步 `api/control-plane-api.zh-CN.md` 与 `api/control-plane-api.en.md`（控制面 API 参考唯一权威；docs/ 文档站同步维护）

---

## 2026-10-30

### 新增

- **凭据推送新增 `degraded` 状态与 PSK 在位查询**：控制面推送结果新增 `degraded`（认证密钥已写入、目标端 TLS 数据面未就绪导致 PSK 未生效），CLI 与 WebUI（中英）同步显示。nvmet-host 新增 `GET /subsystems/{nqn}/tls/{hostnqn}` 只读查询内核 `.nvme` keyring 中的 PSK 是否在位；Agent 新增 mTLS 内部端点 `POST /credential/status` 供控制面对账回查

### 变更

- **凭据推送以「逐对应用」为准（fail-closed）**：控制面不再把 Agent 的 HTTP 200 视为送达——要求 Agent 回报 `tls_expected`（子系统数 × host 数）与 `tls_applied`，二者相等才记 `ok`；旧 Agent 不回报、部分应用或目标数不符一律记 `failed` 并持续重放。全部目标因宿主 TLS 端口未就绪（409）未应用时记 `degraded`。对账在台账为 `ok` 且哈希一致时仍回查 keyring 在位（设置态要求全部在位、吊销态要求全部不在位），宿主重启导致 keyring 丢失会自动补推。定期轮换遇到 `failed` / `degraded` 回滚旧密钥
- **凭据推送增加单调 `generation`**：每次推送分配递增序号，Agent 拒绝迟到的旧 generation，推送台账也拒绝旧 generation 回写；吊销删除条目后保留 generation 墓碑——并发/乱序重放不会用旧密钥覆盖新密钥
- **PSK 生命周期清理**：删除子系统时 nvmet-host 同步摘除其名下全部 `.nvme` PSK，服务启动时清理 subsystem 已不存在的孤儿 key；Agent 吊销改存 `secret: null` 墓碑而非删除缓存条目，保证对账持续摘除目标端 PSK
- **宿主前置预检 tlshd ≥ 1.5.0**：`storager/nvmeof/host-prepare.sh`（含 `--check`）与 `kurrent join` 的 nvmet 预检增加 tlshd 版本与服务运行状态检查，低于 1.5.0 或未运行直接失败；部署指南（中英）与 nvmet-host README 补充 1.5.0 源码安装、systemd drop-in 升级与验证步骤
- **单盘 Worker 自动成为默认启动盘**：单盘建盘时直接写入 `default_disk`（批量建盘原有语义不变；第二块及以后不自动改写默认盘）；删盘后只剩一块时该盘自动成为默认盘；控制面启动迁移为存量单盘 Worker 补齐 `default_disk`（幂等）。可观察差异：新建 Worker 无需再调 `PUT /workers/{worker_id}/default-disk` 才能引导系统盘，`/boot-vars` 的 `menu_default` 由 `reboot` 变为 `boot-os`

### 修复

- **PSK 轮换后旧密钥遮蔽新密钥（F-02）的检测与部署闭环**：ktls-utils 1.5.0 起 tlshd 优先查进程 keyring、再回落会话 keyring，更早版本会让会话中的旧 PSK 遮蔽 `.nvme` 中的新 PSK，而推送侧只看 HTTP 200 无法发现。现以逐对应用结果判定推送、对账回查 keyring 在位并清理旧 PSK，同时把 tlshd ≥ 1.5.0 作为 nvmet 宿主硬前置
- **nvmet-host 拒绝 512 字节扇区 GPT 的 backing**：建子系统时校验盘文件 GPT 布局，512 布局返回 400 并给出 4K 重建指引；此前只有控制面的母盘校验，手工/绕过路径仍可能建出不可引导的盘（演练中 iPXE 报 `EFIBLK cannot locate file systems` / `0x7f22208e`）。Agent 将宿主的 400 透传为 400，不再误报 503
- **MAC 换绑依赖 dnsmasq 主机名绑定（全新 Worker 无法换绑）**：`PUT /workers/{worker_id}/mac` 此前从 `dnsmasq/dhcp-hosts.conf` 反查旧 MAC，再以"该 hostname 有绑定"为前提替换；没有绑定的新 Worker 直接 `409 no dnsmasq binding`，投影丢失时也无法识别旧设备。现绑定关系以设备台账 `state/devices.yml` 为权威：旧设备按 `bound_worker_id` 反查，dnsmasq 改为幂等 upsert 投影（缺失补写、已有替换、清除该 MAC 的封禁行），投影写入失败回滚设备台账；`GET /workers` 与 Worker 详情的 `mac` 字段同样改从设备台账反查

---

## 2026-10-27

### 修复

- **4443 引导入口不再暴露 `/tftp/`**：Secure Boot 的静态脚本全部嵌入签名固件，L1 仍走 443 的 `/tftp/`；4443 只保留 `/file/` 与三个引导端点，减少读取面与路径解析面。nginx 静态断言与部署指南（中英）已同步
- **组件 CRL 首拉与 Agent 启动扫描增加重试**：nvmet-host / agent 的 CRL 初始拉取改为短重试（吸收控制面或 nginx 启动期的 502），仍失败则保持 fail-closed 并交给周期刷新；Agent 的启动扫描在宿主服务/CRL 未就绪时按指数退避重试，不再一次性放弃导致 4421/subsystems 长期缺失。新增 CRL 重试单测

---

## 2026-10-24

### 新增

- **组件证书吊销后的自动恢复**：被吊销的组件证书无法再完成 mTLS，组件改为启动时从公开只读入口 `GET /crl/nodes.crl`（nginx 直出，无认证、仅 `GET`/`HEAD`、`Cache-Control: no-store`）取 nodes CRL，用本地 nodes CA 校验签名、issuer 与 `nextUpdate` 后比对本地 client / serving 序列号，命中即用引导凭据重新 enroll——**不再需要手工删除节点上的证书**（`KURRENT_PKI_SELF_CHECK=0` 可关闭）。`kurrent join` 在重启 agent 后等待其写出派生的 nvmet-host 凭据，再自动重启 nvmet-host，恢复只需一条命令
- **WebUI 身份与访问页**：侧边栏「身份」，路由 `#/identity`（`#/credentials`、`#/certificates` 重定向到此），把凭据与证书两张台账合成一页。页头先给三个任务入口（接入存储节点 / 生成管理令牌 / 收回访问），再按下表四个访问主体分页签：**用户**（users CA，按 CN 聚合，组来自 `users.yml`，当前请求自身的证书不可吊销；下方为用户会话）、**存储节点**（nodes CA，按 `agent_id` 聚合 agent 与 nvmet-host 的 client + serving 当前证书，状态区分有效 / 部分可用 / 已吊销 / 已过期，未登记节点有标注；下方为 bootstrap / 派生节点令牌，一次性已用尽的令牌不再列出）、**控制面**（control CA，只读）、**自动化**（管理 token 与令牌会话，均可逐行吊销）。接入存储节点不要求填写名称：节点标识由前端生成 12 位 hex 并直接写入 join 命令，备注在接入后由 Agent 台账设置；吊销支持原因选择，节点恢复可签发一次性 bootstrap token（明文仅本次可见），`GET /pki/users` 增加 `groups`（组来自 `users.yml`，证书只承载 CN）
- **L1 引导信任档：吊销设备在 DHCP 层即被拒绝**：`DELETE /devices/{mac}` 现在同时清除该 MAC 的 dnsmasq 静态绑定并写入 `MAC,ignore`，被吊销设备不再取得租约（拒绝点前移到网络层，不再依赖无鉴权端点）；`DELETE /devices/revoked` 清除条目时移除对应封禁行，`POST /devices/report` 重新登记时清除历史遗留封禁。dnsmasq 写文件失败不回滚吊销结果，记 `device.dhcp_block` / `device.dhcp_unblock` 错误事件。部署指南（中英）新增「引导网络边界（L1 信任档）」一节，给出 VLAN/DHCP snooping/DAI/RA guard/端口安全/802.1X 清单与明确的残余风险（同二层伪造 DHCP/ARP 与控制面被攻陷不在 L1 覆盖范围内，L2/L3 未实施）
- **Secure Boot 引导档（可选，L2）**：控制面启动时幂等生成独立 boot CA（`state/pki/ca/boot/`，10 年）与 4443 叶证书（`state/certs/boot-server.*`，1 年、SAN 复用 `spec.serverCert.san`，剩余有效期不足 30 天在启动时重签且不动 boot CA）；nginx 新增 4443 server，只反代 `/boot-vars`、`/devices/report`、`/devices/challenge` 并提供 `/file/` 静态资产（Secure Boot 脚本已嵌入签名固件，4443 不暴露 `/tftp/`），443 的证书与 TOFU 行为不变。固件侧新增 `build-sb.sh`：内嵌静态脚本、以 `TRUST=` 钉 boot CA 指纹并以 `CERT=` 嵌入 CA 证书、关闭 TOFU，产物经 `sbsign` 签名并附 manifest。宿主端口用 `KURRENT_BOOT_HTTPS_PORT` 配置；新增离线密钥、USB 导出与目标机预检脚本（`tools/secureboot/`）
- **Secure Boot `db` 的 Redfish 预置工具**：新增 `tools/secureboot/enroll-redfish.sh`，通过 Redfish 读取 Secure Boot 与 `db`、要求已有 Microsoft UEFI CA、幂等导入产物签名证书（默认只追加，绝不清空键）、读回校验 SHA-256（BMC 不暴露 `CertificateString` 时降级为 subject/serial 并告警）、可选 `PATCH SecureBootEnable`。凭据只从 `KURRENT_BMC_USER` / `KURRENT_BMC_PASSWORD` 或 0600 文件读取，`--dry-run` 只读；BMC 不支持时以退出码 3 指向 USB/BIOS 兜底，不静默降级。部署指南（中英）以 USB/BIOS 为受支持路径，Redfish 作为需 BMC 与厂商支持的可选路径；该工具仅完成协议级验证（模拟 BMC），厂商实现须逐型号确认

### 修复

- **组件面证书吊销不生效（F-11）**：此前 4840 / nvmet-host 的 mTLS 只校链不读 CRL，控制面出站同样不校，被吊销的组件证书在剩余有效期内仍可调用（控制面证书也没有台账与吊销入口）。现：
  - 控制面生成 `nodes.crl` / `control.crl`（`nextUpdate` 30 天），新增 `GET /enroll/crl`（组件 mTLS + `class=component`，人类证书 `403`），enroll / renew 响应附带同一组 CRL
  - 控制面出站（Agent 调用）与 agent → nvmet-host 出站每次建连时加载 `nodes.crl` 并检查新鲜度，启用 `VERIFY_CRL_CHECK_LEAF`
  - Agent（4840）与 nvmet-host 入站在 TLS 之外做应用层校验：从 peer cert 取 issuer / CN / serial，CN 白名单之外拒绝，序列号命中 CRL 返回 `403`，CRL 缺失或过期返回 `503`（fail-closed）；组件默认每 60 秒刷新（`KURRENT_COMPONENT_CRL_REFRESH_SECONDS`），吊销无需重启
  - 新增 control CA 台账 `state/pki/control/issued.yml`、`GET /pki/control` 与 `DELETE /pki/control/{cn}`（可选 `serial`）；控制面启动自举的证书自动登记
  - 部署探针增加 CRL 分发与 control 台账断言；API 文档（中英）与部署指南（中英）同步升级顺序（控制面先行）、生效窗口与验证方式
- **`/devices/challenge` 的 nonce 可被无鉴权请求抢占（引导 DoS）**：此前每个 MAC 只保留一个未消费 nonce，局域网内任意主机反复请求 challenge（或用错误 nonce 请求 `/boot-vars`）都会清掉设备正在使用的那一个，使已签名请求失败、设备拿不到启动变量。现每个 MAC 保留最多 8 个未消费 nonce，消费只删除命中的一项，错误 nonce 不再影响其它待用项
- **Agent 建盘/删盘入参路径穿越（组件面任意文件读）**：`POST /lun/disk` 的 `master`/`filename`、`POST /lun/cd` 的 `iso`、`DELETE /lun` 的 `iqn` 与 `POST /credential` 的 `sub_nqns`/`host_nqns` 此前直接拼路径或原样透传 NQN；持有控制面客户端证书即可把 `../../etc/hostname` 克隆成盘镜像并导出、越界写文件，或让 NQN 逃出 configfs 子系统目录改配置 / 删盘。现 Agent 的文件名只接受盘目录内的裸名并对最终路径做 realpath 越界检查，IQN/NQN/hostnqn 按严格字符集与 `iqn.`/`nqn.` 形态校验；nvmet-host 独立复核 NQN、`hostnqn` 与 backing（绝对路径、无 `..`、basename 重拼后仍在盘目录），`subsystems/`、`hosts/`、`allowed_hosts/` 的路径拼接全部先校验。非法入参统一 `400`，正常建盘 / 删盘 / 凭据推送行为不变

### 变更

- **`/boot-assets` 增加 Secure Boot 状态**：返回 `secureboot` 段（签名镜像哈希与 manifest 是否一致、签名者、boot CA 指纹、引导端口及其与 `KURRENT_BOOT_HTTPS_PORT` 是否一致、构建时间）；`tftp/secureboot/` 无产物时 `present=false`，不占引导链状态板；有产物时状态板增加一行 Secure Boot，哈希不一致、端口与部署不一致或 CA 缺失/不匹配判为降级
- **nvmet-host 管理面收为共享卷 UDS**：管理面不再监听宿主 TCP `4841`，改听共享卷 `/run/kurrent/nvmet/nvmet-host.sock`（目录 0700，mTLS 保留）；Agent 的 nvmet 客户端改为 stdlib `AF_UNIX` + TLS；compose 增加 `kurrent-nvmet-run` 共享卷，健康检查改走 UDS。`host-baseline.sh --fix-4841` 不再需要（保留参数时提示并退出），宿主 4841 无监听
- **引导链 TFTP 面收窄**：`boot.ipxe.cfg` 改经 HTTPS（nginx `/tftp/`，TOFU 同锚）取回，`boot.ipxe` 只保留一次跳转指针；移除 hostname/uuid/mac/pci/chip 覆盖脚本与 `boot.ipxe.cfg` 的 `boot-url`/`boot-dir`。TFTP 只承载 `snponly.efi` 与 `boot.ipxe`，其余引导脚本全走 HTTPS。dnsmasq 的 TFTP 根改为文件级挂载，仅挂这两个文件；`boot.ipxe.cfg` / `menu.ipxe` 不再能经 TFTP 取到，更新 `snponly.efi` 后需重建/重启 dnsmasq 容器。`https-port` 需与 compose 的 `KURRENT_HTTPS_PORT`、`boot.ipxe`、`boot.ipxe.cfg` 三处同步；`GET /boot-assets` 的脚本链解析同步支持 HTTPS 形式的配置地址

## 2026-10-23

### 新增

- **WebUI 引导链状态板**：侧边栏新增「引导链」，路由 `#/boot`。页头给出整体结论（正常 / 降级 / 阻塞 / 无法判定）与检查时间，下面固定五项检查：UEFI 固件（版本串和六项能力计数，缺项降级，文件缺失 / 为空 / 路径越界阻塞）、引导服务（脚本链齐全、菜单可解析，并对脚本做浏览器 HEAD 探测确认能经 HTTPS 取回）、数据面（启用的 nvmet 节点端口就绪比例与 TLS 版本 / 端口）、Worker 就绪（绑定设备且有系统盘的比例）、最近上报（最新 `last_seen`、MAC、绑定 Worker 与相对时间，不判过期）。只有异常项就地展开原因与处理位置；不展开菜单项、不做目录浏览，工具菜单与 iSCSI 安装器不检查。新增控制面 `GET /boot-assets`（`boot_assets.py`；控制面只读挂载 `./tftp`），返回 `firmware` / `stage2` / `menu`；并移除 njs 目录列表 API（`/api/browse/`、`webui/deploy/nginx/njs/`）与通用文件浏览页面；`/file/` 下载保留

### 变更

- **WebUI 标签页图标**：与文档站一致（`favicon.ico` + `logo.svg`），入口页声明两种图标格式
- **WebUI 路由兜底**：修复失效或已删除路由（如旧书签 `#/files`）与畸形百分号编码导致的空白页——未匹配路由回退概览，启动前清理畸形 hash；路由内容增加渲染错误边界，单页异常只替换内容区并保留侧边栏，切换页面即可恢复
- **dnsmasq 引导规则收窄为 UEFI x64**：删除 `tag:bios` 匹配与 `undionly.kpxe` 下发；项目早已以 UEFI-only 为基线，该规则只会把引导文件交给不支持的固件
- **管理面边缘加固**：443 由仅 TLS 1.2 改为 `TLSv1.2 TLSv1.3`（iPXE mbedTLS 只支持 1.2，按上限协商，不受影响）；`server_tokens off` 不再回显 nginx 版本；所有响应增加 `X-Content-Type-Options: nosniff`、`X-Frame-Options: SAMEORIGIN`、`Referrer-Policy: no-referrer`
- **`/boot-vars` 限速**：按来源 IP 默认 240 次/分钟（`KURRENT_CP_RATE_LIMIT_BOOT_VARS` 覆盖、`<=0` 关闭），超限 `429` + `Retry-After` + `boot_rate_limited` 告警——该端点是无鉴权的 ECDSA 验签路径，此前未限速
- **配置键更名**：`spec.boot.autoBootTimeoutSec` → `spec.boot.autoBootTimeoutMs`（值本来按毫秒消费，字段名与单位此前不一致）；旧键按未知字段拒绝启动，配置模板（`kurrent config print init-defaults`、`kurrent.yaml.example`、`control_plane/kurrent.yaml`）与 API 文档同步
- **认证体系 P4：CRL 长跑边界、用户证书签发与逐序列号吊销**：
  - 控制面每 12 小时重签 CRL 并 reload nginx（`CRL_REFRESH_HOURS`；`KURRENT_CP_CRL_REFRESH_INTERVAL` 以秒覆盖，`<=0` 关闭），避免 7 天 `nextUpdate` 在长跑中过期——过期 CRL 会让 nginx 拒绝**全部**客户端证书；reload 失败写 `crl_refresh_failed` 告警
  - 新增 `POST /pki/users/{cn}`：为 `state/users.yml` 中已登记用户签发证书（默认 365 天、上限 3650）；请求必须携带本地生成的 CSR（CN 必须与路径一致、除 CN 外不得带 subject 属性），私钥**永不上送**、控制面只回传证书；`kurrent init` 的 admin 证书仍本地自动生成；证书文件留在 `state/pki/users/<cn>/issued/` 供审计；签发写 `user_cert_issued` 告警
  - `DELETE /pki/users/{cn}` 增加 `serial` 参数：只吊销指定序列号，并只踢绑定该证书指纹的会话（省略仍为吊销该用户全部证书并清空其会话）
  - CLI 新增 `kurrent cert keygen <cn> [--out DIR]`（本地 RSA-2048 私钥 0600 + CSR）与 `cert issue <cn> --csr FILE`（只写证书、已存在需显式覆盖），`cert revoke` 增加 `--serial`
  - 两个不鉴权的引导端点 `/devices/report` 与 `/devices/challenge` 增加按来源 IP 的滑动窗口限速（默认 120 次/分钟，`KURRENT_CP_RATE_LIMIT_REPORT` / `_CHALLENGE` 可覆盖、`<=0` 关闭）：超限 `429` + `Retry-After` 并写 `boot_rate_limited` 告警
  - 新增 `POST /devices/rotate`：设备用旧密钥签名 `rotate‖mac‖new_pubkey‖nonce` 轮换公钥（nonce 由 `/devices/challenge` 一次性签发、不透传注册窗口）；成功后 `key_hash` 更新并记 `key_rotated_at`，旧密钥在下一次 `/boot-vars` 验签即失败；重放 `401`、已吊销 `403`、未认领 `409`，限速 10 次/分钟/IP。nginx 增加 `/devices/rotate` 精确匹配的反代 location
  - 控制面容器移除 `/var/run/docker.sock` 挂载与 `docker` Python 依赖：dnsmasq 自己监听 `dhcp-hostsfile`（删除控制面向容器发 SIGHUP 的路径与 `spec.dnsmasq.reload` 声明，旧配置带该键将按未知字段拒绝启动）；nginx 容器内 watcher 监听 CRL 变化与控制面哨兵自行 `nginx -s reload`，并以 `nginx.ack` 让控制面确认吊销已生效（超时仍返回 `500`）
  - 新增 `control_plane/host-baseline.sh`（默认只读）：检查对外 TCP 监听、Docker TCP/发布端口/`docker.sock` 挂载、`DOCKER-USER` 收口与 CA 私钥/台账权限；`--fix-4841` 显式 opt-in，幂等安装三条 INPUT 规则（lo 与 `172.16.0.0/12` ACCEPT、其余 DROP）；部署指南（中英）新增「宿主基线」一节
  - 组件证书可吊销：enroll / renew 签发时把 client + serving 两张证书按序列号登记进 `state/pki/components/issued.yml`；新增 `GET /pki/components`（含吊销/过期状态）与 `DELETE /pki/components/{agent_id}?serial=`（标记吊销、写 nodes CRL、reload nginx、删除该节点的 bootstrap token）；`/enroll/renew` 对已吊销序列号返回 `403`，堵住 CRL reload 窗口内换发新证书的路径。CLI 对应 `kurrent cert components` 与 `cert revoke-component`
  - DHHC-1 定期轮换：凭据对账线程每周期检查密钥年龄（`rotated_at`，缺省回落 `updated_at` / `created_at`），超过 `KURRENT_CP_CREDENTIAL_ROTATION_DAYS`（默认 90 天，`<=0` 关闭）即生成新密钥；先推送到该 Worker 的全部目标 Agent，**全部送达后才切换台账**，任一失败则把已送达目标回推旧密钥并在下个周期重试；审计 `credential.rotate`
  - 告警外投（可选）：`KURRENT_CP_ALARM_WEBHOOK` 非空时把 `event=alarm` 审计事件以 JSON POST 出去；后台线程 + 有界队列（200）+ 重试 2 次（1s/4s，单次超时 5s），队列满丢弃并记日志——投递不进入请求路径
  - 新增 `DELETE /devices/revoked`：清除已吊销设备（`dry_run` 预览；可重复 `mac` 限定范围），只动 `revoked` 状态，用于清理探测残留
- **认证体系 P3：会话登记、证书 CRL、token scope 与审计告警**：
  - 会话改为服务端登记（`state/sessions.yml`，key = `sha256(jti)`）：Cookie `__Host-kurrent_session` 只携带 `{jti, exp, epoch}`，身份与组以登记为准；登出删登记，管理员可列出 / 逐个吊销 / 全部下线（递增 epoch）；人类会话每次请求重查 `users.yml`，删用户立即 `401`；Cookie 写请求校验 `Origin` 与 `Host` 一致，会话 12 小时 TTL、剩余不足一半才滑动续期
  - users CA 增加 CRL：`state/pki/users/issued.yml` 按序列号登记证书；`DELETE /pki/users/{cn}` 标记吊销、重签 `state/pki/nginx/clients.crl`（users + nodes 两张 CRL 拼接）并 reload nginx，被吊销证书下一次 TLS 握手即失败；同步删除该用户全部会话，不能吊销当前凭据自身
  - 自动化 token scope 化：`admin` / `readonly` / `enroll`；`readonly` 只能读，`enroll` 只能签发 bootstrap token；TTL 默认 24 小时、上限 30 天；`last_used_at` 每分钟最多回写一次；台账新增 `scopes` / `uses` / `max_uses` / `last_used_at`，列表仍不返回明文与 hash
  - bootstrap token 改为**一次性**（`max_uses` 默认 1，`--uses N` 可提高到 100），使用额度在签发前原子占用、签发失败退回；TTL 改为默认 60 分钟、上限 24 小时。声明字段 `spec.pki.bootstrapTokenTtlDays` 更名为 `spec.pki.bootstrapTokenTtlMinutes`（旧键按未知字段拒绝启动）
  - 新增结构化认证审计 `state/audit.jsonl`（`event` / `principal_class` / `principal` / `issuer` / `serial` / `fingerprint_sha256` / `route` / `decision` / `reason` / `request_id` / `client_ip`），不含 secret、token 明文、私钥与 PSK；审计写入失败时请求返回 `503`（fail-closed）。实现八条告警：引导凭据 `injected && !verified`、component/control 进入管理路由、未知 issuer / CRL 命中、同源认证失败风暴、bootstrap 身份/CSR CN 不符、管理员证书签发 / CRL 更新 / CA 生成、`users.yml` 变更、审计写入失败
  - nginx 透传 `X-Client-Cert-Verify` 与原始 issuer（仅审计取证，不参与身份判断）；`ssl_crl` 指向客户端 CRL 拼接文件
  - CLI 新增 `kurrent sessions list|revoke|revoke-all` 与 `kurrent cert list|revoke`；`token create` 增加 `--scope` / `--ttl` / `--uses`；WebUI「凭据」页新增权限 / 最近使用列与会话台账
  - 新增 `tests/control_plane/test_sessions.py`、`test_bootstrap_token.py`、`test_token_scopes.py`、`test_certs.py`、`test_audit.py`；部署探针增加 CRL、会话吊销、readonly scope 与审计字段断言
- **认证体系 P2：三 CA 分根、主体类授权与 token 绑定节点**：
  - PKI 拆为 `users` / `nodes` / `control` 三个独立根（`state/pki/ca/<kind>/`）；遗留单 CA 启动时原位提升为 `nodes`（CN 保持 `kurrent-ca`，nginx class map 兼容该 issuer），存量组件证书继续有效；admin 证书与控制面客户端证书按 issuer 检测后重签（users / control CA）
  - 组件证书由 nodes CA 签发，**subject 重建为仅 CN**：CSR 自报的 O/OU 等属性一律拒绝；管理面组只来自 `state/users.yml`（CN → groups，首次启动自举 admin；未知 CN `403`，用户被删后既有会话下次请求 `401`）
  - nginx 按证书 issuer 判定主体类并透传 `X-Client-Class`（users → `human`，nodes → `component`），信任包改为 `state/pki/nginx/ca-bundle.pem`（仅 users + nodes 公钥）；`/api/cp/` 拒绝 component（`403`），`/enroll/renew` 仅接受 component（人类证书 `403`）
  - 控制面路由声明接受的主体类（`human` / `automation` / `component` / `anonymous`），未声明即启动失败（默认拒绝）；`Identity` 增加 `class` / `issuer` / `serial` / `fingerprint`
  - bootstrap token **必须绑定 `component` + `agent_id`**（字符集 `[a-z0-9][a-z0-9.-]*`）：`POST /pki/tokens` 缺 `agent_id` 或非法字符返回 `400`，enroll 校验绑定匹配；不再签发「任意节点可用」的通用 token
  - `kurrent token create` 省略 `--agent-id` 时生成随机 12 位 hex 节点身份，输出命令携带 `--agent-id`；`kurrent join` 将其写入声明 `metadata.name`，与已声明身份不一致时拒绝
  - `POST /enroll` 响应改为下发 `ca_nodes_crt` + `ca_control_crt` 两个信任锚（不再返回 `ca_crt`）；节点侧引导要求两个锚齐备，agent → nvmet-host 与控制面 → agent 使用 nodes CA，节点校验控制面客户端使用 control CA
  - WebUI「加入节点」弹窗采集节点 ID（默认随机生成）并展示绑定关系；API 参考（中英）、架构文档、CLI README 同步
  - 新增 `tests/control_plane/test_pki_split.py`（三分根 / 遗留提升 / CSR 拒绝 / 信任包 / token 绑定）与路由矩阵用例；`tests/deploy/probe_edge.sh` 增加跨类拒绝与信任包断言

- **基础镜像与依赖收口（CVE 排查批次）**：
  - 基础镜像与 APK 包按 digest / 版本钉死（`python:3.11-slim@sha256:b7a9…`、`nginx:1.31-alpine@sha256:a355…`、`alpine:3.24@sha256:be26…`、`dnsmasq=2.92_p2-r0`）：标签漂移不再改变已提交的 CVE 基线
  - dnsmasq 改由本地镜像构建：`alpine:3.24` 官方包 `2.92_p2-r0`（上游 `2.92rel2`），修复 DHCPv6 use-after-free、DHCPv6 CLID 溢出与 EDNS 报文大小三处可达漏洞；compose 不再使用 2018 年的 `jpillora/dnsmasq` digest 镜像。构建阶段的 `network: host` 仅用于访问 Alpine 仓库
  - nginx 基座 `1.27-alpine` → `1.31-alpine`（nginx 1.31.8、njs 0.10.2、OpenSSL 3.5.9）；代理与静态配置未改，`nginx -t` 与部署探针全部通过
  - 控制面、Agent、nvmet-host 三个镜像统一为 Python 3.11.17 + OpenSSL 3.5.7；`requirements.txt` 由裸名改为直接依赖与传递依赖全钉死，带入 `urllib3 2.8.0`、`certifi 2026.10.19`、`opentelemetry-api 1.45.1`；安装后移除运行时 `pip`/`setuptools`/`wheel`
  - CLI 改由 Go 1.27.3 构建（`toolchain` 钉在 `go.mod`），govulncheck 无已调用漏洞
  - WebUI 路由补丁升级 `react-router-dom 6.30.6`；`npm audit fix` 将 `nanoid`、`source-map-js` 升到已修复版本

### 修复

- **`enroll` scope 的自动化 token 越权读管理面**：此前该 scope 只拦写操作，token 仍可 `GET` 设备 / Worker / 凭据台账 / 组件证书 / 操作审计，甚至可换一个继承 scope 的浏览器会话；现仅放行 `POST /pki/tokens`（签发 bootstrap token），其余读写接口与 `POST /session` 一律 `403`。`readonly` 行为不变
- **`/devices/challenge` 对已吊销设备仍签发 nonce（200）**：吊销设备不再进入验签链路，统一返回 `404`，与 `/boot-vars` 的拒绝语义一致
- **nvmet 部署的 `storager-agent` 不再挂载 `/var/run/docker.sock`**：nvmet 后端由 Agent 经 mTLS HTTP 驱动 `nvmet-host`，不需要 Docker；移除后容器逃逸面不再直接等于宿主 root。iscsi 编排（`storager/iscsi/docker-compose.yml`）保持挂载以 exec 后端容器
- **文件浏览页存储/反射 XSS**：目录项文件名与 URL 路径段此前原样拼进 `innerHTML`（面包屑与文件列表），构造文件名或 `/browse/%3Cimg…%3E/` 链接即可在操作者浏览器执行脚本（同源会话可发起管理操作）；该页面与 njs 目录列表 API 已整体移除（引导链状态板取代目录浏览），`/file/` 只保留静态下载
- **操作审计与凭据审计的来源恒为 `unknown`**：控制面只经 nginx UDS 访问，`request.client` 为空，`client_host()` 未回退到 nginx 覆盖写入的 `X-Real-IP`，导致 `operations.jsonl` 的 `client` 与 enroll/token 审计的 `client_ip` 全部失真；现统一取 `X-Real-IP`，并给 `/api/cp/enroll/renew` 反代补上该头
- **`/file/` 与 `/tftp/` 跟随符号链接**：归档解包带入的链接可把 nginx 容器内其它路径（同容器挂载着 TOFU 服务端私钥 `/etc/nginx/certs/server.key`）读成静态文件；两个 location 均加 `disable_symlinks on from=/data|/tftp`，文件浏览 API 的 realpath 守卫保持
- **管理面 API 响应可被缓存**：`/api/cp/` 响应（设备/会话/凭据台账）无缓存指令；现下发 `Cache-Control: no-store`，并因 location 级 `add_header` 会取消 server 级继承而重列三项安全头

## 2026-10-22

### 变更

- **管理面收口为 unix socket（认证体系 P1）**：
  - 控制面改用 `uvicorn --uds /run/kurrent/cp.sock`，容器不再监听 TCP；compose 移除 `127.0.0.1:4839:8080`，新增仅控制面与 nginx 挂载的共享卷 `kurrent-run`，健康检查改经同一 socket
  - nginx upstream 改为 `unix:/run/kurrent/cp.sock`；443 server 级默认清空 `X-Client-Cert-DN/Issuer/Serial/Fingerprint`，每个反代 location 必须显式 include 对应片段；`/api/cp/` 与 renew 仅在 mTLS SUCCESS 时注入四条证据头
  - nginx 只挂载 `ca.crt` 单文件，`ca.key` 与用户私钥不再出现在面向前端的容器文件系统
  - CLI 移除 `127.0.0.1:4839` 回退：无管理凭据且未给 `--server` 时直接报错；`kurrent init` 改为等待容器健康状态
  - WebUI 开发代理改指 nginx 443（`VITE_CP_PROXY` 可覆盖），保留 `/api/cp` 前缀，不再依赖控制面 TCP 端口
  - 新增 `tests/deploy/probe_edge.sh` 与 nginx/compose 静态断言，覆盖无 TCP 管理口、伪造头拒绝、PEP 无私钥
  - 文档：API 参考（中英）、架构文档、部署指南、CLI README 同步入口与健康检查方式

### 修复

- **应用层暴露面收口（LAN-PT-2026-10-22-01）**：
  - `/boot-vars` 的 NVMe-oF 凭据改为只在设备身份验签通过后注入：`mac` + 一次性 `nonce` + 有效签名缺一不可；仅 hostname 的请求在验签强制开启时以 `missing_mac` 拒绝，过渡期只下发菜单/导航变量，不再返回 `nbft_secret` / `nbft_tls_psk`。`device_auth_blocked()` 由返回布尔值改为 `verify_boot_identity()` 返回 `(allowed, verified)`，nonce 仍只消费一次
  - nginx 不再按 `/devices/` 前缀透传：仅精确反代 `/boot-vars`、`/devices/report`、`/devices/challenge`，其余 `/devices/*` 返回 404；三个引导端点显式清空 `X-Client-Cert-DN`，身份头只能来自 `/api/cp/` 的 mTLS 校验结果
  - njs 目录浏览在二次解码后拒绝任何 `..` 段，并对 `/data` 拼接结果做 `realpath` 校验，修复双重编码路径穿越与符号链接逃逸
  - `/devices/report` 对已有设备只允许补空 `uuid`，不再允许无鉴权上报覆盖绑定比对依据
  - 控制面交互式 API 文档默认关闭（`/openapi.json`、`/docs`、`/redoc`），仅 `KURRENT_CP_OPENAPI=1` 时开启
  - 文档：API 参考（中英）同步凭据验签条件、`missing_mac` 语义与 `uuid` 写入边界；部署指南补充 443 引导链端点白名单与 OpenAPI 默认关闭

## 2026-10-20

### 修复

- **文档与实现对齐**：修正 `/boot-vars` 的识别顺序（MAC 优先、主机名兜底）、`hostnqn` 的派生方式与固件补丁号、`menu_timeout` 的配置来源，并将启动变量示例改为 nvmet 后端、补齐 TLS 字段；部署指南同步凭据快照的发现链与已验证宿主环境，WebUI 的 boot-vars 预览补齐 `disk-nqn` 与 TLS 字段；架构文档的引导准入与设备侧描述同步当前实现。

## 2026-10-18

### 变更

- **引导数据面与控制面脱钩：`/boot-vars` 下发完整盘 NQN，移除主机名下发端点**——此前 menu 用 `${hostname}.${os}.${os-tag}` 重拼盘 NQN，使数据面路径依赖 DHCP/DNS 主机名时序；主机名只影响菜单标签后，这层耦合不再有存在理由：
  - `control_plane/app/routers/boot.py`：`disk_nqn` 取代 `base_nqn`（盘记录 `nqn` 原样下发，不再由前缀派生），`os_tag` 不再下发，`os` / `os_version` 降为菜单展示用；删除 `GET /devices/hostname`；Worker 查找改为 MAC → 设备台账 `bound_worker_id` 优先，hostname 仅作无 MAC / 未命中时的兜底
  - `control_plane/app/trust.py`：验签数据由 `nonce‖mac‖hostname` 收窄为 `nonce‖mac`——hostname 由客户端自报且服务端不与台账比对，放进签名串不增加身份约束；身份仍由 MAC → 设备绑定与一次性 nonce 保证
  - `tftp/boot.ipxe.cfg`：删除主机名获取阶段（阶段 2.5），`sign` 改用 `${nonce}${mac}`；`tftp/menu.ipxe`：`root-path` 直接拼 `${disk-nqn}`，缺变量即 `goto failed`
  - 测试：`tests/control_plane/test_trust_chain.py` 同步契约（验签去 hostname、`disk-nqn` 投影、跨 MAC 签名拒绝、MAC 优先于滞后 dnsmasq 投影），22 passed
  - 文档：`api/control-plane-api.{zh-CN,en}.md`、`about/{en,zh}/ARCHITECTURE.md`、`webui/deploy/nginx/nginx.conf` 注释
- **文档站内容对齐实现（`docs/`）**——解除「冻结待重写」状态，按当前实现更新中英双语站点：
  - 部署指南：数据面改为单口 `4421`（NVMe/TCP TLS 1.3，固件段与内核段共用），补 `4841` 宿主本地管理口；宿主前置改为 `host-prepare.sh` + TLS 内核选项 + `tlshd`；防火墙与排障同步；删除全部 `kubeadm` 血统标注
  - 新增「使用流程」页（中英）：注册窗口入池 → 绑定 Worker → 创建系统盘（放置/批量/dry_run）→ 设置默认盘 → 开机链 → 凭据轮换/吊销 → 日常操作
  - 首页特性与侧边栏导航同步；`host-prepare.sh` 注释端口修正为 `4421`
- **文档站视觉重制：纸墨版式与单色标识**——首页由默认 VitePress 版式改为自绘的刊物式首页，去掉霓虹/终端视觉，主视觉落在「无状态 / 系统即单元」的命题而非引导链：
  - `docs/.vitepress/theme/components/KurrentHome.vue`：中英双语首页组件（`layout: false`），纸墨色板 `#f4f1ea` / `#151412`、宋体标题、等宽元信息、细分割线；六虚记号按 logo 比例用 CSS 渐变实现断爻对齐；「已验证发行版」一节明确验证范围不构成发行版限制
  - `docs/.vitepress/theme/index.ts` 注册组件；`docs/index.md`、`docs/en/index.md` 改挂载点
  - `docs/public/logo.svg` 改为单色墨黑，新增 `logo-dark.svg`（骨白）；`config.mts` logo 明暗双份
  - `docs/.vitepress/theme/custom.css`：全站纸色 / 石墨暗色两套色板，正文标题改为宋体，代码块与引用块同步
  - `docs/{,en/}guide/deployment.md`：拓扑图「无盘引导 / Diskless clients」改为「网络引导 / Bare-metal clients」

## 2026-10-06

### 变更

- **NVMe-oF 数据面默认全链路加密：撤明文口（段 2.3 端口收口）**——1.3c 的双口形态（`4420` 明文 + `4421` TLS）以「固件引导段不做 TLS 握手」为前提；固件侧 TLS 1.3 PSK 客户端（固件仓段 2.1/2.2）就绪后，固件段可自行握手，明文口失去存在理由。按「默认全链路加密、不搞特殊」裁定，数据面收敛为单口、无明文回退：
  - `storager/nvmeof/nvmet-host/app/main.py`：数据面单口（`ports/2`、`4421`、`tsas=tls1.3`）；`/capabilities` 端口键由 `tls_port` 合并为 `port`（含 `port_id`/`trsvcid`/`tsas`/`ready`，数据面关闭时为 `null`）；`POST /port` 无参（幂等确保数据面口）；启动增 `remove_legacy_plain_port()`——拆除历史明文口残留（先摘 `subsystems/*` 挂载再 `rmdir`），使升级宿主收敛单口；`ensure_tls_port()` 失败即 `tls_ready=false`（不再「降级明文」）
  - `control_plane/app/routers/boot.py`：`/boot-vars` 改为「加密是默认、无明文回退」——nvmet 后端下「有密钥 + 数据面端口就绪」缺一即**整体不下发引导变量**并记审计（`boot_vars.encryption` denied，reason `no_credential`/`port_not_ready`）；端口值取自 `/capabilities.port.trsvcid`（要求 `ready`）
  - `tftp/menu.ipxe`：`:boot-os` 端口取 `${nbft-tls-port}`（不再硬编码 `4420`、不再回落明文）；`nbft-tls-port`/`nbft-tls-psk` 缺失即 `goto failed`
  - 测试：`tests/nvmet_host/test_nvmet_host_service.py`（单口能力 / 建口 / 迁移拆口）、`tests/control_plane/test_credentials.py`（端口未就绪拒绝、无凭据拒绝、非 nvmet 后端不下发）；全量 pytest **371 passed**
  - 文档：`README{,.zh-CN}.md`、`about/{en,zh}/ARCHITECTURE.md`、`storager/nvmeof/nvmet-host/README.md`（单口、「无明文回退」措辞）、`cli/README.md`、`api/control-plane-api.{zh-CN,en}.md`（`nbft_tls_port` 能力来源 `/capabilities.port.trsvcid`、吊销语义「拒绝引导」）、`storager/nvmeof/docker-compose.yml` 注释

## 2026-10-05

### 新增

- **数据面传输加密：内核段 NVMe/TCP TLS 1.3（段 1.3 主仓侧）**——DH-HMAC-CHAP 认证只回答「谁能连」，数据面内容此前全程明文；而 TSAS 是**端口级**属性、只接受 `tls1.3`，因此同一子系统必须挂两个端口才能「固件引导段明文 + 内核段加密」：
  - `storager/nvmeof/nvmet-host/app/main.py`：新增 TLS 端口（`ports/2`、`4421`、`tsas=tls1.3`，内核自动置 `treq=required`；`NVMET_TLS_PORT_ID` 为空 = 关闭 TLS）与 `NvmePskKeyring`——ctypes 直调 `add_key`/`keyctl` 把 `psk` key 注入内核全局 `.nvme` keyring（identity `NVMe0R01 <hostnqn> <subsysnqn>`）；端点 `PUT /subsystems/{nqn}/tls`、`DELETE /subsystems/{nqn}/tls/{hostnqn}`；`/capabilities.tls_port` 如实报 `ready`（建口失败只降级为仅明文并留痕，不静默）；`create/delete_subsystem` 同时挂/摘两口。两处实测坑已固化：`psk` key 的 possessor 权限不含 Write ⇒ 必须先摘旧 key 再写（一条 identity 只留一条）；淘汰须**限定在 `.nvme` 内**（`KEYCTL_READ` 读挂载列表定位），照 `/proc/keys` 全局同名条目找会 UNLINK 得 `ENOENT` 且旧 key 越积越多
  - `storager/nvmeof/docker-compose.yml`：`security_opt` 增 `seccomp=unconfined` 与 `systempaths=unconfined`——写内核 `.nvme` 的两处默认策略（seccomp 拒 `add_key`/`keyctl` → EPERM；`/proc/keys` 被 MaskedPaths 屏蔽成 `/dev/null`）缺任一即静默失败
  - `control_plane/app/routers/workers.py` + `app/agent_client.py`：新增 `_derive_tls_psk`（`HMAC-SHA256(raw_key, b"kurrent-nvme-tls-psk-v1")` → 64 hex）——**PSK 是认证密钥的纯函数，不新增存储字段**，故轮换/吊销认证密钥即原子地轮换/吊销 PSK（推送台账指纹也不需扩展）；`_push_one` 现算并随推送携带（吊销带 `None`），`set_credential` 增 `tls_psk` 形参
  - `storager/agent/app/{main.py,nvmet.py}`：`PUT /credential` 透传 `tls_psk`（审计只记 `tls: bool`，不记本体）；缓存期望状态含 `tls_psk`；`_sync` 有 PSK → 宿主 `/tls` 注入（**409 = 目标端未启用 TLS，降级明文记警告、非故障**；404 幂等）、吊销 → 摘除、**密钥在但未带 PSK（旧控制面）→ 保持不动**（避免误清已有 PSK）
  - `control_plane/app/routers/boot.py` + `tftp/menu.ipxe`：`/boot-vars` 在「已绑定 + 有密钥 + 后端 nvmet + Agent 上报的 TLS 端口就绪」四条同时满足时投影 `nbft_tls_psk` / `nbft_tls_port`（缺任一不下发——固件走明文，与未启用 TLS 的 target 兼容）；menu 在 sanboot URI 追加 `&tls_psk=${nbft-tls-psk}&tls_trsvcid=${nbft-tls-port}`，随 NBCT 表交给 initramfs，由其在 `4421` 以 `--tls` 重连（**固件引导段本身仍走 4420 明文**）
  - 测试：`tests/nvmet_host/test_nvmet_host_service.py`（TLS 端口创建/挂载、Keyring 注入与摘除、`.nvme` 限定查找回归）、`tests/agent/*`（`tls_psk` 透传与吊销）、`tests/control_plane/test_credentials.py`（派生 + 推送携带 + `/boot-vars` 三例：TLS 就绪下发 / 端口未就绪不下发 / 非 nvmet 后端不下发）；全量 pytest **369 passed**
  - 已实测（2026-10-05）：1.3a 目标端供给（R1 容器写 `.nvme` 需两处放行、R5 同子系统挂两口）、1.3b 控制面派生→agent→目标端 PSK（含 1.3b′ 内核发起端 `nvme connect --tls` 全自动供给、1.3b″ 对账周期重放不产生重复 key、1.3b‴ 负对照：无 key 必挂）
  - **待验收**：1.3c 冷启动端到端（用户 root 执行）——判据为到 `login:`、`:4421` pcap 零明文 NVMe/TCP PDU、`TlsTxSw`/`TlsRxSw` 非零、`:4420` 段无回归；initramfs 侧 R2/R3/R4（tlshd / keyctl / `nvme-keyring` 等模块在 initramfs 中可用）在固件仓
  - 文档：`api/control-plane-api.{zh-CN,en}.md`（5 节 `nbft_tls_psk`/`nbft_tls_port` 两行、7.7 推送体与静态口径「三处」改「四处」、能力清单）、`about/{en,zh}/ARCHITECTURE.md`（数据面双端口、交接给 OS 带 TLS 参数、「认证 ≠ 加密」改写为两层独立）、`storager/nvmeof/nvmet-host/README.md`（新增「传输加密」节 + 环境变量/端点表/安全边界/宿主前置含 tlshd）、`cli/README.md`、`blueprint/nvmeof-credential-design.md`（§6 端口行、§8 核对项、§9 静态加密条目）
  - 设计依据：`blueprint/nvme-tcp-tls-in-kernel.md`（机制、实测证据、验收判据）与 `blueprint/nvme-tcp-tls-kurrent-changes.md`（主仓改动清单）

- **CLI 管理凭据用户级快照（`kurrent credentials install|show|remove`，脱离仓库可用）**——CLI 凭据发现此前只服务仓库内：二进制安装到 PATH 或在仓库外调用时静默回退无凭据通道（401 难以归因），而把 admin.conf 写成绝对路径又会在仓库迁移/凭据轮换后静默不同步。凭据按「获取一份快照」处理——复制而非跟随，过期靠重新获取（重跑 install）：
  - `cli/credentials.go`：发现链尾追用户级快照——`--credentials` ＞ `KURRENT_CREDENTIALS` ＞ 仓库内（cwd 逐级上溯 → 可执行文件旁）＞ `~/.kurrent/admin.conf`（仓库源在场时优先实时源，脱离仓库才吃快照）；`credentialsFile` 增 `installed-from`/`installed-at` 溯源键（仅快照写入）；`authHint` 修法首推 `kurrent credentials install`
  - `cli/credentials_cmd.go`：`install` 把当前发现的凭据**复制**为自包含快照 `~/.kurrent/`——证书副本放 `pki/<文件名>`、admin.conf 重写为相对键（整目录可拷走）+ 溯源键，目录 0700 / 文件 0600、tmp+rename 防半写；源即快照时报错（须以仓库源刷新）。`show` 汇报生效凭据层级、四键可达性与快照状态；`remove` 只删快照（仓库源与已签发 token 不受影响）
  - `cli/main.go` 命令组与 usage、`cli/init.go` 完成输出加一行安装提示
  - 测试：`cli/credentials_test.go` 既有发现用例注入临时 HOME（隔离真实 `~/.kurrent`），新增 6 例——install 内容/权限/自包含整包拷贝、刷新覆盖、拒绝以快照为源、仓库源优先、remove 与重复 remove、show 输出；全量 `go test` 通过
  - 文档：`cli/README.md`（构建/配置/命令块）、`api/control-plane-api.{zh-CN,en}.md` 1.3 鉴权表补发现链说明
  - 真实端到端：仓库内 install → 二进制拷至 /tmp（仓库外）→ `ops list` 经 443 mTLS 取回审计数据 → remove 后回退 401+安装提示 → 重装恢复
- **密钥治理：控制面→Agent 凭据推送对账（B3「推不丢、看得见、边界清」）**——DHHC-1 凭据链路此前「能推、但推丢看不见」：`_push_credentials` 失败仅记审计不重试——轮换失败 → boot-vars 注入新钥而 configfs 仍旧钥（引导认证失败）、吊销失败 → 旧钥滞留仍可连（安全缺口）；且无状态投影，`GET /credential` 只读台账、CLI/WebUI 无入口：
  - 对账闭环（`control_plane/app/routers/workers.py`）：新增推送状态台账 `state/credential-push.yml`（键 `worker/agent`，含所推密钥审计哈希，不含明文）+ 控制面对账线程（`reconcile_credentials_once`，FastAPI lifespan 启动，默认 60s，`KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL` 秒可调、0=禁用）——「期望 × 记录」比对，密钥哈希一致且已送达才跳过，否则幂等重放（凭据 opt-in：无凭据且无推送历史的对不推送、不留账）：覆盖推送失败、进程崩溃缝隙（台账已改未推送）与吊销未送达三类静默缺口；Agent 注销剪除记录、重新登记自动重推（节点重置后凭据自动补投；投影同口径——未登记 Agent 不计为同步目标，不显示永不收敛的「待推送」）；对账重放仅在状态/密钥变化时记审计（去噪），用户动作照常每次记录
  - 状态可见：GET/PUT/DELETE `/workers/{id}/credential` 响应增 `push` 投影（result = ok/failed/pending/n-a/none + 逐 Agent 明细与错误）；CLI 增 `workers credential show|set|revoke`（`--generate` 就地生成 32B 随机密钥，不打印明文；未送达提示对账重试）；WebUI Worker 详情独立「NVMe-oF 凭据」页（条件展示：该 Worker 有 NVMe-oF 盘才出现，对齐「不支持即隐藏」；状态徽章 + secret_hash 前缀 + 推送明细 + 生成设置/吊销二次确认，浏览器就地生成 DHHC-1 且不落浏览器存储）
  - 语义与边界：非 nvmet 后端目标记 `n/a`（凭据不适用，非故障，不计入失败、不参与重放判定）；凭据 **opt-in**——从未设置且无推送历史的 Worker 不推送、不记台账、不记审计（Agent 离线时不出现「同步失败 / 待同步」虚假故障），有过推送记录后吊销即便未送达也持续重放；静态存储裁定——控制面/Agent 缓存（均 0600）与 configfs 三处明文**不引入应用层加密**，边界责任归部署方（磁盘/宿主持久化加密、root 访问边界），已在文档写明
  - 测试：`tests/control_plane/test_credential_sync.py` 8 例（状态回写与投影、失败可见→对账恢复且审计去噪计数、吊销补投、崩溃缝隙补推、失效记录剪除与重新登记重推、非 nvmet 判 n/a、凭据 opt-in 不推送不留账、有历史后吊销仍重放；opt-in 与「未登记 Agent 投影 none」两组断言经变异测试证实非空转）；前端 `generateDHHC1`/`crc32` 3 例（标准校验值 + CRC 自检 + 随机性）；全量 pytest 319 passed
  - 文档：`api/control-plane-api.{zh-CN,en}.md` 7.7（对账语义/静态口径/带 push 的响应示例；顺带修正 Host NQN「按绑定设备 UUID 派生」的陈旧描述——实现早已是 worker 派生 + boot-vars 注入）、`cli/README.md` 新增「NVMe-oF 认证凭据」节、`webui/DESIGN.md`
  - 真实端到端：控制面重建后以真实 HTTP stub agent 走通 set→推送 ok→停 stub→轮换失败可见（failed+原因）→恢复→对账自动收敛；CLI show/set/revoke 实测（详见验证记录）
- **系统盘放置：位置由控制面决定（`POST /workers/luns/disk/batch` 支持 `workers` + `placement`，含 `dry_run` 计划）**——此前批量建盘把放置推给调用方（`targets[].agent` 必填），单盘不点名时只有 `select_disk_agent()` 取「注册顺序第一个健康节点」：既不看负载，也**不检查母盘是否在该节点本地存在**（克隆在节点本地完成）→ 会选中一台没有这块母盘的机器，直到 agent 侧才失败；策略散在客户端还会让「预览」与「执行」成为两套实现而相互漂移：
  - `control_plane/app/placement.py`（新）：候选 = 磁盘角色 ∧ 启用（非排空）∧ 有 `base_url` ∧ 数据面就绪（`healthz.backend_ready` 非 `false`）∧ 在 `candidates` 内 ∧（`type=master` 时）**母盘在该节点本地存在**；分配 = 现有盘数最少优先（同负载按注册顺序）；`pins` 逐 Worker 钉死（被钉节点不要求在 `candidates` 内，但须自身可用）；无候选抛 `PlacementError`，`excluded` 逐节点给出原因
  - `control_plane/app/routers/workers.py`：批量端点 `targets` 与 `workers`+`placement` 二选一（同时给 → 400）；抽出 `_resolve_batch_targets()` 供 `dry_run` 与执行共用——**预览即执行**；`dry_run=true` 返回 `plan`（逐行 `create`/`skip`/`error`）与 `warnings` 且不建盘；放置阶段已校验的 agent 在执行阶段复用（免去按 Worker 数重复 healthz/capabilities）；放置调用移到 `store.locked()` 之外（不再持锁做网络 I/O）
  - `control_plane/app/models.py`：新增 `DiskPlacementRequest`（`candidates` / `pins`）；`BatchCreateWorkerDiskRequest` 增 `workers` / `placement` / `dry_run`，`targets` 改为可选（原有调用方不受影响）
  - `control_plane/app/scheduler.py`：删除变为死代码的 `select_disk_agent()`（`_select()` 仍服务 CD 角色）
  - 测试：`tests/control_plane/test_worker_disk_placement.py` 20 例（均摊按现有盘数、`candidates` 限定且未列入者不探测、母盘缺失排除、空白盘跳过母盘校验、无候选 503 带逐节点原因、非磁盘角色/排空/数据面降级排除、pin 覆盖与 pin 越出 `candidates`、pin 指向不可用节点 503、`dry_run` 计划与执行一致且不落盘、`dry_run` 的 skip/error 行、`targets` 向后兼容且绕过放置、二选一 400、放置探测缓存只探一次、单盘自动放置选中有母盘的节点）；均摊比较、母盘校验、执行改用 `req.targets`、`dry_run` 短路、探测缓存五处各自经变异测试证伪；全量 pytest 341 passed
  - 文档：`api/control-plane-api.{zh-CN,en}.md` 7.1.2 / 7.1.3（放置语义、字段表、`dry_run` 请求与计划示例）与端点一览

### 变更

- **NVMe-oF 凭据改为「建盘即签发」：nvmet 后端建盘自动生成密钥并推送（取代逐台手工设置）**——此前凭据是 opt-in 且只能人工逐台设置（WebUI 详情页「生成并设置密钥」/ CLI `workers credential set --generate`），批量建盘 N 台就要 N 次人工操作，而盘与凭据本是同一个交付单元（用户 2026-10-05 指出「那得多慢呀，你忘了有批量创建的盘的情况了吗」）：
  - `control_plane/app/routers/workers.py`：新增 `_generate_dhhc1()`（32 字节随机 + CRC32 小端终值，与 CLI `--generate` 同格式，产出必过 `_validate_dhhc1`——生成与自检互为闭环）与 `_issue_credential(worker_id, backend)`——**仅 nvmet 后端签发**（DHHC-1 是 NVMe-oF 专属，stgt/lio 走 CHAP，不签发），**已有条目保留**（按 Worker 跟盘，同一 worker 多盘共用一把，建盘不触发轮换），新签发审计 `credential.issue`（不记密钥本体）；单盘（7.1）与批量（7.1.3）两条建盘路径都在建盘成功后、推送前调用
  - 可观察差异：nvmet 节点上建盘后 `GET /workers/{id}/credential` 直接 `exists: true` 且 `push: ok`，无需任何人工步骤；批量 N 台一次得到 N 把互不相同的密钥。手工端点（PUT/DELETE `/credential`）语义收窄为**显式轮换 / 吊销**——吊销即声明「不用认证」（条目被删），不会被后续建盘复活
  - 「无密钥不推送」判定保留（`_no_sync_needed`）：nvmet 建盘已自动签发，该分支实际只剩非 nvmet 后端的盘（推送只会得 `n/a`），避免 Agent 离线时出现虚假「同步失败 / 待同步」
  - 测试：`tests/control_plane/test_credentials.py` 新增 5 例（生成器契约闭环与随机性、nvmet 建盘签发并推送且明文不入审计/不经 API 回传、已有密钥建盘不轮换、非 nvmet 建盘不签发、批量建盘逐 Worker 各签发一把）；三处变异（摘掉签发调用、拆掉后端闸门、强制轮换）各自使对应用例失败，证实断言非空转；`tests/control_plane/test_credential_sync.py` 的 opt-in 用例改述为「非 nvmet 不签发」；全量 pytest 346 passed
  - 文档：`api/control-plane-api.{zh-CN,en}.md` 7.1 步骤 7 / 7.1.3 / 7.7（签发时机与手工端点收窄）/ 端点一览 / 能力清单；`blueprint/nvmeof-credential-design.md` §4.1 写入时机与 §9 待定决策第 7 条（本裁定）；`webui/DESIGN.md` Workers 段与 NVMe-oF 凭据页签；WebUI i18n `workerDetail.credentialHint` 中英同步
- **WebUI 操作结果改为通知条（右下角 Toast），不再写进页面流**——此前建盘/删盘/建 Worker 的结果是列表上方一行
  「成功 2 ×」，混在内容里像一截正文，又不带任何消失时机（用户 2026-10-05 指出「成功的提醒为什么不做成通知样式」）：
  - 新增 `webui/app/src/components/Toast.jsx/css`（+ `ToastStack`）：固定右下角、可堆叠，不进页面流；
    **成功/部分成功 6s 自动消失，失败常驻须手动关闭**（错误不能被时间吞掉）；变体只用单色语言
    （`ok` 细灰边；`error` 深色描边 + 左侧重墨条）；带明细逐条列失败/跳过原因（成功项不复述）；
    `role=status` / `role=alert`；自动消失计时器用 ref 持有 `onClose`，只依赖 tone/duration，**不随重渲染重置**
  - `webui/app/src/pages/Workers.jsx`：建盘结果、批量删除结果、批量创建 Worker 结果、以及接口失败（建盘/删除）
    全部改走通知条；创建弹窗**提交后关闭**（原先把结果留在弹窗里）；失败时**保留表单值**便于改掉个别 MAC 重提
    （已建成的按幂等跳过），全部成功才清空；单条创建也发通知（1 台与 N 台的反馈一致，此前单条静默无反馈）
  - 附带的既存 i18n 坏键修复（会把键名当文案显示）：`AgentLuns.jsx` 的 `agentLuns.unknown`（`agentLuns` 块无此键，
    该位置本就用字面 `—`）、`Devices.jsx` 的 `devices.wizard.emptyManifest`（补 `devices.wizard.manifestEmpty`
    中英两条，原先借用的是「导入清单」的文案）
  - 验证：`npm test` 11 passed + `vite build`；浏览器实测——成功（建 1 台 Worker / 批量删除，`toast-ok` 文案「成功 1」，
    约 6s 后自动消失）、失败（批量创建带 2 个不在池中的 MAC → `toast-error` 常驻「成功 0 · 失败 2」+ 逐条原因，
    弹窗已关闭且表单值保留，再次提交仍带原值）
  - 文档：`webui/DESIGN.md` 新增 `Toast（结果通知）` 组件节 + Workers 段结果反馈改写
- **Workers 页交互去模式化：位置是策略，不是逐行输入（取代同日早前「批量模式」交互契约）**——原设计有三个互斥模式（批量创建 Worker / 批量创建系统盘 / 批量删除）+ 页头四个平级按钮：同一件事（勾选）在不同模式下行为不同，把「系统该做的决定」变成了用户要先学会的模式；批量建盘还要求用户逐行指定存储节点，与「调度系统而非调度应用」相悖：
  - `webui/app/src/pages/Workers.jsx`（全量重写）：**没有模式**——勾选列常驻，选中即浮出操作条（黑条）。**行点击语义由选中态决定**：无选中 = 进详情，有选中 = 勾选/取消（Shift 标定区间终点，表头全选作用于筛选后可见行）。创建 Worker 收进弹窗（数量 = 1 时名称即完整 Worker ID，> 1 时为前缀；MAC 列表按行逐个绑定；Windows ISO 收进「高级选项」折叠块，且批量不支持该字段时整块隐藏）
  - 「创建系统盘」= **在操作条上就地展开放置计划**：参数（类型 / 系统 / 母盘或大小）、逐行目标节点、结果摘要、`确认创建`/`取消`；参数档位不齐时摘要位直说缺什么（如「未发现可用母盘」）而非停在「计算中…」。高级面板 = 对系统决策的约束（限定可用节点，按当前母盘本地存在性标注「缺该母盘」）+ 低频参数（系统版本 / 盘备注）+ 已排除节点及原因
  - 置位交给控制面：只发 `workers` + `placement{candidates,pins}` + `dry_run`，同一份 body 先取计划再执行（**预览即执行**）；逐行下拉是 pin 显式覆盖（标「手动」，`×` 恢复自动，自动态的节点由系统按现有盘数最少选定）；`candidates` = 「限定可用节点」；放置 503 的逐节点排除原因在高级面板展示
  - `webui/app/src/api/client.js`：请求失败时把原始响应体挂到 `error.payload`，供调用侧取 `detail` 之外的字段（放置失败的 `excluded`）
  - 移除：存储节点侧栏（拖动指定 / 接管所选 / 均摊分配）、三模式状态机与页头平级按钮、对应的旧 i18n 键与 CSS（批量提示、容量标签、结果块）；`webui/app/src/pages/Workers.css` 按新结构重写
  - i18n：`workers.*` 重组为 `sel` / `plan` / `bulkDelete` 三组；`WorkerDetail` 的建盘表单改用 `workers.plan.*`（同一标签单一来源），仅保留 `workers.diskAgent` / `workers.masterGpt512`（`masterGpt512` 补齐——原 `workers.empty`/`master`/`diskType`/`masterName`/`noMasters`/`diskSize` 等键在本次重组中被移除，不同步会让详情页显示键名）
  - 验证：`npm test` 11 passed + `vite build`；浏览器端到端（真实控制面 + 两个 HTTP stub agent，走 mTLS 代理到 nginx）——建 2 台 Worker（弹窗，批量时高级块正确隐藏）→ 勾选 → 计划：母盘模式 2 块全落在本地持有该母盘的唯一节点、空白盘模式均摊 ×1/×1 → 手动改一行（出现「手动」+ `×`，摘要变 ×2）→ `×` 恢复自动 → 取消勾选一个节点（计划收拢，该节点选项禁用）→ 确认创建（stub 日志佐证：dry_run 期间零 POST，执行只向计划节点发 2 次 `POST /lun/disk`，载荷含计划显示的母盘与 `os_tag` 文件名）→ 列表显示 `ubuntu → e2e-stub-01` → 重新计划两行皆「已有系统盘 · 跳过」且确认禁用 → 删除（勾选同时删除 .img，成功 2，`DELETE /lun?delete_file=true` 落在正确节点）→ Esc 退出计划、无选中时点行进详情；另实测降级路径（唯一节点离线）：摘要直说「母盘清单不可用」、放置失败给出「无法计算放置」并在高级列出 `storage-nvmet-01（Connection refused）`。环境已复原（`agents.yml` 字节一致、stub 停止、临时文件清理）
  - 文档：`webui/DESIGN.md` Workers 段按新契约重写（列表列、行交互、弹窗、放置语义、预览即执行、Esc 分层、独立取数）
- **单盘建盘不点名节点时的选点语义**：由「注册顺序第一个健康节点」改为放置策略（数据面就绪 ∧ 母盘本地存在 ∧ 现有盘数最少优先）。可观察差异：`type=master` 且无任何节点本地持有该母盘时，现在在控制面即返回 `503`（附逐节点排除原因），不再选中一台没有该母盘的节点、到 agent 侧才失败。

### 修复

- **删除 Worker 未清理凭据条目（孤儿密钥）**——`DELETE /workers/{id}` 与 `POST /workers/delete/batch` 此前只向 Agent 推一次吊销、**不删 `state/credentials.yml` 里的条目**：删完 Worker 后留一条孤儿 DHHC-1 密钥，而 `GET /workers/{id}/credential` 对已不存在的 Worker 直接 404 → 它从 API 与 WebUI 都看不见、也删不掉（只能读文件）；更实际的风险是**同 id 的 Worker 日后被重建时会静默继承这把旧钥**并被推给新机器的盘：
  - `control_plane/app/routers/workers.py`：新增 `_forget_worker_credentials()`（清密钥条目 + 该 Worker 的推送台账，删到条目时审计 `credential.forget`）；单删在 `workers.delete` 之后调用、批删在逐项成功分支内调用（两条独立代码路径都覆盖）
  - 顺带清推送台账：台账键 = `<worker_id>/<agent_id>`，留着会让同 id 重建的 Worker 复用陈旧"上次推送"记录（对账 60s 内虽会剪除，但重建快于一个周期时就会读到）
  - 测试：`tests/control_plane/test_credential_sync.py` 新增 2 例（单删后密钥条目与台账皆空、同 id 重建 `exists=false` 且投影 none、审计含 `credential.forget`；批删同样清空）；两例分别经变异测试证实各自覆盖对应调用点
  - 文档：`api/control-plane-api.{zh-CN,en}.md` 第 11 节删除步骤补齐（新增"删除前吊销推送"与"清空凭据痕迹"两步，编号重排）
- **WebUI 批量建盘：节点列表被 `/masters` 502 拖垮 + 错误被吞**——批量建盘把节点列表与母盘清单耦合成一个 `Promise.all`，而 `/masters` 是跨节点聚合端点（任一节点不可达即整批 502）：一台存储节点掉线就让**整个批量建盘入口可用性归零**（连不需要母盘的「建空白盘」也拿不到节点、无法分配），且失败被吞（`batchError` 在批量建盘侧栏根本没渲染），空态还显示成「未发现可用存储节点」——把「取数失败」说成「没有节点」：
  - `webui/app/src/pages/Workers.jsx`：取数改为**独立两段**（与 `WorkerDetail`/`AgentLuns` 同款 `getMasters()` 兜底）——节点列表不再受母盘清单影响；新增 `nodesError`/`mastersError` 两个独立失败面：节点取用失败显式报错，母盘清单失败只在「母盘」类型下提示并改掉误导性占位文案
  - 验证：`npm test` 11 passed + `vite build`；浏览器实测（真实控制面，`/masters` 正处于 502 的环境）——节点恢复显示并可分配（此前为空）、母盘类型下错误可见（该轮的批量模式交互已在同日「Workers 页交互去模式化」中整体替换）
  - 文档：`webui/DESIGN.md` Workers 段（后由「去模式化」重写）
- **文档同步：清掉几处与实现矛盾的陈旧表述（无代码变更）**——逐份对照代码与真实内核核了一遍跟踪内的文档：
  - `storager/nvmeof/nvmet-host/README.md`：子系统 NQN 由「盘 IQN 派生、格式 `<base>:worker-XX.os`」改为 **NQN 权威** `<base>:<worker_id>.<os>.<os_tag>`（IQN 由 NQN 派生）；`allowed_hosts/` 的说明**说反了**（原写「内核在 allow_any_host=1 时自动维护、严格模式用户侧不操作」，实际严格模式准入就是 symlink 进 `allowed_hosts/`）——按 `NvmetManager.set_host` 改写；configfs 属性名纠正为 `dhchap_key`（原写 `nvme-auth-dhchap-secret` + `nvme-auth-dhchap-control` 置位，是 2026-08-23 那条**已作废**方向的残留：写 key 即启用认证，内核无独立 control 属性）；推送触发点补「nvmet 建盘即签发」
  - `cli/README.md`：删除「命令语义沿用 kubeadm / kubectl」的血统标注（对齐对外表述纪律）；「NVMe-oF 认证凭据」节改述为建盘即签发 + 手工端点=轮换/吊销
  - `about/{en,zh}/ARCHITECTURE.md`：`/boot-vars` 步骤补「nvmet 节点上建盘时即自动签发密钥」
  - `about/{en,zh}/Manifesto.md`、`about/{en,zh}/AI_POLICY.md`：iBFT 时代字眼改为 NBFT/NBCT（`iPXE 写入 iBFT 表` → 固件启动瞬间交出引导表；`iPXE/iBFT 引导链` → `iPXE/NVMe-oF`），论证与文风未动
  - `README.md` / `README.zh-CN.md`：把「凭据 enroll 时自动派生」拆开——DHHC-1 按 Worker 跟盘、**建盘即签发**；存储节点宿主服务凭据 enroll 时派生；Roadmap 段那句「贯穿所有计算层的元协议」是愿景（在 Manifesto 里），原来却只指向不含它的 ROADMAP.md——改为愿景指向宣言、方向指向 ROADMAP；「可挂 Windows/Ubuntu/Debian/CentOS/ESXi 多盘」的发行版举例与固件仓验收矩阵不符（矩阵实为 Debian 12/13、Ubuntu 24.04/26.04、Fedora 44）——改为按矩阵列举「无盘冷启动已通过整机验收」的发行版（Windows/ESXi 保留在 ROADMAP 的待办方向里）
  - `webui/DESIGN.md`：路由行 Worker 详情 Tab 列表补「NVMe-oF 凭据（有盘时）」
  - 未动：`docs/`（冻结待重写）、`about/{en,zh}/Barriers.md`（iSCSI 期战史，用户裁定保留）

---

## 2026-10-01

### 新增

- **Token 台账可视与吊销（管理凭据从「能发不能收」到可收）**——`/pki/tokens` 此前只有签发（POST），token 作废只能进容器手改 `state/pki/bootstrap-tokens.yml`；本次补列表与吊销：
  - `control_plane/app/pki.py`：新增 `list_bootstrap_tokens()`（仅未过期条目；不返回 `token_secret_hash`——明文本就不可恢复；`created_at` 对历史条目为空串）与 `revoke_bootstrap_token()`（删条目即吊销，无 tombstone）；`issue_bootstrap_token` 签发时注入 `created_at`；新增模块级 `_TOKENS_LOCK`（RLock）包住 issue / revoke / `revoke_agent_tokens` 的 load→改→save 段（同步路由跑线程池，签发与吊销交错会把已吊销条目写回；读路径靠 `os.replace` 原子替换不加锁）
  - `control_plane/app/routers/tokens.py`：新增 `GET /pki/tokens`（200 裸数组，字段 snake_case）与 `DELETE /pki/tokens/{token_id}`（200 `{"revoked": id}`；不存在 404；审计 `pki.token.revoke`）；签发审计补 `token_id`（签发与吊销两处可对齐追溯）。鉴权零改动：router 已挂认证器链 + RBAC 最小版，DELETE 因 `WRITE_METHODS` 自动要求 admin 组
  - 吊销语义：删除即失效——Bearer 校验每请求重读台账（无缓存），吊销后立即 401、无法恢复；「谁在何时吊销了哪个 token」由操作审计留痕
  - CLI：`kurrent token list [-o table|json]`（TOKEN_ID / USAGE / GROUPS / COMPONENT-AGENT / NAME / EXPIRES / CREATED）与 `kurrent token revoke <token_id>`（`cli/token.go` 分发改 switch；列表复用 agents 的 tabwriter 范式；`cli/main.go` usage 补两行）
  - WebUI：新增「凭据」页（路由 `/credentials`，侧边栏位于 Agents 与 Operations 之间）——token 台账列表（含归属 / 签发 / 到期时间，旧条目签发时间显示 `-`）、ConfirmAction 二次确认吊销、「签发管理 token」一次性明文展示 + 复制；`api/client.js` 增 `getTokens` / `revokeToken` / `issueAdminToken`；`webui/DESIGN.md` 同步
  - 测试：`tests/control_plane/test_join_flow.py` 新增 `TestTokenManagement` 4 例（列表字段与无 hash、过期条目过滤与旧条目空 `created_at`、吊销删除 + 审计、吊销不存在 404）；`tests/control_plane/test_auth.py` 新增端到端 1 例（Bearer 200 → 证书通道吊销 → 同一 token 立即 401）；全量 `pytest tests` 295 passed
  - 文档：`api/control-plane-api.{zh-CN,en}.md`（端点表 + 6.5 说明与新增 GET/DELETE 两小节）、`cli/README.md`、`webui/DESIGN.md` 同步
  - 边界：会话全踢（session 轮换）与证书台账 / 吊销（需 CRL）另立项，本次不做
- **节点健康反映数据面拓扑（`health` 三态 `ok` / `degraded` / `error`）**——`GET /agents` 的 `health` 此前只有 ok / error：宿主服务停机可经 capabilities 探测落为 error，但**宿主服务存活而 configfs 未就绪**（宿主前置缺失）时能力接口照常 200，节点显示健康、数据面实际不可用：
  - Agent `GET /healthz`：nvmet 后端追加 `backend_ready` / `backend_detail`（宿主服务可达且 configfs 就绪才为 true），进程存活（`status:ok`）与数据面就绪分开表达；探针 2s 超时 + 5s 结果缓存，控制面高频轮询不放大宿主服务压力
  - 控制面 `GET /agents`：`health` 增 `degraded`——`backend_ready=false` 时标 degraded，具体原因（`backend_detail`；capabilities 也失败时保留前者不覆盖）写入 `error` 字段；仅 agent 整体不可达才 error
  - WebUI：`degraded` 显示黄色徽章（`badge-warn`），徽章外包 `title` tooltip 展示原因
  - 测试：agent 3 例（就绪 / 宿主服务不可达 / configfs 未就绪）与控制面 3 例（ok / degraded 且 capabilities 失败不覆盖原因 / error）
  - 文档：`api/control-plane-api.{zh-CN,en}.md` 的 `GET /agents` 补 health 三态语义
- **母盘 4K 原生校验（建盘前拒绝 512 扇区 GPT）**——nvmet file backend 按底层 FS 块（典型 4096 B）导出逻辑块，512 字节扇区布局的 GPT 母盘在 4K 导出下被发起端按 4K 解释、EDK2 找不到 ESP（生产实测 sanboot `EFI_NOT_FOUND`）；主仓建盘此前无任何校验，克隆出不可引导的盘只在引导时暴露：
  - `storager/agent/app/nvmet.py`：新增 `disk_gpt_layout()`（GPT 头 LBA1 探测：offset 4096 → `4k` / offset 512 → `512` / 无签名或读取失败 → `none`）与 `NvmetBackend.validate_disk_file()`（`512` → 400 拒绝并给出「`losetup --sector-size 4096` 后重建 GPT」指引；无 GPT 签名告警放行）
  - `storager/agent/app/main.py`：`Backend.validate_disk_file` 基类默认 no-op（stgt / LIO 不受影响）；`POST /lun/disk` 克隆前校验（拒绝时不落 backing、不建子系统）；`MasterScanner` 清单项增 `gpt` 字段（控制面 `/masters` 聚合透传，供前端标注）
  - WebUI：消费 `gpt` 字段——建盘克隆下拉中 512 布局母盘禁用并标注原因（`Select` 组件支持选项级 `disabled`）、Agent 母盘页对 512 母盘加警示 Badge；i18n 双语；`webui/DESIGN.md` 同步
  - 测试：新增 `tests/agent/test_agent_disks.py` 5 例（探测三态 + 校验拒绝 / 放行 + 端点 400 不落盘不建子系统 + 4K 正常克隆 + 母盘扫描字段）
  - 文档：`storager/nvmeof/nvmet-host/README.md` 新增「盘文件要求（4K 原生 GPT）」段（含制盘要点）；`api/control-plane-api.{zh-CN,en}.md` 第 15 节 `gpt` 字段与 14.2 的 400 情形同步

### 变更

- **存储节点宿主前置工具化（nvmet 数据面部署收口）**——节点侧此前唯一手工步骤（`modprobe nvmet nvmet-tcp` + `mount configfs`，重启持久化要自行写 `/etc/modules-load.d` 与 `/etc/fstab`）是部署踩坑面；本次把「未就绪」从静默变显式、把「就位」收敛为一条幂等命令：
  - 新增 `storager/nvmeof/host-prepare.sh`（宿主机运行，就位需 root）：加载 nvmet / nvmet-tcp 模块、挂载 configfs、写重启持久化（模块 → `/etc/modules-load.d/kurrent-nvmet.conf`，configfs → `/etc/fstab`）；幂等可重跑，`--check` 只读诊断（部署前排查，无需 root）
  - `cli/join.go`：nvmet 后端增加宿主前置预检——`/sys/module`（模块已加载）+ `/proc/mounts`（configfs 已挂载），未就绪 fail fast，报错列出缺失项并指路 `sudo bash <storager>/nvmeof/host-prepare.sh`（stgt/lio 后端不要求，照常收敛）；声明 `spec.agent.diskDir` 盘目录 join 自动创建兜底（compose bind 挂载源，缺省时可归 root 所有故提前建）
  - `storager/nvmeof/docker-compose.yml`：nvmet-host 增 mTLS 健康检查（`/healthz` 带本组件 client cert，configfs 就绪才报健康）——宿主前置缺失时如实标记 unhealthy，不再是「容器在跑但数据面不可用」；`nvmet-host/app/main.py` 启动配置端口失败时明确报「宿主前置未满足」并指路
  - 测试：`cli/join_test.go` 沙盒注入假宿主环境（`stubHostReady`），新增 3 例（宿主未就绪 → fail fast 且不启动 compose；stgt 后端跳过预检；盘目录自动创建）；全量 `go test` 通过
  - 文档：`storager/nvmeof/nvmet-host/README.md` 部署步骤 1 改为 `sudo bash ../host-prepare.sh`、`cli/README.md` 一键加入流程与要点同步、compose 头注释同步

### 修复

- **同 worker 多盘吊销残留（configfs EBUSY 中断 host 清理，旧密钥留存）**——`delete_host` 原实现摘除自身 `allowed_hosts` 链接后直接 rmdir 全局 host 目录；同 worker 挂多盘时另一子系统仍引用该目录，rmdir 返回 EBUSY → 宿主服务 500 → Agent 侧同步中断：第二块盘的链接与旧密钥留存，而凭据缓存条目已移除（reconcile 不再重放）——吊销后该盘仍可凭旧密钥连接：
  - `storager/nvmeof/nvmet-host/app/main.py`：回收改为**引用扫描式** `_gc_host`——仅当不再被任何 `subsystems/*/allowed_hosts/<hostnqn>` 引用才 rmdir；`delete_host` 与 `delete_subsystem` 均先摘链后 GC；真实 configfs 实测语义写入模块 docstring
  - 测试：`tests/nvmet_host/test_nvmet_host_service.py` 新增 2 例（共享条目保留至最后引用摘除、删子系统触发 GC）；另以真实 configfs（容器直挂 + 真实组件代码）双场景验证通过
  - 文档：nvmet-host README 认证模型补多盘共享 hostnqn 的回收语义
- **母盘标记 `_tpl_` 判定大小写敏感（大写母盘被注册为 NQN，且不出现在母盘清单）**——目录扫描跳过母盘与 MasterScanner 母盘清单两处判定区分大小写：`_TPL_win11.img` / `_Tpl_x.img` 这类命名（Windows 习惯大写）会被 scan 当普通盘**注册为子系统**（进 `/lun`，可被引导误用），同时又不在 `/masters` 清单里（无法克隆）——双重失败：
  - `storager/agent/app/main.py`（stgt/lio scan + MasterScanner）与 `storager/agent/app/nvmet.py`（scan）：判定统一为 `n.lower()`；母盘语义自此严格一致（是母盘 ⇔ 不进 scan ⇔ 进清单）
  - 测试：`tests/agent/test_agent_disks.py` 新增 2 例（scan 不注册大小写变体母盘且不报 skipped、清单认大小写变体）
  - 文档：`api/control-plane-api.{zh-CN,en}.md` 第 15 节补「大小写不敏感」
  - 存量注意：已被注册的大写母盘子系统不会自动清理（scan 只创建不删除）——标识生成时已转小写，形如 `nqn.<base>:_tpl_win11`；清理用 `DELETE /lun?iqn=<nqn>`（不带 `delete_file`，只删 target 不动文件）
- **WebUI 错误提示显示 `[object Object]`（未适配 dict detail 契约）**——控制面「Agent 代理错误透传」族（probe / LUN 管理 / 建删盘 / 删 worker / `/masters` 全失败，共 15 处）按契约返回结构化 detail `{"agent": <id>, "error": <原因>}`（API 文档「LUN 管理」节写明；CLI `detailOf` 已适配），唯独 WebUI `api/client.js` 只处理了字符串与 422 数组，dict 落入 `new Error(dict)` → `message` 即 `[object Object]`，经各页面错误横幅原样展示（任何 Agent 代理操作失败即触发）：
  - `webui/app/src/api/client.js`：新增导出纯函数 `extractDetail()`（与 CLI `detailOf` 同语义：字符串直用 / `{agent, error}` 提取 error / 422 数组映射「字段: 消息」/ 其余对象 JSON 化），错误构造改用它；对畸形 422 元素（loc 非数组、元素非对象）加防御不再抛错
  - 前端回归：新增 `webui/app/src/api/client.test.mjs`（Node 内置 test runner，零依赖；`npm test` 8 例覆盖字符串/422 数组/dict 契约/JSON 兜底/畸形输入），`package.json` 增 `test` script
  - 后端契约断言：`tests/control_plane/test_agents.py` 新增 1 例把 `{agent, error}` dict detail 钉死（capabilities 失败 → 502 + 结构断言）
  - 边界：后端 15 处契约与 CLI 均不改（文档/CLI/后端三者一致，WebUI 是唯一漏配的消费者）

---

## 2026-09-27

### 新增

- **注册窗口放行额度（`max_devices`，按设备数量放行）**——窗口期在时长之外可再限台数，防止一次放窗被批量刷入过量设备：
  - `control_plane/app/trust.py`：新增 `window_limit` / `admitted_count` / `window_quota_left` / `admission_allowed`；已放行数**派生自设备台账**（窗口期内 `source=ipxe` 且 `first_seen >= opened_at` 的条目），不另存计数器；`window_status()` 增投影 `max_devices` / `admitted` / `remaining_devices` / `quota_exhausted`（`open` 保持时间维度，额度用尽不清窗口记录）
  - `control_plane/app/routers/settings.py` + `models.py`：`POST /settings/registration-window` 接受可选 `max_devices`（1-1000，超出返回 400；省略 / `null` = 不限），操作日志带上该值
  - `control_plane/app/routers/boot.py`：`/devices/report` 入池判定改为「窗口开启 + 额度未用尽 + 有效公钥」；额度在 `devices.locked()` 内以本次加载的台账复核，避免并发上报各自读到余量 1 而超额放行。密钥认领（存量补 `key_hash`）只看窗口期，不受额度约束
  - `control_plane/app/config.py`：新增 `REGISTRATION_WINDOW_MAX_DEVICES_UPPER = 1000`（额度上限，防误填大数等于关掉额度）
  - 口径：额度只约束未知设备自动入池；管理员手动登记 / 清单导入（`source=manual`）是显式动作，不占额度；窗口关闭后重开，额度按新的开启时刻重新派生（已放行数归零重算）
  - WebUI：`pages/Devices.jsx` 开窗处增放行台数输入（留空 = 不限，前端同步 1-1000 校验，带「台」单位提示），开启中显示「已放行 n/N」并每 10s 重拉窗口状态（已放行数派生自台账，不轮询看不到额度消耗；同一 tick 顺带刷新设备列表，多选/绑定向导进行中除外，否则计数在涨而表格仍是旧行），额度用尽 Badge 转「额度已满」并提示密钥认领与手动登记不受影响；`api/client.js` 传 `max_devices`、`i18n/{zh-CN,en-US}.js` 补键、`Devices.css` 加 `.reg-window-quota` / `.reg-window-unit`、`webui/DESIGN.md` 同步
  - 浏览器实测（真实控制面 + `Devices` 页）：开窗 `max_devices=2` → 面板显示「已放行 0/2」；两次 `/devices/report` 携有效公钥后 10s 内翻为「已放行 2/2」且 Badge 转「额度已满」，第三次上报被拒（`admitted` 不增）；`GET /settings/registration-window` 返回 `remaining_devices=0` / `quota_exhausted=true`。注意容器内代码为构建期 `COPY`，改动须 `docker compose build kurrent-control-plane` 后才生效（`config/`、`state/` 才是 bind mount）
  - 测试：`tests/control_plane/test_device_registry.py` 新增 `TestRegistrationWindowQuota` 7 例（额度用尽拒入池且不写审计、状态投影随入池递增、省略 `max_devices` 即不限、手动登记不占额度、认领不受额度约束、重开窗口重置额度、上下界 0/-1/1001 返回 400）
  - 文档：`api/control-plane-api.{zh-CN,en}.md` 的 5.1（GET 字段表 / POST 请求体 / 说明段）、16.6 上报行为、引导链概览与「注册窗口期注册」控制项表同步

### 变更

- **存储节点管理语义落地（A 类三批次）**——同一台节点的字段按"唯一主人"分层：节点声明 `storager/kurrent.yaml` 拥有 base_url / remark / role.cd；控制面 `agents.yml` 拥有 enabled / storager_ip / 人工 tags；后端类型不存副本、实时问节点（见同日"修复"）：
  - **批次 1 `enabled` 收窄为排空语义**：`false` = 只拦新增——自动挑选（原有）之外，显式点名建盘（单盘 `disk_agent` 与批量 `target.agent`）与直接对该节点建 LUN（disk/cd 端点）一律 409；**存量盘照常服务**、已引导设备的 boot-vars 投影不受影响（`storager_ip_for` 无 enabled 守卫是裁定的一部分，边界回归锁定）。顺手修批量分支死代码（`agents.get` 抛 KeyError 而非返回 None，原 `if not disk_agent` 使缺失项落成 500 `"'xx'"` → 清晰 404）与 `create_worker_disk` 的 `except Exception` 吞 HTTPException（补显式放行，校验类 4xx 不再变 500）
  - **批次 2 声明权威补完**：`AgentRegistry.refresh_from_declaration()`——已在册 agent 再次 enroll/renew 时按节点声明刷新 base_url / remark / role.cd（控制面属性不碰；节点未上报的字段保留现值），节点搬家改声明后 renew 即对齐、无需重装；`PUT /agents/{id}` 对自动登记节点（tags 含 `auto`）拒绝改 base_url（409 并指路改节点声明——否则改动会在下次 renew 被声明值冲掉；手工登记节点仍可改）；WebUI 编辑弹窗对自动登记节点禁用地址输入并附灰字提示（i18n 双语）
  - **批次 3 删除语义与登记缺失韧性**：`DELETE /agents/{id}` 语义定为**仅清控制面记录**（软删除：节点下次 renew 自动重新登记，真正下线在节点侧做），响应新增 `disks_hosted` / `affected_workers` 告知（派生自 workers.yml，仅提示不拦截）；修两处"登记缺失即崩"的路径——`_push_credentials` 找不到节点改为跳过并记审计（原 KeyError 使 delete_worker 半路 500 且设备未解绑）、`_delete_target` 找不到节点在 ignore_missing 时视为已缺失、否则清晰 404（原来 500 `"'ag-01'"`）
  - 测试：新增 11 例（排空四处入口 + boot-vars 存量边界回归 + 批量停用/缺失 agent + enroll 声明刷新 3 例 + PUT base_url 拒改/放行 2 例 + 删除告知 + 登记缺失删 Worker），全量 `pytest tests` 290 passed
  - 文档：`api/control-plane-api.{zh-CN,en}.md` 6.1/6.3（enabled 排空、base_url 归属与 409）、6.5.1（enroll 刷新规则改写，原"已登记不更新、agents.yml 为权威"与裁定相反）、6.6（软删除语义与新响应字段）、7.1/7.1.3/14.2/14.3（排空 409 与批量 failed 文案）、7.4/11（登记缺失时的 ignore 语义）；`webui/DESIGN.md` Agents 段
  - 边界：Agent 侧 `/config` 对账端点（两次 renew 之间的漂移可见性）明确不做，待真实需求再议

### 修复

- **CLI 管理凭据发现改为逐级上溯（修"仓库内子目录调用必 401"）**——原实现只认 `<cwd>/control_plane/state/admin.conf` 单一路径，而从 `cli/`（二进制所在目录，最自然的调用位置）执行时发现不到凭据，静默退回无凭据明文通道，表现为难以定位的 `POST /pki/tokens -> HTTP 401: unauthorized`（同一命令在仓库根可正常签发）：
  - `cli/credentials.go`：`discoverCredentials()` 改为两步自动发现——① 从 cwd **逐级上溯** `control_plane/state/admin.conf`（git 式，命中最近祖先）；② 退回**可执行文件旁** `<exe 目录>/../control_plane/state/admin.conf`（仓库布局 `cli/kurrent` → 仓库根，覆盖从仓库外调用该二进制的情形）。命中返回绝对路径（错误提示据此指明实际使用的凭据）；`exeDir` 变量化以便测试注入
  - `cli/api.go` + `authHint()`：401 且**两条凭据通道都空**时追加排查提示（点明"未发现管理凭据"并给出三种修法），不再只丢一个 `unauthorized`；已发现凭据或已给 `--token` 时不追加（此时 401 属凭据本身问题）
  - 测试：`cli/credentials_test.go` 新增 4 例——子目录上溯发现、多级祖先取最近不越级、cwd 无凭据时按可执行文件旁发现、`authHint` 触发条件；原 `TestCredentialsDiscovery` 断言同步为绝对路径
  - `cli/README.md`：发现顺序与回退语义按实际行为重写（原写"repo 根目录下直接可用"）
  - 实测：`cli/` 下 `kurrent agents list` 由 401 转为正常返回；仓库二进制从 `/tmp` 调用经 exe 旁发现亦可；无同级仓库的孤立二进制仍 401 但附带发现提示
- **节点侧文档与注释去陈旧环境变量（业务配置早已收敛到 `storager/kurrent.yaml`，文档仍停在 env 时代）**：
  - `storager/.env.example`：join 签名由 `<cp-url> <bootstrap-token> <agent-id> [--nvmet-token T]` 改为现行 `kurrent join <cp-url> --token <bootstrap-token>`（节点身份自动生成、后端等业务配置写入节点声明）
  - `storager/nvmeof/nvmet-host/README.md`：部署步骤改为 `kurrent join` 主路径（另留"仅重启本编排"的手动路径；验证 curl 改用真实证书路径 `control_plane/state/pki/components/nvmet-<agent_id>/client.{crt,key}`）；**「环境变量」表重写**——本组件实际只读 `KURRENT_CONFIG_FILE` / `NVMET_CONFIGFS` / `NVMET_PORT_ID`，监听地址端口为模块常量（`0.0.0.0:4841`，compose 不注入）；原表列的 `KURRENT_AGENT_ID`/`KURRENT_COMPONENT`/`KURRENT_PKI_DIR`/`KURRENT_CP_ENROLL_URL`/`KURRENT_CP_CA`/`KURRENT_BOOTSTRAP_TOKEN(_NVMET)` 全部改为"已收敛 + 新来源"（声明 `metadata.name` / `spec.controlPlane.url`、compose 挂载、`storager/bootstrap/nvmet-host.token`），并注明 `KURRENT_DISK_DIR` 与 `KURRENT_NVMET_PKI_HOST` 仍是 `.env` 插值键（compose 挂载源，不经组件代码读取）
  - `storager/nvmeof/docker-compose.yml`：nvmet-host 段两处注释——启用条件由 `KURRENT_BACKEND=nvmet` 改为声明 `spec.agent.backend=nvmet`；修正"见 `../kurrent.yaml` 的 `spec.agent.nvmetHostUrl`"（该键**不存在**，且在节点声明 `extra="forbid"` 下写了会启动即失败；地址实为部署事实常量 `storager/agent/app/config.py:DEFAULT_NVMET_HOST_URL`）
  - 同类陈旧注释（退役环境变量名 → 声明键名）：`storager/agent/app/nvmet.py`（实例化分支条件）、`storager/agent/app/pki_client.py`（`base_url` 与能力摘要的来源）、`control_plane/app/routers/enroll.py`（自动登记 `base_url` 来源）
  - 边界：CHANGELOG 历史条目与 gitignored 的 `docs_copy/` 不动；`cli/join.go` 的 `migratedEnvKeys` 与 `tests/agent/test_config.py`、`tests/agent/test_pki_client.py` 里对这些键的断言是"已退役"的权威声明，保留原样（刻意提及旧键名）
- **后端类型判定改为实时查询节点（修"过期标签覆盖实况 → 拼出错误 iSCSI 连接符"）**——`tags` 原是混合来源（enroll 按节点上报能力写入 `nvmet`/`stgt`/`lio`，运维又在控制面手填人工标签），而 `_backend_for` **标签优先**于 `/capabilities`，标签与实况漂移时按过期值拼连接符。此为存储节点管理语义（A 类）落地的第一项：承重的后端类型不再寄居 tags：
  - `control_plane/app/routers/boot.py`：`_backend_for` 去掉 tags 优先分支，一律实时查 Agent `/capabilities` 的 `backend`（节点自报，能力即真相）；未登记 / 查询失败 / 取值非法 → 默认 stgt（保持既有格式兼容）
  - `control_plane/app/routers/enroll.py`：自动登记初始标签由 `("auto","storage",backend)` 改为 `("auto","storage")`（旧 agent 不上报能力仍为 `("auto",)`）——tags 此后纯归人工
  - `control_plane/app/routers/agents.py`：`/agents/probe` 预览 `tags` 由 `["storage",backend]` 改为 `["storage"]`（`backend` 仍以独立字段返回）
  - 测试：`test_trust_chain.py` 新增 `test_backend_from_capabilities_not_stale_tags`（注册时 tags 带陈旧 `nvmet`、Agent 实报 `stgt` → 仍按 stgt 发 `iscsi-sep`；旧实现下必失败）；`test_agents.py` 的 probe tags 断言与 `test_join_flow.py` 两处自动登记 tags 断言同步。全量 `pytest tests` 279 passed
  - 文档：`api/control-plane-api.{zh-CN,en}.md` 的 5.1 `iscsi_sep` 行、6.1/6.3 `tags` 字段与全部示例（不再以 `stgt`/`lio` 作 tag 示例）、6.2 推导表、6.5.1 自动登记四处同步
  - 存量影响：本次改动前已登记的 agent，其 `agents.yml` 里仍留有 `nvmet`/`stgt`/`lio` 标签，现已不参与任何判定（纯展示）；需要清理时经 `PUT /agents/{id}` 重写 tags
  - 边界：A 类其余各项（`enabled` 排空语义收窄、声明↔台账权威与 renew 刷新、`agents delete` 前置守卫）本次不动

---

## 2026-09-23

### 修复

- **文档同步（以代码为准，逐条校正陈旧表述）**——接口文档、WebUI 设计文档与代码内注释同实现对齐：
  - `api/control-plane-api.zh-CN.md` / `api/control-plane-api.en.md`：API 示例入口由
    `http://<host>:4839/...` 改为唯一对外入口 `https://<host>/api/cp/...`（引导链
    `/devices/report` 保持裸路径、无鉴权）；1.1 补 TOFU 自签证书的 curl 用法
    （`-k` 或 `--cacert control_plane/state/certs/server.crt`）；1.3 认证链措辞由
    “并列尝试”改为“串行短试（首个成功即止）”；示例凭据变量统一为 1.3 定义的 `$TOKEN`；
    移除已退役的 `IPXE_CP_TOKEN` 表述；端口清单重写（nginx `80`/`443`、dnsmasq `53`/`67`/`69`、
    Control Plane `4839` 仅回环、Agent `4840`、nvmet-host `4841`、NVMe-oF `4420`、iSCSI `3260`）
  - `api/control-plane-api.{zh-CN,en}.md` 第 2 节：标题由「文件即真相 / Files Are the Source of Truth」
    改为「状态与配置文件」；表格补齐当前状态面（`devices.yml` 标注为绑定关系唯一权威，补
    `credentials.yml` / `settings.json`）；修正两处与实现不符的说明——`dhcp-hosts.conf` 由「唯一真相」
    改为「派生视图」，写入方式由「临时文件 atomic replace」改为「截断写保持 inode」
    （该文件以文件级 bind mount 进 dnsmasq 容器，换 inode 即读不到新绑定）
  - `about/{zh,en}/AI_POLICY.md`：贡献者问题清单中「文件即真相」一问改为「状态与配置分别落在哪里、
    谁是权威、谁是派生物」；第五节 PR #3 案例里已退役的 `KURRENT_CP_AUTO_REGISTER=false` 改为当前
    机制（注册窗口默认关闭、TTL 1–60 分钟、身份验签强制开关，见 `state/settings.json`），
    同句的「注册 Worker」一并改为「入池」以对齐注册≠授权；第二章问题清单里仅剩的两处旧数据面措辞
    一并更新——修辞举例「手写 iSCSI 登录报文」改为「数据面的登录报文（iSCSI Login / NVMe-oF Connect）」，
    末条问题「iSCSI 会话保活机制…」改为「数据面会话的保活与移交（iSCSI 的 NOP 保活 /
    NVMe/TCP 的 Keep-Alive Timer）」，覆盖并存的两个数据面
  - `webui/DESIGN.md`：路由 5→7（补 Devices 页与 Agent 详情页）；布局由顶部导航改为可折叠侧边栏；
    新增 `Modal` 组件与登录页/认证门；鉴权由构建期 token 改为会话 Cookie（前端不落任何凭据）；
    部署由独立 compose 改为仓库根 `docker-compose.yml` 的 `kurrent-nginx`；目录结构与产物体积同步
  - 代码内注释：`routers/enroll.py`、`pki.py` 的吊销/renew 语义（“移除即拒绝 renew”仅对
    nvmet-host 成立，agent 组件在 renew 上会自动登记）；`auth.py` 认证器链串行短试；
    `storager/nvmeof/nvmet-host/app/main.py` 运行形态（容器 + host 网络 + `4841`，且必须
    挂载盘目录以解析 configfs `device_path`）；`tftp/boot.ipxe.cfg` 固件补丁编号
    （`0006`/`0007`/`0008`，原写 `0008`/`0009`/`0010`）
  - `cli/README.md`：依赖表述由“零第三方依赖”改为“仅 `gopkg.in/yaml.v3`”
  - `about/zh/ARCHITECTURE.md` / `about/en/ARCHITECTURE.md`（中英同步）：重写为当前架构——三角色现状
    （控制面仅回环 + nginx 443 单入口、Storager 的 Agent/nvmet-host 双组件与 4420 数据面）、
    盘标识 NQN 权威与 `os_tag`、引导链补入设备身份信任根（NVRAM 密钥 / 挑战响应 / TOFU）
    与 NBCT·NBFT 分工、状态与配置收敛为一句（撤销"文件即真相"独立章节）、
    安全边界拆成"管理面认证"与"设备身份信任根"两层、协议演进改为 NVMe-oF 为主路径
  - `assets/architecture.svg`：按当前架构重绘（原图为 iSCSI 时期形态：LSC 容器、WebUI `:4838`、
    iSCSI 数据面、无任何 NVMe 字样）。新图为英文标注的单一图源（沿用原决策），
    等宽字体 / 零圆角 / 黑白线稿，含控制面·存储节点·设备三区、四条跨区通路
    （HTTPS 引导链、mTLS 管理、NVMe/TCP 数据面、DHCP·TFTP）与关键点标注
  - `webui/app/.env.example`：作废 `VITE_CP_TOKEN`（前端已不读取任何构建期凭据）

---

## 2026-09-15

### 新增

- **对外 API 认证升级：认证器链 + Identity（kubeadm/k8s 同构）**——管理面从“单一共享
  Bearer token（空=放行）”升级为三种凭据**串行短试（首个成功即止；凭据无效即 401，不降级）**、统一产出 `Identity{name, groups,
  method}`：① mTLS 客户端证书（443，nginx 校验链后透传 DN，CN=用户名、O=组）
  ② 管理 token（Bearer，pki 台账 usage=admin，只存 sha256 摘要、TTL/吊销）③ 会话
  Cookie（`kurrent_session`，HMAC-SHA256 签名、HttpOnly/Secure、TTL 7 天滑动续期）。
  授权分层：读请求只需已认证（不做组校验）、写请求需 admin 组（RBAC 最小版）；审计自动附加 actor
  （state.current_actor → operations.jsonl）；开发模式仅 `KURRENT_CP_INSECURE=1` 显式
  放行——空放行路径移除（`KURRENT_CP_TOKEN` env 通道退役）
- **管理凭据自举并入组件 PKI（admin.conf，kubeadm 同构）**——控制面启动时以内部 CA
  签发 admin 客户端证书（CN=admin、O=admin，TTL 365 天 → `state/pki/users/admin/`）
  并渲染管理凭据文件 `state/admin.conf`（kurrent 原生扁平 YAML：`server` +
  `certificate-authority`/`client-certificate`/`client-key`，0600；证书路径相对引用，
  宿主/容器视角一致；服务端信任锚 = TOFU `server.crt`；历史 kubeconfig 结构启动时
  自动无损迁移为扁平格式）；会话签名密钥 `state/session.key` 自举；
  凭据文件属主对齐 `state/` 目录属主（容器 root 写卷时宿主用户直接可读，免 sudo
  chown——kubeadm 需 `sudo cp admin.conf` 的体验简化）
- **会话与身份端点**——`POST /session`（凭据换会话 Cookie）、`DELETE /session`（登出）、
  `GET /whoami`（返回当前 Identity；WebUI 自动认证探测用）
- **CLI 管理凭据自动发现（kubectl 同构）**——新增全局 `--credentials`（env
  `KURRENT_CREDENTIALS`；默认发现 `control_plane/state/admin.conf`），存在即以 admin
  证书走 443 mTLS（`--server` 可覆盖凭据文件中的 server 地址，便于远程管理）；无
  管理凭据时回退 `--server` + `--token`；`kurrent token create --usage admin` 签发
  管理 token；`kurrent init` 完成输出同步 admin.conf 提示
- **token 台账 usage/groups 扩展**——`POST /pki/tokens` 支持 `{"usage": "admin"}`
  （不绑组件、groups=[admin]）；bootstrap token（usage=enroll）不可作管理凭据（401）
- **WebUI 登录与会话（kubeadm / K8s Dashboard 同构）**——登录页粘贴管理 token →
  `POST /session` 换 HttpOnly 会话 Cookie（7 天滑动续期，刷新/重开浏览器保持登录）；
  启动 `GET /whoami` 探测——已导入管理证书的浏览器 mTLS 自动认证（无登录页）；
  任何请求 401 自动回登录页（会话失活钩子）；侧边栏底部登出（`DELETE /session`）；
  localStorage 明文 token 退役（前端不落任何凭据）；登录页/启动屏 mono 极简样式与
  中英文案（auth.*）；部署指南（中英）补“WebUI 登录方式”小节（管理 token / PKCS#12
  证书自动认证，含 openssl 命令）
- **测试**——新增 `tests/control_plane/test_auth.py`（三通道认证/401/会话签发·篡改·
  过期·登出/RBAC viewer·admin/凭据自举·旧格式迁移/DN 解析）；新增 `cli/credentials_test.go`
  （mTLS 请求/相对路径/发现顺序/--server 覆盖）；conftest 测试统一走
  `KURRENT_CP_INSECURE=1`

### 变更

- **唯一入口 443（端口收拢）**——控制面宿主映射改 `127.0.0.1:4839`（仅本机回环：
  init 健康检查/调试；X-Client-Cert-DN 头只信 nginx 注入，4839 不得对外）；WebUI 删除
  `4838:80` 映射、新增宿主 `80:80`；nginx 80 端口块改为 301 重定向到 443（输入
  `http://<host>` 自动升级）；WebUI SPA 与 `/browse/`、`/api/browse/` 迁移至 443 块；
  `/api/cp/` 增加 X-Client-Cert-DN 透传（仅 `$ssl_client_verify = SUCCESS` 时经 map
  非空，防无效证书伪造身份头）；nginx upstream 改容器网络服务名
  `kurrent-control-plane:8080`（4839 绑回环后 host.docker.internal 不可达）
- **API 文档 1.3（中英）鉴权章节重写**——认证器链/凭据表/RBAC/会话/开发模式；修正
  旧名 `IPXE_CP_TOKEN` 与“空则放行”描述；接口概览补 `/whoami`、`/session`；6.5 补
  token usage=admin 与管理 token 返回（groups）
- **部署指南（中英）**——端口表（4838 移除、4839 标注仅本机回环）、WebUI 访问改
  `https://<host>/`、防火墙仅放行 443、架构图同步；cli/README 配置段重写（管理凭据
  自动发现优先）
- **节点身份随机化：`metadata.name` 机器自动生成、人工输入降级为备注 `metadata.remark`**
  （示例值 `storage-nvmet-01` 退役）——身份分层与盘 os_tag 模型对齐：数据面唯一标识 =
  随机短标识，人类可读信息 = 备注。`kurrent join` 留空时自动生成 12 位 hex 随机标识
  （与盘 os_tag 同构、docker 容器 ID 短形式；已生成则持久化于声明、重跑身份不变；显式
  声明仍校验字符集）写入 `storager/kurrent.yaml` 的 `metadata.name`（机器身份，勿手工
  输入）；节点声明新增 `metadata.remark`（备注，手工输入的唯一字段）。备注全链路：
  agent 引导 enroll 上报 `remark`（读声明）→ 控制面自动登记写入 `agents.yml`（首次登记
  带入，之后以台账为权威——WebUI 编辑表单 / `PUT /agents/{id}` 可改，与 `base_url`
  生命周期一致）→ WebUI Agents 卡片 id 旁展示备注、`kurrent agents list` 新增 REMARK 列；
  `AgentConfig`/`public_dict`/`CreateAgentRequest`/`UpdateAgentRequest`/`MetadataSpec` 全量
  携带 remark。模板/示例（`kurrent config print node-defaults` 与 `kurrent.yaml.example`
  同源）、api 参考（中英 6/6.1/6.3/6.5-6.5.2）、部署指南（中英）、cli/README 同步；测试补：
  join 随机身份生成 + 重跑稳定（cli/join_test.go）、remark 声明加载（tests/agent）、
  enroll remark 上报/自动登记/旧 agent 兼容（tests/control_plane）

---

## 2026-09-04

### 修复

- **nvmet-host host 凭据登记/删除的 configfs 语义（固件仓库全链路测试反馈）**——`set_host` 在 link 显式 host 前先把子系统切严格模式（写 `attr_allow_any_host=0`）：内核在 allow_any=1（默认）时拒绝 link（-EINVAL），遗留/手工创建的宽松子系统上登记 host 认证即失败；`delete_host` 改用 `os.rmdir` 删全局 `hosts/<hostnqn>`（configfs group 删除语义——属性文件不可单独 unlink，rmdir 即连带清理），替换会静默残留目录的 `shutil.rmtree`；allowed_hosts 判断改 `lexists`。mock configfs 夹具同步 group 删除语义（rmdir 前清理属性模拟文件）；回归测试补 allow_any 兑底断言
- **menu.ipxe nbft-secret isset 链兜底（固件仓库全链路测试反馈）**——`isset ${nbft-secret} && set root-path ...?secret=...` 行尾补空 `||`：未注入 nbft-secret（明文/免认证场景）时 isset 短路不产生失败链状态，后续 `sanboot` 正常求值

---

## 2026-09-03

### 新增

- **CLI `version` 子命令**——`kurrent version` 输出版本号（`kurrent v0.3.0` 格式）；发版构建经 `go build -ldflags "-X main.version=vX.Y.Z"` 注入，日常构建显示 dev；main.go usage、cli/README 命令清单同步（顺带修正清单中 token create/join 与 usage 不一致的旧表述）；Go 测试 TestVersionOutput
- **v0.3.0 发布（GitHub Release）**——首次附带 CLI 单二进制资产（Linux amd64/arm64 + Windows amd64 交叉编译，版本号注入）

---

## 2026-09-02

### 变更

- **bootstrap token 收敛为集群级通用凭据（签发不带节点名，kubeadm token create 同构）**——新命令组 `kurrent token create`（`nodes token` 退役，cli/nodes.go 删除）：通用 token 不绑节点（节点名由 join 自决缺省宿主机名 + enroll 自动登记）、TTL 内可多次 enroll 复用（去一次性消耗，pki.py 台账 key 改 token_id，`validate_bootstrap_token` 不消耗）；新签发端点 `POST /pki/tokens`（旧 `/agents/{agent_id}/bootstrap-token` 删除，409 幂等限制随每次新签移除）；**nvmet-host 组件凭据不再手工签发**——agent enroll 上报 `backend=nvmet` 时控制面派生（先 revoke 旧条目再新签，绑 agent_id 防串用）随 enroll/renew 响应下发，agent 落盘 `storager/bootstrap/nvmet-host.token`（compose 挂载由单文件改共享目录 `../bootstrap`，agent 可写 / nvmet-host 只读）；`kurrent join` 去掉 `--nvmet-token` 只写 agent.token（nvmet 后端重跑时清除旧派生文件做迁移），.env 的 `KURRENT_NVMET_PKI_HOST` 改按 backend 写入；agent 证书就绪但派生凭据缺失时强制 renew 一次以重新派生（历史部署迁移出口）；webui 加入弹窗同步（去 agentId/nvmet 勾选，输出新 join 命令）；cli/README、部署指南（docs 中英）、api 参考 6.5 节、kurrent.yaml.example 同步；新增测试：通用 token 跨节点复用、nvmet 派生 + 防串用（401）、agent 派生落盘/跳过、token 端点每次新签；tests/requirements.txt 相对引用修复（`-r ../control_plane/requirements.txt`）
- **kurrent init/join 重构为配置即声明模式（kubeadm init/join --config 同构，yml 是唯一输入）**——命令不再带网络/节点参数：声明文件即用户维护的 `control_plane/kurrent.yaml` / `storager/kurrent.yaml`（模板由 `kurrent config print init-defaults|node-defaults` 输出，与对应 kurrent.yaml.example 同源）；`kurrent init` 读声明 → 校验（networking 五键必填 + CIDR/IP/DHCP 对格式）→ 收敛启动控制面（compose up -d → restart control-plane → 轮询 /healthz → restart dnsmasq 加载新 conf，编排为内部细节，重跑幂等 = 改声明重跑即生效；`--config` 指向其他路径时应用为控制面配置）；`kurrent join <cp-url> --token T`（kubeadm join <endpoint> 同构：命令携带控制面地址——签发命令输出即带地址，节点粘贴执行即获得，无需预编辑文件；声明 storager/kurrent.yaml 缺失时按模板自动生成（url=命令地址、name=宿主机名、backend nvmet、advertiseUrl 推导、diskDir/nqnBase 默认），已存在则读入合并——非 forbid 保留手工编辑，地址同步进 spec.controlPlane.url）→ 引导凭据落盘（`--token` 可选，不给则要求 bootstrap/agent.token 已就位）→ .env 插值键 → 收敛启动 agent（up -d + restart storager-agent，backend 决定 nvmeof/iscsi 编排）；`kurrent token create` 保留 `--cp-url`（缺省按 --server 主机推导）输出带地址的 join 命令（kubeadm token create --print-join-command 同构）；webui 加入弹窗保留控制面入口输入（join 命令带地址）；cli/README、部署指南（docs 中英）、api 参考 6.5.2、两份 kurrent.yaml.example 注释同步；Go 测试重写（声明缺失自动生成/命令地址补入/声明校验/应用外部配置/幂等重跑/print 与 example 同源比对）

## 2026-09-01

### 修复

- **enroll token 消耗顺序（nvmet-host 重试不再烧 token）**——`POST /enroll` 先校验在册再 consume：nvmet-host 与 agent 共享 agent_id，agent 未登记时 400 拒绝且 token 保持可用（restart 重试即 201），消除「400 但 token 已 used」崩溃循环；回归测试 `test_nvmet_token_not_burned_when_agent_missing`
- **storager-agent 镜像旧导致 base_url 上报缺失**——容器内 `pki_client.py` 为旧版（`docker compose up -d` 不带 `--build` 不更新 COPY 层，宿主文件与镜像内文件不同步），`--build` 重建镜像并重走 join 后恢复 base_url 登记

### 变更

- **官网兼文档站重建（docs/ 重新引入，2026-08-24 冻结裁定解除）**——VitePress 双语骨架（zh-CN 默认 root + en 子路径）；官网首页（hero「Make bare metal flow」+ 四特性 + 三角色架构图 + logo 六虚）；首篇《多机部署指南》中英双语（控制面 init + 存储节点 join 的声明式流程，含网络/端口规划、跨机 advertiseUrl 覆盖、内核 nvmet 前置）；.gitignore 的 docs/ 排除移除（docs_copy/ 保留）；修复 frontmatter YAML 冒号问题
- **kurrent init：控制面声明配置一键生成（kubeadm init 同构）**——补齐 kubeadm 侧 CLI 闭环（init/join/token）；`kurrent init --interface --subnet --dhcp-range --gateway --dns [--dir]` 生成 control_plane/kurrent.yaml（networking 五键必填参数，不自动探测——部署环境事实显式传入；其余块默认值全量写出，与 example 同源）；已存在即拒绝（preflight 同构，yml 是手工编辑的权威文件）；参数合法性校验（CIDR/IP 对）；新增 init.go/init_test.go（生成/拒绝/校验 5 用例）
- **NQN 命名域收敛为节点单一权威（消除双声明歧义）**——控制面 yml 移除 `spec.nqnBase`：盘 NQN 与 Host NQN 的 base 均以节点侧 `spec.agent.nqnBase` 为唯一权威（enroll 经 `capabilities.base_nqn` 上报）；控制面 Host NQN 派生改为盘 NQN 前缀重拼（`_base_nqn_from_target` 归入 workers.py，boot.py 复用同机制），多 Agent 异 base 场景下 Host NQN 与目标盘自动同域；hostnqn 投影与 base-nqn 同条件（无盘/盘缺 nqn 存量不注入）；config.Spec 与 Settings 移除 nqn_base
- **控制面收敛为 yml 声明式配置（kubeadm InitConfiguration 同构）**——control_plane/kurrent.yaml（apiVersion/kind/metadata/spec；spec.networking 必填五键 interface/subnet/dhcpRange/gateway/dns，pki/serverCert/boot/agentTimeoutSec/nqnBase/dnsmasq.reload 带默认值）；pydantic v2 严格校验（extra="forbid"，配置错误启动即失败）；KURRENT_CP_* 业务 env 全部退役（control_plane.env/.env.example 删除，compose env_file 移除），容器内路径/容器名变代码常量（DEFAULT_*，Settings 属性动态解析），管理口令 KURRENT_CP_TOKEN 留 compose environment 注入（凭据不进声明配置）；dnsmasq.conf 由控制面启动时按 spec.networking 生成（yml 是权威、conf 为派生物，subnet CIDR 推导 netmask，幂等写）；PKI 策略常量收敛进 spec.pki；注册窗口（运行时状态 settings.json）与 TTL 上下限（安全契约）不进 yml；新增 tests/control_plane/test_control_plane_config.py
- **kurrent join 不再携带节点自身地址（kubeadm join 同构）**——`--advertise-url` 参数移除：节点地址不进 join 命令（kubeadm 只带控制面地址/token），默认推导 `https://<cp-host>:4840`，特殊场景（NAT 等）编辑 kurrent.yaml 的 `spec.agent.advertiseUrl` 覆盖（kubelet `--node-ip` 类比）
- **kurrent join agent-id 缺省取宿主机名（kubelet Node 命名同构）**——`<agent-id>` 由必填改为可选：缺省自动取宿主机名并规范化（小写、非字母数字/点/横线替换为 `-`，保留 `.`），显式传参仍可覆盖（对应 kubeadm JoinConfiguration 的 `nodeRegistration.name` 缺省 = hostname 语义）；.env 插值键与凭据 CN 同步
- **容器内路径品牌统一（ipxe-agent → kurrent）**——容器内配置/凭据/日志路径由 /etc/ipxe-agent、/var/log/ipxe-agent 统一为 /etc/kurrent、/var/log/kurrent（Dockerfile 与两份 compose 挂载目标、应用侧模块常量同步）；代码/配置文件注释不再携带变更日期（变更记录归 CHANGELOG）
- **kurrent.yaml 分层职责修正（K8S 同构收紧）**——声明配置只保留节点级业务键：`metadata.name`/`spec.agent.{backend,advertiseUrl,diskDir,nqnBase}`/`spec.controlPlane.url`（diskDir 语义 = 宿主存储路径，kubeletConfiguration.rootDirectory 类比，join 同步为 .env 插值键 `KURRENT_DISK_DIR` 供 compose 挂载源）；容器内路径（pki/cp-ca/日志/缓存/盘目录挂载点）、监听与内部通讯地址（nvmetHostUrl/nvmetHost 块）、一次性 bootstrap token 全部移出 yml——前者归部署清单 docker-compose.yml（应用侧模块常量固化），token 移入独立凭据文件 storager/bootstrap/{agent,nvmet-host}.token（kubelet bootstrap-kubeconfig 同构，join 写入 0600、compose 挂载进容器、enroll 后即废）；kurrent-join.sh 退役（CLI 为唯一加入入口，脚本与对应测试删除）；存量 kurrent.yaml 手动清理后重启容器生效
- **Agent/nvmet-host 配置收敛为 yml 声明式（kubeadm JoinConfiguration 同构）**——业务配置从 `storager/.env` 迁移到单文件 `storager/kurrent.yaml`（apiVersion/kind/metadata/spec；agent/nvmetHost/controlPlane/pki/bootstrap 子块），agent 与 nvmet-host 容器挂载同一份只读配置、各自消费自己的 spec 子块；pydantic v2 校验（extra="forbid" 未知字段拒绝、必填缺失报错、默认值注入——启动即失败，杜绝 .env 自由键值）；.env 仅保留 compose 插值键（KURRENT_AGENT_PKI_HOST/KURRENT_NVMET_PKI_HOST）；kurrent-join.sh 与 cli/join.go 幂等写 kurrent.yaml（既有文件保留未知字段与手工业务键，旧 .env 业务键迁移继承，.env.example 同步精简）；新增 storager/kurrent.yaml.example（运行时生成文件不入库，.gitignore 追加）
- **Agent 能力上报（K8S `--node-labels` 同构）**——`pki_client.py` 引导时随 enroll 上报 `capabilities`（backend/cd；映射表 `BACKEND_CD_CAPABILITY`：stgt 支持 CD，lio/nvmet 不支持）；`/enroll` 自动登记据此推导 `tags=[auto, storage, <backend>]` 与 `role.cd`；旧 agent 缺省兼容（tags=auto、role.cd 默认 false）
- **光驱能力 UI 隐藏策略（不支持的后端不显示）**——AgentLuns 详情页：`role.cd=false` 时隐藏「+ 创建光驱」按钮与提示（移除写死 LIO 文案）、状态栏 cd 标记条件显示；Agents 列表页光驱属性行仅 `capabilities.cd=true` 时显示；i18n 键拆分（`statusRole`/`statusRoleCd`）

### 新增

- 分层职责回归测试——`tests/agent/test_config.py` 新增 `test_container_path_keys_rejected`（容器内路径/监听/内部通讯/一次性 token 键出现即拒绝）、`cli/join_test.go` 断言 yml 不含部署细节键且凭据写入 `bootstrap/*.token`、`KURRENT_DISK_DIR` 插值键同步
- 配置加载与校验测试——`tests/agent/test_config.py`（合法加载/默认值注入/未知字段拒绝（extra=forbid）/必填缺失/bad backend/坏 yaml/文件缺失）、`cli/join_test.go` 与 `tests/scripts/test_kurrent_join.sh` 断言 kurrent.yaml 幂等生成与 .env 收敛为插值键
- 回归测试 4 用例（token 不烧 1 + 能力推导 3：nvmet/stgt/旧兼容），控制面全量 187 测试通过

---

## 2026-08-31

### 新增

- **组件 PKI 体系（kubeadm 同构：bootstrap token + 组件证书 mTLS）**——控制面 `pki.py`：CA 自举（`state/pki/`）、bootstrap token 签发与一次性消耗（复合键 `agent_id/component`，Bearer 头携带）、组件证书签发（client/serving 双证书，CSR 控制面签发、serving SAN 校验）；`POST /enroll` 登记端点（agent_id/component/双 CSR/serving_sans/base_url），agent 与 nvmet-host 共享 agent_id，agent 自动登记（首次引导写入 agents.yml：base_url、role_disk、tags=auto）；`storager/agent` 与 `nvmet-host` 各新增 `pki_client.py` 引导客户端（首次引导 enroll → 证书落盘 → 到期自动续签，`KURRENT_*` 环境变量驱动，`KURRENT_ADVERTISE_URL` 支持 base_url 上报）
- **一键加入（kurrent-join.sh + CLI）**——`storager/kurrent-join.sh` 幂等 upsert `.env`（agent_id/bootstrap token/CP 地址/backend 等 8 键）+ `docker compose up -d`；`cli/`（Go）`kurrent join` 命令（连接控制面 → 签发 agent/nvmet-host 双 bootstrap token → 本地执行 join 脚本）+ `agents/nodes/workers` 子命令；`tests/scripts/test_kurrent_join.sh` 端到端脚本测试
- **Agent 管理 WebUI 增强**——Agents 页 agent 详情卡（backend/tags/base_url/health）、AgentLuns 操作面板、全局 `Modal.jsx` 组件、Layout 侧边栏重构（收起态）
- 组件 PKI 与一键加入测试套件——`tests/control_plane/test_join_flow.py`（join 全流程）、`tests/agent/test_pki_client.py`、`tests/pki_testkit.py`（测试 CA 工具）、`tests/scripts/test_kurrent_join.sh`；既有测试夹具同步 mTLS 化

### 变更

- **nginx 客户端 mTLS 认证**——`/agents/*`、`/luns/*` 等 agent 通道要求控制面 CA 签发证书，与 agent/nvmet-host 证书体系闭环；`nginx.conf` 新增 `ssl_verify_client` 配置
- **storager-agent / nvmet-host mTLS 化**——两组件对外调用改走 pki_client 证书（`agent_client.py` 双端适配），`/capabilities` 端点返回 backend/cd；nvmet-host 接入 `KURRENT_BACKEND`/`AGENT_PKI_HOST` 等配置；docker-compose/Dockerfile/requirements/.env.example 配套更新
- **WebUI 全站改造**——Agents/AgentLuns/Operations/WorkerDetail/Workers 页面重写（详情页布局：顶部状态栏 + 分类 Tab）；控制面 `agent_client.py`/`config.py`/`main.py` mTLS 与证书链适配

---

## 2026-08-30

### 变更

- **母盘身份标签分层模型（os_tag 盘级随机标识，2026-08-30 裁定）**——数据面唯一标识 = `worker_id.os.<os_tag>`（os_tag = 12 位 hex 随机串，`secrets.token_hex(6)`，docker 容器 ID 短形式；进盘 NQN / 文件名，IQN 由 NQN 派生）；上层台账 = (os, os_version, os_tag) 备注性质（人类理解用）；`build_nqn`/`build_disk_filename` 后缀带 os_tag（`worker-01.ubuntu.0d26b6f33a89`）；盘唯一性改为同一 worker 同一 (os, os_version) 至多一块（`find_disk_by_os_version`，空版本用 `''` 参与唯一键），同系统不同版本可并存；删盘/默认启动改按 os_tag 引用（`find_disk_by_tag`）
- **OS_ITEMS 退役（建盘 os 校验放开）**——2026-08-30 起不再存在合法 OS 集合：建盘 `os` 为自由字符串（小写归一，仅格式校验），同系统多版本靠盘级 os_tag 区分；`CreateWorkerDiskRequest`/`BatchCreateWorkerDiskRequest` 新增 `os_version` 字段（空 = 无版本）；`PUT /workers/{worker_id}/default-disk`（原 `/default-os` 废止）`disk` = 盘 os_tag（12 hex，精确引用具体盘），`menu_default` 严格校验集合收敛为导航项 + 遗留 OS 名（归一为 `boot-os`）
- **boot-vars 从默认盘投影系统标识 + menu 归一（MAIN MENU 动态化）**——`/boot-vars` 新增 `os`/`os_version`/`os_tag` 字段（来源 = 默认盘记录投影，与盘 NQN 后缀同源；空版本不下发），`base_nqn` 语义 = 盘 NQN 前缀（统一模板 `base:worker_id.os.<os_tag>`）；`menu_default` 推导链 `default_disk > boot.menu_default > reboot`，OS 语义默认值统一归一为唯一通用项 `boot-os`；`tftp/menu.ipxe` 主菜单收敛为唯一 OS 项（`os-label` 拼版本展示、root-path 拼 `${hostname}.${os}.${os-tag}`，新增 OS 无需改脚本）；WebUI WorkerDetail 建盘表单/默认启动选择器（os_tag）/boot-vars 预览、Workers 批量建盘同步；api/ 两份 API 文档全量同步（6.4 母盘标签新节、7.0-7.4 重写、13.6/17 节）

### 新增

- **母盘标签登记台账 + 端点（控制面登记，备注性质）**——`state/masters.yml`（`MasterTagStore`，键 = agent_id → 母盘名 → {os, os_version}，库表形态：字段原子、空版本 `''`）；`PUT/DELETE /agents/{agent_id}/masters/{master_name}/tag` 登记/清除（不校验母盘存在性，Agent 离线台账即权威）；`GET /masters` 聚合时合并标签（有登记附加 os/os_version，未登记不附加）；`KURRENT_CP_MASTERS_FILE` 配置项；WebUI AgentLuns 页母盘标签登记面板（编辑/保存/清除）；控制面测试新增母盘标签 8 用例，全量 203 测试通过

---

## 2026-08-24

### 修复

- **nvmet-host configfs 语义纠正（真实内核验证；2026-08-23「DH-HMAC-CHAP 认证三缺陷」条目方向作废，以本条为准）**——固件仓库联调实测 Linux v7.x nvmet 语义：host 条目是**全局**的（顶层 `hosts/<HOSTNQN>/dhchap_key` 写 DHHC-1 明文即启用认证，**无独立 control 属性**）；严格模式（`attr_allow_any_host=0`）host 准入 = `subsystems/<NQN>/allowed_hosts/` 下 **symlink 挂载全局 host 条目**（target 须 `nvmet_host_type`，否则 EINVAL），symlink 目标由内核按进程 cwd 解析须**绝对路径**（08-23 的「subsystems hosts/ 目录登记即准入 + nvme-auth-dhchap-control=1 置位 + 删除 allowed_hosts symlink」与真实语义不符）；端口在首个子系统挂载时才启用监听，`addr_*` 启用后不可写（-EACCES），删除 `addr_tsas` 写入（不写即无 TLS）；删除子系统须先摘 port / allowed_hosts 挂载再 rmdir（否则 EBUSY）；同步 storager/agent nvmet.py docstring、nvmet-host main.py 全量 configfs 操作、测试断言（allowed_hosts 列表 / dhchap_key / 删除顺序），nvmet-host 14 用例通过
- **/boot-vars 签名兼容还原（联调 verify_failed 修复）**——iPXE 拼 URL 不做百分号编码：base64(DER) 的 `+` 原样进入 query，Starlette 按 form-urlencoded 规则解码为空格 → 验签必败；验签前 `sig = sig.replace(" ", "+")`（base64 字符集不含空格，同时兼容 %2B 显式编码与未编码两种传递）
- **/devices/report 返回空脚本体（联调 EOF 修复）**——4 处空 `Response(status_code=200)` 改为 `Response("#!ipxe\n", media_type="text/plain")`：空 body 部分 iPXE 报 EOF，统一返回合法空脚本供 chain 直接消费（无脚本副作用）
- **凭据幂等重放补推**——`set_worker_credential` 同值幂等分支（changed=False）也调 `_push_credentials`：上次推送失败（agent 离线 / 中途失败）时重放同值密钥可补推，agent 侧 set_host 幂等、重复推送无害

### 变更

- **Host NQN 注入（C2 凭据链路配套，固件 0011 补丁消费）**——`/boot-vars` 新增 `hostnqn` 字段（iPXE / JSON 双格式）：iPXE nvmetcp 默认 hostnqn 为 `nqn.2014-08.org.ipxe:<uuid>`（无 UUID 回退 `:ipxe`），与 nvmet-host 登记的 `host.<worker_id>` 不匹配则严格模式认证必败，须按 worker 投影同一身份覆盖；`HOST_NQN_PREFIX` 硬编码改读 `settings.nqn_base`，新增 `KURRENT_CP_NQN_BASE` 配置（默认 `nqn.2026-07.com.kurrent`，与 agent/storager 的 `KURRENT_NQN_BASE` 同源，变更须两侧同步）；WebUI Worker 详情 boot-vars 预览补 hostnqn 行；api/ 两份 API 文档字段表 + iPXE/JSON 示例同步（示例域 .controller → .kurrent）；全量 189 测试通过（.venv-linux，Python 3.14，含 hostnqn 断言）
- **iscsi_sep 条件化（仅 stgt / lio 后端下发）**——`_worker_boot_payload` 仅 `backend in {"stgt", "lio"}` 时下发 `iscsi_sep`（nvmet 无 iSCSI target 不下发）；`_backend_for` 支持 nvmet（tags / capabilities 优先序 nvmet → lio → stgt）；`menu.ipxe` 5 个 iSCSI 安装器项加 `isset ${iscsi-sep} || goto start` 守卫（nvmet 后端跳过安装器项）；api/ 文档字段表同步
- **nvmet-host 容器化部署形态调整（联调部署）**——`network_mode: host`（nvmet 监听 socket 由写 configfs 的进程创建、绑定其网络命名空间；host 模式即宿主 netns，客户端可直达 4420，容器重建不丢监听；原 `127.0.0.1:4841` 端口映射与 compose 内部网络访问方式作废，Agent 改经 `host.docker.internal:host-gateway` + `http://127.0.0.1:4841` 访问）；`security_opt: apparmor:unconfined`（Ubuntu docker-default AppArmor profile 拒 configfs 写入）；新增 `NVMET_HOST_DISK_DIR`（configfs device_path 由写入进程所在挂载命名空间解析，按 basename 重拼到容器可见目录，不配置则原样写入）；compose 磁盘挂载与 agent 的 `KURRENT_DISK_DIR` 内容一致、路径可不同
- **构建源阿里云化（国内网络加速）**——control_plane / storager-agent 两个 Dockerfile 的 apt 源改阿里云镜像（deb822 `debian.sources` 与老式 `sources.list` 双格式 sed 覆盖，只替换域名）；nvmet-host Dockerfile 的 pip 走阿里云 PyPI 镜像（与其余服务一致）

## 2026-08-23

### 变更

- **API 文档独立于文档站（docs/ 冻结待重写）**——控制面 API 参考移出 docs/ 站体系：新建根目录 `api/`（`api/control-plane-api.zh-CN.md` / `api/control-plane-api.en.md`，内容自包含无站内链接），此后接口变更仅维护该两份；docs/ 目录整体冻结不再修改（vitepress 文档站为 iPXE-All-Ready 时代产物，架构升级后整体重写，docs/ 内旧副本保留待重写时处理）；CHANGELOG 记录规范中 API 文档权威位置引用同步更新
- **固件仓库更名 Kurrent Firmware（dutyc/kurrent-firmware）+ ABOUT 标语定稿**——配套固件仓库 `ipxe-stateless` 更名 `kurrent-firmware`，ABOUT 标语定为 *The firmware engine for Kurrent. Make bare metal flow at the boot layer.*；主仓库引用全量同步（中英 8 文件）：README 两版「固件仓库」小节（新名称/链接 + 标语引言）、《项目环境部署》1.3 节与固件升级警告（名称 + 仓库/Releases 链接）、《引导介质制作指南》本地加载引用、about ARCHITECTURE 协议演进章节固件分支引用；CHANGELOG 历史条目与蓝图契约文件名（ipxe-stateless-handoff）保留原名（历史记录，GitHub 仓库改名后旧链接 301 重定向）
- **C3 引导链拼接实施（NVMe-oF 数据面激活）**——`/boot-vars` 退役单值 `nqn` 投影，改投 `base_nqn`（盘 NQN 前缀；盘 NQN 由 Agent 按统一模板 `base:worker_id.os` 生成，无存量盘下固件重拼 = 盘记录权威值；盘记录缺 nqn 不下发）；`iscsi_server` → `storager_ip` 全面更名（boot-vars 键 / iPXE 变量 `storager-ip` / `agents.yml` 配置键 / Agent API 字段 / WebUI 表单与 boot-vars 预览 / 中英 API 文档字段表）；`menu.ipxe` 主引导 5 OS 项（windows/ubuntu/debian/centos/esxi）改 NVMe-oF 拼装 `nvme://${storager-ip}:4420/${base-nqn}:${hostname}.<os>` + `nbft-secret` isset 条件化附加 `?secret=`，安装器 5 项保持 iSCSI（引用同步改名）；`boot.ipxe.cfg` 静态默认变量与 vars-done 注释同步；测试 153 通过
- **修复：nvmet-host DH-HMAC-CHAP 认证三缺陷**——`hosts/<hostnqn>/dhchap_key` 非内核属性名（真实为 `nvme-auth-dhchap-secret`，真 configfs 上 open 即 FileNotFoundError，从未生效）改为正确属性写 DHHC-1 明文（无换行）；新增 `nvme-auth-dhchap-control=1` 置位（不置位认证不启用，密钥形同虚设）；删除 `allowed_hosts` 手动 symlink（内核在 allow_any_host=1 时自动维护的连接记录，严格模式准入靠 hosts 登记）；同步 delete_host/delete_subsystem、Agent docstring、nvmet-host README 认证模型章节、测试断言（14 用例通过）
- **Kurrent 全量品牌化（GitHub 仓库改名 dutyc/kurrent 后同步）**——环境变量前缀全量 `IPXE_*` → `KURRENT_*`（控制面 `KURRENT_CP_*`、存储节点 `KURRENT_BACKEND`/`KURRENT_NQN_BASE`/`KURRENT_AGENT_TOKEN`/`KURRENT_DISK_DIR`/`KURRENT_LOG_FILE`/`KURRENT_NVMET_*`/`KURRENT_ISCSI_CONTAINER`、HTTPS 入口端口 `KURRENT_HTTPS_PORT`；`.env.example`、compose 插值、webui 构建注释、tests conftest 同步）；容器名 `ipxe-*` → `kurrent-*`（根编排 dnsmasq/control-plane/webui 三服务，存储节点 `kurrent-nvmet-host`，自签证书 CN `kurrent-controller`）；NQN 命名空间统一 `nqn.2026-07.com.kurrent`（盘 NQN base 示例由 `.controller` 域更新；Host NQN 落地 C3 点 1 裁定：按 worker_id 派生 `nqn.2026-07.com.kurrent:host.<worker_id>`，废弃设备 UUID 派生与 `:ipxe` 共享回退，解绑后 host_nqns 恒定不变）；about/ 宣言与 AI 政策、CHANGELOG 抬头、README Star History URL 收尾；测试保留 `nqn.2026-07.com.test` 隔离域（产品示例与测试域分离）；vitepress 文档站为 iPXE-All-Ready 时代产物暂不动（架构升级后整体重写）；全量 189 测试通过

## 2026-08-22

### 变更

- **README 项目更名 Kurrent（周流）**——英文名 Kurrent（取自 current，C 换成 K8s 生态的 K），中文名周流（取自《周易》“周流六虚，上下无常”）；开头重写为品牌形态：`Make bare metal flow.` + 与 K8s 对标（K8s orchestrates the containers. Kurrent orchestrates the compute.），定位由“云原生无状态计算平台”升格为“云原生无状态裸金属交付范式”（中英同步，核心特性标题改“算力周流，即插即用”）；badges 与快速上手仓库 URL 同步为 `github.com/dutyc/kurrent`（准备 GitHub 仓库改名）；固件仓库段品牌化；其余文件（docs 站、CHANGELOG 抬头、NQN 命名空间）留待后续批量更名
- **challenge 响应契约定稿：#!ipxe 脚本体（设备信任根链 R7 对齐）**——`GET /devices/challenge` 响应由 text/plain 单行 `nonce=<64hex>` 改为 `#!ipxe\nset nonce <64hex>\n`：iPXE chain 直接消费为 `${nonce}`，原单行格式无法被 iPXE 作为脚本执行（固件侧 usage 文档假设 #!ipxe，联调前裁定修改控制面）；boot.py 响应体修改，测试同步更新（test_trust_chain / test_device_registry 按 `#!ipxe` 首行 + `set nonce ` 解析），123 用例复跑通过
- **API 参考补录 challenge 端点（中英同步）**——control-plane-api.md 端点表新增 `GET /devices/challenge` 行、新增 5.2 节（query 参数 mac、成功 #!ipxe 响应示例、400 非法 MAC / 404 未注册未认领、三步挑战-响应链路）、boot-vars Query 参数表补 nonce/sig 行（强制开启后缺失 → missing_sig 拒绝）；/boot-vars iPXE 接入方式示例同步为信任根链形态（HTTPS + nonce/sig 签名路径 + challenge 失败降级路径）
- **固件侧 R1-R5 交付对齐（ipxe-stateless 仓库）**——固件实施完成（补丁 0008/0009/0010：HTTPS + keygen/pubkey/sign 命令 + EFI 变量 NVS 后端 + TOFU 自动固化，QEMU 实测）；thirdparty/docs/device-trust-usage.md 按主仓库实际契约修订（/devices/report 空响应语义、/devices/challenge、/boot-vars 带 nonce+sig、阶段 4 注释残留修复）；handoff / trust-root-blueprint 状态行与 nonce 契约定稿同步
- **T5 引导链集成（主仓库侧，QEMU 联调后置）**——nginx 新增 443 ssl server（TLS1.2、TOFU 单锚统一证书、`/devices/*` + `/boot-vars` 反代控制面、/file/ 资产与 /tftp/ 菜单同源）；证书由**控制面启动时自动生成**（cert_bootstrap 幂等：RSA-2048 自签叶证书 CA=False、SAN 可配 `IPXE_CP_CERT_SAN`（默认 IP:127.0.0.1,DNS:localhost）、指纹输出 state/certs/fingerprint.txt（DER SHA-256 hex，与 openssl x509 -outform DER | sha256sum 同格式），新增 4 单测；nginx Dockerfile 等待证书就绪后启动，compose 挂载 state/certs 只读；gen-cert.sh 与 webui/deploy/nginx/certs 挂载废止）；docker-compose nginx 服务加 `443:443` + certs/tftp 卷；tftp/boot.ipxe.cfg 信任根链集成（keygen → pubkey → report → challenge → sign → boot-vars，challenge 失败降级不带 nonce/sig，menu/chain-url HTTPS 化）；environment-deploy（中英）新增 1.4.2「HTTPS 引导链（T5，必需）」：证书自动生成与指纹获取、固件 0008/0009/0010 前置条件、旧固件断链警告、过渡期验签强制开关语义、证书轮换说明
- **HTTPS 引导入口端口可配置化**——nginx 宿主端口映射改 `${IPXE_HTTPS_PORT:-443}:443`（容器内固定 443，nginx.conf 零改动）；boot.ipxe.cfg 新增 `set https-port`（默认 443）拼入 https-url；environment-deploy 1.4.2 说明两处同步要求（compose 变量 + 脚本变量）
- **NVMe-oF 认证凭据库 + 凭据 API（C1，按 Worker 跟盘裁定）**——新增 `CredentialStore`（`state/credentials.yml`，复用 `_load_yaml`/`_atomic_write_text` 原子写 + 线程锁，键 = worker_id；`IPXE_CP_CREDENTIALS_FILE` env）；workers 域 3 端点（API Token 鉴权）：`PUT /workers/{worker_id}/credential`（DHHC-1 自检：前缀 `DHHC-1:`、类型两位数字、base64 解码长度 36/68、zlib.crc32 小端终值校验，失败 422；worker 不存在 404；同值幂等 `updated_at` 不变）、`DELETE /workers/{worker_id}/credential`（吊销，不存在 404）、`GET /workers/{worker_id}/credential`（元数据 `exists`/`secret_hash` 前缀/`updated_at`，不返回明文）；审计 `credential.set`/`credential.revoke`/`credential.get` 不记密钥本体
- **/boot-vars 注入 nbft-secret（C2）**——`_worker_boot_payload` 追加 `nbft_secret`：绑定 Worker 在密钥库有条目时注入（按 Worker 跟盘：注入值 = 该 worker 密钥，无条目 → 不注入回落明文）；`_boot_vars_ipxe`/`_boot_vars_json` 渲染 `set nbft-secret <key>`；审计 `boot_vars.credential`（injected 布尔，不记密钥本体）；固件侧消费 `sanboot nvme://...?secret=${nbft-secret}`（C3 引导链分支后置，本轮只投影变量）
- **nvmet 管理服务容器化（部署形态调整，不再手动跑 Python）**——`ipxe-nvmet-host` 以容器运行（与 Agent 同一 compose 托管，删除 systemd unit 与宿主 venv 部署路径）：内核 nvmet target 与 configfs 仍宿主（modprobe + mount configfs 为唯一宿主步骤，无法容器化）；容器无 privileged（configfs bind mount 后容器内 root 直写，与 LIO 的 targetclid/dbus 特权依赖无关）；端口映射仅绑宿主 loopback `127.0.0.1:4841`（可本机 curl 验证），局域网不可达，Agent 经 compose 内部网络 `http://nvmet-host:4841` 访问；`.env` 的 `IPXE_NVMET_HOST_TOKEN` 经 compose 插值注入容器 `NVMET_HOST_TOKEN`（不再需要 `/etc/ipxe/nvmet-host.env`）；新增 nvmet-host Dockerfile，compose 新增 `nvmet-host` 服务块，部署文档（中英）2.2/2.2.1/2.3 与 nvmet-host README、.env.example 同步更新
- **存储节点目录重组：iscsi-server → storager/（iSCSI 与 NVMe-oF 后端独立编排）**——`iscsi-server/` 整体更名 `storager/`，后端按协议分目录：`storager/iscsi/`（stgt/lio）与 `storager/nvmeof/`（nvmet-host）各自目录持有**完整独立编排**（`docker-compose.yml` 内嵌共享 Agent 服务块，仅代码共享 `storager/agent/`，无跨文件引用）；部署命令统一为 `cd storager/iscsi|nvmeof && docker compose --env-file ../.env up -d`（.env 单一来源，插值经 --env-file 读取）；同一节点同一时刻仅启用一个后端 compose（`storager-agent` 容器名固定，互斥语义不变）；`.gitignore`、tests conftest 路径、部署文档（中英）结构图/2.1/2.2/2.2.1/2.3/2.5、nvmet-host README、`.env.example` 同步更新，全量 189 测试通过
- **存储节点服务命名统一 storager- 前缀**——存储节点服务不再用 ipxe- 前缀：`ipxe-agent` → `storager-agent`、`ipxe-iscsi` → `storager-iscsi`（两个 compose 的 service/image/container_name 同步；`IPXE_ISCSI_CONTAINER` 配置默认值改为 `storager-iscsi`——Agent 经 docker.sock 按该变量调度容器，无硬编码容器名）；日志路径 `/var/log/ipxe-agent` 与 `IPXE_*` 环境变量名不变（不属于服务名）；部署文档（中英）2.1/2.2/2.3 与探索文档容器示例同步；全量 189 测试通过
- **NVMe-oF 宿主 nvmet 服务 + Agent 推送驱动（C4）**——新组件 `iscsi-server/nvmet-host/`（FastAPI + Bearer token，绑定 localhost:4841，root 运行）：configfs 直写 `/sys/kernel/config/nvmet`（subsystem/namespace/port/hosts+dhchap_key，严格模式 `attr_allow_any_host=0`，allowed_hosts 相对 symlink），端点 `/healthz`/`/capabilities`/`/subsystems` CRUD + hosts 同步 + `/port`；systemd unit 样例 + README（modprobe nvmet nvmet-tcp、configfs 挂载、token env）；Agent 新增 `NvmetBackend`（不继承 Backend 基类——基类初始化 docker 客户端，nvmet 无 docker 依赖；iqn 字符串直接作子系统 NQN）与 `NvmetHostClient`（urllib 零依赖）+ `NvmetCredentialCache`（**缓存先行** 0600 落盘 + 60s 周期 reconcile 幂等重放 + 子系统 404 丢弃过期条目）；`POST /credential` 端点（非 nvmet 后端 400、宿主不可达 503）；控制面 `_push_credentials` 推送驱动（凭据设置/吊销、设备绑定/解绑/换绑、建盘/删盘含批量、删 Worker 共 8 触发点，失败仅审计不阻断）；Host NQN 派生 `nqn.2014-08.org.ipxe:<uuid>`（无 UUID 回退共享 `nqn.2014-08.org.ipxe:ipxe`）；env：`IPXE_NVMET_HOST_URL`/`IPXE_NVMET_HOST_TOKEN`/`IPXE_NVMET_CACHE_FILE`；单测 36 例（宿主服务 13 + Agent 23，Windows symlink/路径冒号兼容层）+ 控制面推送链路 7 例，全量 189 通过；真实挂载验证留部署环境（本机 Windows 无内核 nvmet）
- **NQN 标识体系修正（盘记录双标识，C4 缺陷纠正）**——子系统 NQN 不可用 IQN 替代（NVMe Base Spec §7.9：NQN 必须 `nqn.` 前缀，发起端拒绝 `iqn.` 前缀）：控制面新增 `iqn_to_nqn`（同后缀前缀变换），建盘/批量建盘盘记录新增 `nqn` 字段（存量盘读取时由 `iqn` 派生兼容）；`_push_credentials` 推送 `sub_nqns` 改用 NQN；Agent `NvmetBackend` 经 `to_nqn` 变换后写 configfs（create/delete/list/scan 全链路），`list_targets` 新增 `nqn` 键（`iqn` 键保留历史契约，值同为 NQN），`capabilities` 本地补 `base_iqn`（宿主服务未知 `IPXE_IQN_BASE`——此前 nvmet 建盘会 KeyError）；`_target_actual` 标识集合对比兼容双数据面；测试断言全量改 NQN，全量 189 通过
- **存储节点变量文件按 NVMe-oF 首选原则翻转（.env.example + 部署文档 2.3）**——`IPXE_BACKEND` 默认值 `lio` → `nvmet`（nvmet = NVMe-oF 首选/默认，stgt/lio = iSCSI 降级选项）；`IPXE_ISCSI_CONTAINER` 移至「iSCSI 降级段」并标注仅 stgt/lio 后端需要（nvmet 后端经 HTTP 驱动，不读该变量）；`IPXE_IQN_BASE` 注释改为双数据面语义（iSCSI 直接用为 IQN 前缀，NVMe-oF 派生 NQN `nqn.` + iqn[4:]）；NVMe-oF 专属变量（`IPXE_NVMET_HOST_URL`/`IPXE_NVMET_HOST_TOKEN`/`IPXE_NVMET_CACHE_FILE`）提前为后端专属第一段；部署文档（中英）2.3 `.env` 示例块同步翻转；Agent 代码零改动（`IPXE_BACKEND` 必填无默认，默认值仅体现在模板）
- **NQN 权威标识体系（盘标识方向反转：NQN 唯一配置入口，IQN 自动派生）**——`IPXE_IQN_BASE` 废止，节点只配置 `IPXE_NQN_BASE=nqn.2026-07.com.controller`（NQN 权威命名空间）；IQN 一律由 NQN 自动生成（`nqn_to_iqn` = `iqn.` + nqn[4:]，幂等），NQN 绝不由 IQN 定义；控制面新增 `build_nqn`（base_nqn + worker_id + os 生成盘 NQN）与 `nqn_to_iqn`，建盘/批量建盘盘记录 nqn 权威在前、iqn 派生（存量盘记录 `iqn_to_nqn` 回退保留，不违反方向原则）；capabilities 契约 `base_iqn` → `base_nqn`（scheduler 校验、agents 台账、建盘消费同步）；Agent 改读 `IPXE_NQN_BASE` 并内部派生 `IQN_BASE` 供 iSCSI 后端（stgt/lio），`NvmetBackend` 构造参数改 nqn_base（内部派生 iqn_base 供 scan 候选）；CD 盘经 `nqn_to_iqn(cd_caps["base_nqn"])` 派生；boot-vars `base-iqn` 变量保持 IQN 形态（iSCSI 引导消费，来源 = 盘 NQN 派生）；`.env.example`、部署/API 文档（中英 9 份）、WebUI（Agents 页展示与探测改 base_nqn、WorkerDetail 盘记录补 nqn 权威行）同步；全量 189 测试通过
- **boot-vars 下发盘 NQN（权威标识到消费端）+ 存量回退取消（架构基线 = research/nvme-of 分支，面向未来）**——`/boot-vars` 新增 `nqn` 字段：默认启动盘的盘 NQN（权威字段），iPXE 渲染 `set nqn <...>` / JSON 同键，固件侧 `sanboot nvme://<ip>:<port>/${nqn}` 拼装消费（C3 引导链分支后置，当前只投影变量）；与 `base-iqn` 同选盘规则（默认启动盘）、同条件（无系统盘不返回）；盘记录缺 `nqn`（存量盘）**不返回该键、不派生**——架构设计基于 research/nvme-of 分支、完全面向未来，不兼容 iSCSI 设施与遗留环境：`iqn_to_nqn` 函数删除（utils.py），`_push_credentials` 跳过缺 nqn 的盘（不推凭据），nvmet.py `to_nqn` docstring 去除引用；文档（中英）字段来源表 + iPXE/JSON 示例同步；全量 189 测试通过

---

## 2026-08-21

### 变更

- **ARCHITECTURE.md 协议演进章节细化（NVMe-oF 验证落地，中英同步）**——基于 ipxe-stateless research/nvme-of 分支验证结果：固件层 nvmetcp 驱动原生执行 `sanboot nvme://`、DH-HMAC-CHAP 认证（控制面按次启动注入 `nbft-secret`，不进固件镜像、不进引导菜单）、NBFT 接力链路 QEMU 端到端闭环；明确双数据面并存（iSCSI 生产路径 + Windows 回退通道，NVMe-oF 迁移方向）与认证/加密两条独立主线（TLS 为认证后开放主线）；蓝图侧同步更新（blueprint/ 不入库）：security-blueprint C6/已定决策 2 过时假设修正、ipxe-nvmeof-stack-blueprint 里程碑状态标注（M1/M2 已验证）、新增 nvmeof-credential-design.md 认证凭据落实设计（/boot-vars 注入契约、密钥库、3 个凭据 API 端点设计；密钥管理模型列为待定决策，暂不讨论）
- **信任根架构裁定：固件内置 CA 作废，信任根归位设备 NVRAM 密钥（蓝图专项）**——裁定「固件里不放 CA 证书，固件是所有的局域网设备都可以获取的，固件里面放 CA 无意义」：security-blueprint 已定决策 3 修订（信任根 = 设备 NVRAM ECDSA P-256 私钥，固件侧生成、私钥不出设备、控制面只存公钥）、§6 三层身份 CA 层取消、§8 注册窗口改固件生成+公钥上报、待定决策 1（CA 归属）消解（TOFU 模式下自签证书即可）；新建 blueprint/trust-root-blueprint.md（双向信任模型：设备侧 TOFU 服务器证书指纹固化 NVRAM + 控制面侧挑战-响应设备签名；自研 EFI 变量 NVS 后端复用上游 NVO 层；UEFI-only、BIOS 遗留回退；T1-T5 实施分期）；nvmeof-credential-design 注入条件扩为四条（新增设备身份签名验证）；thirdparty/ipxe 上游源码克隆查证（NVO 抽象层 / SetVariable 先例 / ecdsa_sign / EFI RNG 熵源）
- **抛弃传统 BIOS 引导：UEFI-only 升级为全系统基线（蓝图专项联动）**——裁定「抛弃 BIOS-only 设备」（存量可忽略，X3650 M4 级 2012 硬件已带 UEFI）：trust-root-blueprint 待定决策 2（BIOS 平台信任根）消解、§2.3/§3.4 BIOS 载体表述修订；ipxe-nvmeof-stack-blueprint 待定决策 2（BIOS 构型）消解、平台基线/引导链流程图/D3/D5/M7/风险表同步修订；security-blueprint 头部裁定声明追加；iSCSI 回退通道保留、载体收窄为 UEFI（照走 TOFU / 设备签名保护）；固件仓库 BIOS 构建目标（grub-bios / undionly / usb）**暂时保留不砍**（后续裁定：仅架构设计不考虑 BIOS）
- **信任根蓝图待定决策收敛：证书轮换 = 重新进注册窗口，设备密钥算法 = ECDSA P-256（蓝图专项）**——裁定：trust-root-blueprint 待定决策 1（设备密钥算法）消解为 ECDSA P-256（上游 ecdsa_sign 已有、32B 私钥 NVRAM 占用最小、密码学零新写、nonce 重用风险由 EFI RNG 熵源覆盖）；待定决策 3（证书轮换）消解为重新进注册窗口（双指纹过渡被否——需设计受旧指纹保护的新指纹下发通道）；§4.3 证书轮换节更新；security-blueprint 待定决策 1 的证书轮换引用同步更新；至此信任根蓝图待裁定的决策项全部收敛（剩余为实施期/对齐项：EFI 变量 GUID、私钥轮换与吊销、密钥生成侧）
+- **信任根蓝图源码复核修正 + 明文引导面决议（蓝图专项）**——基于 thirdparty/ipxe 上游源码逐行核对修正三处表述：默认构建内置 iPXE root CA（rootcert.c，非「无内置 CA」，附带 ALLOW_TRUST_OVERRIDE=1）、`${trust}` setting 生效路径（rootcert_init 启动早期一次性从 NVRAM settings 源读入，脚本层 `set trust` 无效，DHCP 来源无效）、上游无 TOFU 原语（TLS 验证失败即断链、证书不进 certstore，「首次握手固化指纹」需固件补丁扩展点，R2 交付物改新补丁）；裁定 boot.ipxe.cfg 获取保持明文（HTTP/TFTP）——明文引导面 = boot.ipxe + boot.ipxe.cfg 两个收敛指针脚本（内容收敛红线 + 静态降级参数非敏感化，被篡改可利用价值由 TOFU 兜底降级为引导 DoS），其后全链 HTTPS + TOFU，TOFU 固化时点后移到首个 HTTPS 请求；trust-root-blueprint T5/R1/R2/§3.2/§4.1/§4.2/§10.3 同步修订，nvme-of-bootchain-blueprint 架构图/引导协议/TRUST 构建参数、nvmeof-credential-design 引导态传输表述联动修订
+- **信任根注册模型三裁定（蓝图专项）**——① TOFU 首信 = 固件补丁（自动 TOFU：TLS 验证失败路径扩展点，窗口期首启接受握手 + 取指纹写 NVRAM；部署期预置指纹方案否，与无接触部署节奏冲突）；② 注册只在窗口期：注册窗口取代 auto_register 永久开关（settings.json 持久开关语义废止，无窗口外注册通道，代码层不可配永久）；③ 存量设备密钥认领：已部署设备无豁免，窗口期内开机走一遍密钥认领（固件生成密钥对 + 公钥上报 + key_hash 填充），认领后全部受验签保护；trust-root-blueprint 头部/§4.1/§5/§5.2/T3、security-blueprint 头部/§8、control-plane-update-plan 差异表/P4 行联动修订
- **存量未认领设备引导拒绝 + 补救路径（注册模型裁定④，蓝图专项）**——窗口关闭后无 key_hash 设备直接拒绝引导（注入四条件第 4 条硬性不过，已绑定也不放行）；补救路径 = 删除设备池登记 → 重开注册窗口重新注册 → 恢复 worker 绑定；trust-root-blueprint 头部/§5/§6.1/T4、security-blueprint 头部/§8、control-plane-update-plan P4 行联动修订
- **信任根控制面实施（T3/T4）：注册窗口 + 设备身份验签全链路落地**——main.py 按域拆分（2341 行 → 18 行组装入口 + routers/ 六域包，boot 域最先挂载保证 `/devices/report`、`/devices/challenge` 先于 `/devices/{mac}` 匹配）；`auto_register` 永久开关废止：新增 `GET/POST/DELETE /settings/registration-window`（TTL 1-60 分钟硬上限，到期懒计算自动关闭，已开启 409）与 `GET/PUT /settings/enforcement`（验签强制开关，持久化 settings.json）；`GET /devices/report` 扩展 `pubkey` 参数（ECDSA P-256 公钥，130 hex 未压缩点，注册/认领用，密钥不一致拒绝覆盖仅审计）；新增 `GET /devices/challenge`（一次性 nonce 64 hex、短 TTL、未注册/未认领 404）；`/boot-vars` 注入四条件第 4 条：无 key_hash（强制开 → `no_key`）、缺 nonce/sig（强制开 → `missing_sig`）、缺 hostname、nonce 重放、验签失败一律拒绝并审计 `boot_vars.credential`；过渡期无密钥设备照现状放行（伪造签名不放行）
- **控制面全链路单元测试（T9）**——tests/ 扩展至 123 用例（5.08s）：EC 密钥对夹具 + 签名 helper + 注册/认领高层夹具；覆盖设备登记/挑战（15）、验签链路与强制开关（15）、设备池 CRUD/绑定/批量（17）、worker 全生命周期（23）、agent 注册/探测/母盘（16）、审计分页（8）；测试即契约固化产品行为（iscsi_server 回退时机、base-iqn 前缀语义、审计 id 进程内自增、MAC 冒号格式、AgentClient 属性结构）
- **WebUI 注册窗口面板（T8）**——Devices 页旧「自动注册」开关替换为「注册窗口」面板：状态徽章 + TTL 选择（5/15/30/60 分钟）开启 + 倒计时（本地逐秒递减，归零回源刷新）+ 提前关闭 + 「设备身份验签强制」开关；API 文档（control-plane-api.md 中英）5.1 节与 16.6 节、快速部署三文档（environment-deploy 1.4.1 / webui-guide / windows-quick-deploy）同步为注册窗口模型

---

## 2026-08-16

### 变更

- **文档站：新增《WebUI 使用指南》（中英同步）**——新增 `docs/guide/quick-deploy/webui-guide.md` 与 `docs/zh/guide/quick-deploy/webui-guide.md`：页面分区功能说明（Dashboard / Workers / Devices 设备池 / Worker 详情 / Agents / Operations）+「设备入池 → 绑定向导（默认顺序分配）→ 克隆建盘 → 默认启动 → 验证」核心流程 + 常见问题（Token 401、设备不入池、向导左栏无设备、母盘下拉缺失、停在 iPXE 菜单）；《项目环境部署》1.6 / 2.6 验证节与底部流程入口同步链接；侧边栏导航（zh/en）同步
- **快速部署文档修正 P1 反转前的过期流程（中英同步）**——《项目环境部署》1.4.1：自动注册语义由「新 MAC 自动注册为 Worker」改为「自动入设备池」，开关位置由 Workers 页工具栏改为 Devices（设备池）页工具栏，关闭后的手动处理由「添加 Worker」改为「注册设备 / 导入清单 / POST /devices 入池后绑定向导」；《Windows 无盘快速部署》《Debian 系无盘快速部署》第 3 步由「自动注册 Worker + Workers 页查看」改为「自动入池 + Devices 页确认 + 绑定向导绑定」，「批量上线」与「找不到 iSCSI 目标」排查表述同步对齐
- **《项目环境部署》2.4 登记 Agent 补充 WebUI 方式（中英同步）**——由仅「编辑 `agents.yml`」改为两种方式任选：方式一 WebUI（推荐，Agents 页「+ 添加 Agent」两步探测注册：填 Agent ID / API 地址 / Token → 探测自动获取后端/角色/标签/数据面地址 → 确认 iSCSI 数据面为 Worker 可达地址 → 添加，写入 `agents.yml` 立即参与调度）；方式二直接编辑 `agents.yml`（原内容保留）；多节点部署说明同步；《WebUI 使用指南》Agents 页描述同步补全
- **WebUI：Dashboard 新增设备数量统计卡**——统计卡由 Workers / Agents 两卡扩展为 Workers / 设备池 / Agents 三卡，设备数为 `GET /devices`（含全部状态）计数，与 Workers/Agents 同源并行拉取；中英文案同步
- **WebUI：设备池「导入清单」更名为「登记设备入池」并加悬停提示**——与「绑定向导 → 清单配对」的混淆点消除：按钮文案改为「登记设备入池」（en: Register to Pool），鼠标悬停提示「不涉及绑定」（No binding involved，原生 title 提示）；页面介绍弹层中相关描述同步（自动注册开关说明、操作按钮说明）；《WebUI 使用指南》工具栏描述同步
- **WebUI：Workers / Agents 页新增「页面介绍」按钮**——与 Devices 页同款弹层：Workers 页工具栏右侧（顶部操作按钮 / 筛选 / 列表列 / 行交互四区块，含批量创建、批量建盘分配方式、就绪度语义）；Agents 页工具栏右侧（工具栏两步探测注册与在线探测开关 / Agent 卡片字段 / 行交互三区块）；中英文案同步，《WebUI 使用指南》对应页面描述同步
- **README 两版修正 P1 反转前的过期描述（简介 + 核心特性）**——「自动注册」→「自动入设备池」：简介改为「上报指纹自动进入设备池，WebUI 绑定 Worker、克隆系统盘、设定默认系统后即自动进入目标系统」（en 对应 "reports its fingerprint and joins the device pool automatically, then a few clicks in the Web UI bind it to a Worker, clone a system disk and set the default OS"）；核心特性改为「新机器首启自动入设备池，WebUI 点几下即可绑定 Worker、分配系统盘与默认系统」（en 对应 "report their fingerprints and join the device pool on first boot; a few clicks in the Web UI bind them to a Worker..."），与 2026-08-16 首页 Zero-Touch 表述及 P1 语义对齐
- **架构图全英文化 + ARCHITECTURE.md 拓展（中英同步）**——`assets/architecture.svg` 重制为全英文版并作为唯一图源（删除 `architecture-en.svg`，两版文档引用同步）；README 两版架构小节引用同步；`about/en/ARCHITECTURE.md` 与 `about/zh/ARCHITECTURE.md` 由三角色简介拓展为完整架构讲解：控制面/数据面分离（控制面故障不中断数据面 I/O）、三角色（Controller 容器化组件与纯文件态存储、Storager Agent 封装后端差异、Devices 指纹入池无状态计算）、三实体模型（设备/Worker/系统盘表格 + 绑定权威在设备侧与盘机分离 + 生命周期 pooled→bound→revoked 与原子换绑 + 多盘多系统在线切换）、引导链路五步（入池 → boot-vars 绑定校验 → iSCSI 登录）、状态存储文件即真相（devices.yml / workers.yml / settings.json / operations.jsonl / agents.yml）、安全边界（绑定即认证、key_hash 预留双向认证位）、协议演进（数据面可换 NVMe-oF，语义不变）

---

## 2026-08-15

### 新增

- **Control Plane：设备↔Worker 一对一绑定（P2 绑定语义）**——绑定关系权威在设备侧（`bound_worker_id`），worker 侧只投影；新增 `POST /devices/{mac}/bind`（默认 409，`force=true` 原子换绑：预校验 → 新绑定落盘 → 旧绑定清除（旧设备回池）→ 失败回滚台账快照 + 尽力恢复 dnsmasq，幂等）、`DELETE /devices/{mac}/bind`（解绑回池，盘留 worker）、`POST /devices/bind/batch/preview`（只读配对表：matched/conflicts/not_found + summary，manifest 清单配对 / sequential 顺序配对，可选申报列指纹比对 `fingerprint_mismatch`）、`POST /devices/bind/batch`（幂等执行：succeeded/skipped/failed 逐项独立，已绑定重跑全 skipped）；审计 `device.bind`（含 old_worker_id/old_device_mac 换绑历史）、`device.unbind`、`device.bind.batch`
- **Control Plane：Worker 就绪度派生字段**——Worker 列表/详情/状态响应新增 `bound_device`（绑定设备 MAC）与 `readiness`（绑定+有盘 → `ready`；绑定或有盘 → `partial`；皆无 → `idle`），由 `_enrich_worker` 统一投影
- **Control Plane：boot-vars 防冒领（D2，绑定即认证）**——带 `mac` 的 `/boot-vars` 请求须来自 hostname 命中 worker 的绑定设备，不符（绑定其他 worker / 未绑定 / 未知设备）→ 拒绝下发空脚本；不带 mac（仅 hostname）兼容放行
- **Control Plane：批量创建 Worker（P3）**——新增 `POST /workers/batch`：`count`（1–100）+ `name_prefix` 命名规则生成 `worker_id`（`worker-01`…，位宽随 count 自适应），逐项独立（单项失败不影响其余）、幂等（已存在 → `skipped` 不重复创建）；`macs` 可选（与 `count` 等长时逐项校验设备池并直接绑定，池外/已绑定 → 该项 `failed` 且不创建，修正后可重试）；不支持 `windows_iso`；审计 `create_worker.batch`（含 created/skipped/failed/prefix）
- **WebUI：设备池页 + 绑定向导（P3）**——新增 Devices 页（状态过滤/搜索 + 指纹详情 + 手动注册 + 清单导入 + 多选解绑二次确认）；新增「设备绑定」向导（manifest 清单 / sequential 顺序配对 → 预览配对表 → 导出 TSV 核对 → 二次确认执行 → 失败项保留清单重试）；Workers 页表单扩展数量 + 命名规则批量创建、列表新增绑定列（`bound_device` + readiness 三态 Badge）；路由 / 导航 / 中英文案同步

### 变更

- **`POST /workers` 的 `mac` 改为可选（P2）**——不传 = 纯空转 Worker（仅 hostname 绑定，readiness=idle）；传 = 校验设备在设备池中（pooled）并直接绑定（一对一授权），池外/已绑定 → 409（先注册后绑定，语义反转的预期影响面）；绑定失败时 worker 保留为空转
- **`PUT /workers/{worker_id}/mac` 映射为设备换绑（P2）**——新 MAC 须在设备池中（pooled）并绑定到本 worker，旧设备（若绑定本 worker）解绑回池；dnsmasq `replace_binding` 失败回滚台账；审计记 `device.bind` + `device.unbind` + 兼容 `worker.mac.update`；幂等返回 `changed=false`
- **`DELETE /workers/{worker_id}` 与 `POST /workers/delete/batch` 联动解绑（P2）**——先解绑设备落盘（回池不吊销，失败中止删除），再删 worker

- **Control Plane：设备台账（`DeviceStore` + `state/devices.yml`）**——三层实体模型（设备 / Worker / 系统盘）底层实体落地（P1）：绑定关系权威在设备侧（`bound_worker_id`），worker 侧只投影；设备状态 `pooled`（池中未绑定）/ `bound` / `revoked`；新增端点 `GET /devices`（state 过滤）、`GET /devices/{mac}`、`POST /devices`（手动注册入池）、`POST /devices/import`（批量导入，逐项独立，重复跳过、非法/吊销计 failed）、`DELETE /devices/{mac}`（注销吊销，bound 设备 409 须先解绑）；`key_hash` 字段预留安全蓝图阶段
- **Control Plane：`GET /devices/report`（不鉴权）**——iPXE 设备信息上报入口（11 字段：mac/uuid/厂商/型号/序列号/CPU/内存总量/内存类型/内存频率/网卡/总线 ID），宽松解析（空值容忍，`mem-total`/`mem-speed` 兼容 `0x` hex 与十进制、归一化十进制存储），更新指纹 + `last_seen`；未知 MAC 且 auto_register 开 → 入池；返回空响应（chain 无脚本副作用）；`tftp/boot.ipxe.cfg` 在请求 `/boot-vars` 之前先 chain 上报（失败静默跳过，不阻断引导）
- **Control Plane：旧数据迁移（启动时幂等）**——扫描 workers.yml + dhcp-hosts.conf，为存量 MAC 绑定生成 bound 设备实体（`source=manual`，指纹空，等待首次上报补充）；已存在跳过，失败仅记日志不阻断启动

- **自动注册语义反转（P1）**——新 MAC 不再自动创建 Worker + dnsmasq 绑定，只入设备池（`_auto_register_worker` 移除，入池收敛到 `/devices/report` 与 `/boot-vars` 兜底）；`/boot-vars` 变为只读投影（无写副作用）；识别链改为 hostname→worker、mac→设备台账→`bound_worker_id`→worker（迁移后存量绑定自动生效）；池中未绑定 → reboot 循环等待绑定；未知 MAC + auto_register 关 → 空脚本；审计操作码 `auto_register` → `device.register`
- **`IPXE_CP_AUTO_REGISTER` 开关语义**——由「新 MAC 是否自动注册为 Worker」变为「新 MAC 是否自动入设备池」，端点与持久化方式不变（运行时 API 优先于环境变量）
- **WebUI：自动注册开关迁移至设备池页**——Workers 页工具栏的全局自动注册开关移至 Devices 页工具栏（与「新 MAC 自动入设备池」语义同页对齐），Workers 页工具栏随之精简；开关 tooltip 文案同步修正为 P1 反转后的准确语义（原文案仍停留在「自动注册为 Worker」的旧描述）；中英文案同步
- **WebUI：设备池页新增「页面介绍」按钮**——Devices 页工具栏右侧新增介绍按钮，弹出遮罩面板逐项说明页面各功能区（自动注册开关 / 筛选与计数 / 操作按钮 / 列表列 / 行详情展开），中英双语
- **WebUI：绑定向导新增「图形化顺序分配」模式（替代原顺序配对文本模式）**——向导模式由「清单配对 / 顺序配对（文本粘贴）」改为「清单配对 / 图形化顺序分配」：左侧勾选池中未绑定设备（指纹摘要、搜索、全选），右侧勾选可用 Worker（按勾选顺序），按列表顺序一一对应自动分配；Worker 不足时按补建前缀自动调用 `POST /workers/batch` 补建差额，Worker 超出时截断；分配结果锁定后走预览 / 导出 / 二次确认 / 失败重试（graphical 重试沿用锁定分配、不重复补建）；向导数据源独立于页面状态过滤；后端零改动（复用 sequential 模式与 batch 端点）
- **WebUI：设备列表固定按入池时间排序**——设备池列表与绑定向导「图形化顺序分配」左栏统一按 `first_seen`（首次上报/入池时间）升序排列（无 `first_seen` 排最后），替代原 MAC 排序，作为绑定/沟通时指代设备的顺序基准（“从上往下第 N 台”）；纯前端展示层改动，后端零改动
- **Control Plane：`GET /operations` 支持 `mac` 过滤 + 批量绑定逐条审计**——新增可选 `mac` 参数（规范化后按设备过滤操作流水，用于设备绑定记录查看）；`POST /devices/bind/batch` 除汇总 `device.bind.batch` 外，每个 `succeeded` 项另逐条记录 `device.bind`（`mac`/`worker_id`），保证设备绑定历史完整，`skipped`/`failed` 仅汇总计数
- **WebUI：设备页绑定记录 + MAC 复制按钮**——设备行详情展开新增「绑定记录」区块（展开时按 mac 拉取审计，仅显示 bind/unbind 类事件，最新在前，换绑显示 `旧 worker → 新 worker`）；设备行 MAC 旁新增复制按钮（点击复制到剪贴板，1.5s 内 ✓ 反馈）
- **WebUI：绑定向导默认「顺序分配」**——向导默认模式由「清单配对」改为「顺序分配」（图形化双栏勾选分配），「图形化顺序分配」名称简化为「顺序分配」，「清单配对」保留为可选项（去除「默认」标注）；中英文案同步

### 修复

- **`/boot-vars` reboot 循环 payload 缺 `worker_id` 导致 500**——池中未绑定设备的新返回体（`menu_default`+`menu_timeout`）不含 `worker_id`，`_boot_vars_ipxe` 直接索引抛 KeyError（运行级验证暴露）；改为 `payload.get('worker_id', 'unbound')` 兜底，JSON 输出不受影响

---

## 2026-08-13

### 变更

- **《项目环境部署》1.3 节固件来源改为 ipxe-stateless（中英同步）**——不再从 iPXE 官方发布站 boot.ipxe.org 下载，改为从配套固件仓库 [iPXE-Stateless](https://github.com/dutyc/ipxe-stateless) 的 [Releases](https://github.com/dutyc/ipxe-stateless/releases) 页面下载最新 release；以正式语气说明不建议使用官方固件的动因（官方构建未包含高性能网卡原生驱动，RTL8125/RTL8126 仅能走 UNDI/SNP 兼容路径，引导可能失败）；以表格列明所需资产与放入 `tftp/` 后的文件名（`undionly.kpxe` + `pxe-uefi-snponly.efi` → `snponly.efi` + `pxe-uefi-ipxe.efi` → `ipxe.efi`，可选 `*-debug.efi` 调试版，去 `pxe-uefi-` 前缀匹配 dnsmasq 分发名）；`ipxe-legacy.efi` 随新产物移除；下载步骤精简为指引（不再提供逐条 wget / sha256sum / mv 命令）；UEFI 引导异常排查流程更新（先换 `ipxe.efi`，仍异常用调试版抓日志，替换前备份、定位后换回正式版）
- **README 两版「云原生固件仓库」小节精简**——参照 ipxe-stateless 仓库「项目定位」的概况风格，将 1 句引言 + 3 条要点列表压缩为一句全局概况，不含技术细节：与主仓库同一理念的一体两面（主仓库让算力无状态，固件仓库让引导固件无状态）
- **workers.yml 移出版本库，纳入 .gitignore**——`control_plane/state/workers.yml` 为控制面运行时台账（Worker 注册状态），提交会打乱部署环境；经 `git rm --cached` 从索引移除（工作区文件保留），并加入 .gitignore 运行时区块（注释同步改为「运行时状态、日志与租约」），与 `operations.jsonl` 同等对待
- **dhcp-hosts.conf 改为示例模板入库，主文件不再 push**——新增 `dnsmasq/dhcp-hosts.conf.example` 模板（含格式说明与示例绑定、复制命令），`dnsmasq/dhcp-hosts.conf`（MAC → hostname 运行时绑定）经 `git rm --cached` 移出版本库并纳入 .gitignore；部署者须先 `cp dhcp-hosts.conf.example dhcp-hosts.conf` 再填写真实绑定（docker-compose 以文件级 bind mount 挂载该文件，缺失时容器侧会生成目录导致 hostsfile 失效）
- **dnsmasq.conf 改为示例模板入库，主文件不再 push**——新增 `dnsmasq/dnsmasq.conf.example` 模板（环境相关项标注「[按实际修改]」：网卡名、DHCP 地址池、网关，引导链架构配置原样保留），`dnsmasq/dnsmasq.conf`（含真实网段）经 `git rm --cached` 移出版本库并纳入 .gitignore；环境部署文档 1.2 节同步补充首次部署先复制模板的步骤（文件级 bind mount 下缺失会生成目录导致配置不生效）
- **README 两版按 Kubernetes README 风格整体重构精简**——参照 kubernetes/kubernetes 经典 README 的「短段落 + 入口导航」风格：引言压缩为一段（去掉「PoC 演进史」段与「All/Ready」口号段）；Quick Start 压缩为最简命令（补充 `dnsmasq.conf` / `dhcp-hosts.conf` 模板复制步骤，去掉 WebUI token 构建与存储节点部署的可选步骤，外链部署手册）；Community & Contributing 的 AI 政策约 20 行压缩为一句核心要求 + 链接（完整政策保留在 AI_POLICY 文件）；Roadmap 去掉 Phase 状态段压缩为一句 + 链接；API 描述去重复化；License/Star History 保留；架构图引用与中英徽章行对齐
- **README 原 AI 辅助描述整合进 AI_POLICY（中英同步）**——逐条比对 README 删除的 AI 政策描述与 AI_POLICY 现有内容，确认立场声明、核心原则、PR #3 案例均已覆盖，仅缺 README 原第 5 条架构理解要点「iSCSI 会话保活机制在整个链路中的位置与影响」，已补入 AI_POLICY 两版第二章的问题清单（现共 5 条，与 README 原清单一一对应，无内容丢失）
- **Manifesto 标题改为「我们的云原生」，根目录文档分类整理入 about/（中英分目录）**——Manifesto 两版标题由「My/我的 Definition/云原生定义」改为「Our/我们的 Definition/云原生定义」（README 两版引用同步）；新建 `about/en/`（Manifesto、AI_POLICY、Barriers、ROADMAP）与 `about/zh/`（对应中文版）两个目录，经 `git mv` 分类存放，README/CHANGELOG/LICENSE 仍留根目录；README 引用路径全部更新，全仓库 git grep 确认无旧路径残留（文档站与根目录文档本就零引用，不受影响）
- **README「架构」部分独立为 ARCHITECTURE.md（中英双版）**——README 的 Architecture 小节（架构图 + 三角色说明）拆出为 `about/en/ARCHITECTURE.md` 与 `about/zh/ARCHITECTURE.md`（架构图引用路径修正为相对新目录 `../../assets/`），README 只保留一句 + 双语链接，进一步贴近 K8S 纯入口风格
- **about/zh/ 目录中文文档去除 _zh-CN 后缀（中英文件名统一）**——`about/zh/` 下 Manifesto / AI_POLICY / Barriers / ARCHITECTURE 四份中文文档文件名去掉 `_zh-CN` 后缀（目录已按语言区分，后缀冗余），README 两版与 CHANGELOG 当日条目中的引用路径同步更新，全仓库 grep 确认无 `_zh-CN.md` 引用残留
- **README「核心特性」小节列表段落化（中英同步）**——6 条 bullet 列表改为一段话（K8S 风格短段落）：零手工开通、一机多系统、秒级启动、文件即真相、API 优先五个要点并入 3 句连续叙述，保留全部信息量但不再用列表形式
- **README 两版引言定位修正（对齐宣言口径）**——重构时误将项目定位写回「云原生无盘计算平台 / cloud-native diskless computing platform」与「A diskless node」，与宣言「iPXE-All-Ready 从来不是一个简单的无盘项目」相悖（无盘只是表象，灵魂是无状态）；已改为「云原生无状态计算平台 / cloud-native stateless computing platform」与「A node」，措辞同步宣言「把无状态贯彻到算力层本身」（8-01 曾对齐过该口径，本次重构回退，已修正）
- **README 徽章行改为动态徽章为主（K8S 风格，中英两版同步）**——原先 7 枚静态技术栈徽章（iPXE / iSCSI / Control Plane / Agent / dnsmasq 等，颜色杂乱且含「iSCSI-Diskless Storage」与宣言相悖文案）删除，保留宣言灵魂标签「Cloud Native-True Cloud Native」置首，其余替换为 4 枚动态/静态徽章：GitHub Stars（shields.io 动态实时）、Release（v0.1.2，自动读取最新 tag）、License（动态读取仓库 Apache-2.0）、Docs（静态链接文档站 ipxe.lecreate.asia，蓝色取项目主色），全部 URL 已验证返回 200

---

## 2026-08-12

### 新增

- **文档站：新增《引导介质制作指南》（`docs/zh/guide/quick-deploy/boot-media.md`）**——快速部署专题第一篇，置顶于「项目环境部署」之上；涵盖 UEFI 直启（direct-uefi 手工 U 盘 / 本地 ESP）、GRUB 引导（grub-bios 的 GRUB2/GRUB Legacy/SYSLINUX 链式加载）、USB 整盘镜像（usb 的 Linux/Windows 写入）与选型指南，介绍 ipxe-stateless 固件六类构建产物中三类本地引导载体；叙述语气正式化（部署障碍、人工介入成本、Secure Boot 限制等表述）；英文侧边栏同步加「Boot Media」占位条目并创建占位页（翻译待后续补齐）；文档站默认入口（导航与首页 hero 按钮）仍指向「项目环境部署」

### 变更

- **原理探索系列：标题去「全流程」化 + 早期探索声明**——「Windows 11 24H2 无盘系统全流程实战」改为「Windows 11 无盘启动技术攻坚」、「Debian 12 无盘系统全流程实战」改为「Debian 12 无盘启动技术攻坚」（正文 H1 与中英侧边栏同步）；第一～三章（中文 3 篇 + 英文 3 篇）在文首统一加入早期探索声明，说明文章为项目早期探索记录、所述方案与当前架构存在差异、仅供底层研究参考；英文版占位页（`docs/guide/debian-12.md`）标题同步改为 Technical Breakthrough 并更新指向中文版的链接文案
- **第三章（Debian 12 无盘启动技术攻坚）补写路线三并正式收尾**——新增 3.4 节「debootstrap 构建纯净骨架（思路）」：仅讲解工程定位与核心思路（最小化装配、底层依赖注入、UUID 寻址、自动化友好），不再提供逐步操作命令，实操引导至快速部署系列《Debian 系无盘快速部署（母盘克隆）》；文末「未完待续…」替换为「本章小结：从 no way 到三条路线跑通」，以三路线对比表收束 3.1 方法论，并抛出 per-worker initramfs 注入痛点以衔接第四章 iBFT 主题；英文占位页结构同步为三路线并注明 3.4 仅思路级覆盖
- **英文文档目录与中文对齐：原理探索系列迁入 `exploration/` 子目录**——英文 6 篇（Ch1–Ch4、控制面、壁垒）经 `git mv` 从 `docs/guide/` 平铺结构迁入 `docs/guide/exploration/`（保留 Git 历史），与中文 `docs/zh/guide/exploration/` 完全对称；前言仍留 `docs/guide/preface.md`；侧边栏链接同步更新；旧路径不保留重定向（直接 404），标题风格维持英文 Ch1:/Ch2: 紧凑前缀不变
- **文档站：清理 VitePress 脚手架示例页**——删除 `docs/api-examples.md`（Runtime API Examples）与 `docs/markdown-examples.md`（Markdown Extension Examples），两者为脚手架自带示例、全仓库无任何引用，文档站根目录仅保留 `index.md`
- **早期探索声明范围收窄**——从第四章（iBFT 母盘克隆）、控制面能力详解、我们已经攻克的壁垒中英 6 篇文首移除「早期探索声明」：三者描述当前架构能力（iBFT 方案、控制面设计、已攻克壁垒），不属于早期探索记录；前言声明同步移除；声明仅保留于第一～三章（中英各 3 篇）
- **控制面能力详解（中英）内容核对更新**——补齐 8-03 以来全部能力：MAC 绑定修改（`PUT /workers/{id}/mac` + `worker.mac.update` 审计历史）、单系统盘独立删除（`DELETE /workers/{id}/luns/disk/{os}`，delete_file / ignore_missing_target）、批量部署（`/workers/luns/disk/batch` 自动设默认启动 + `/workers/delete/batch` 统一 reload，WebUI 勾选 / 拖拽 / 均摊 / 接管）、自动注册运行时开关（`GET/PUT /settings/auto-register` + `state/settings.json`，环境变量降级为启动默认值）、Agent 注册 / 探测 / 在线编辑（probe 两步推导、enabled 停用）、母盘清单（`GET /masters` 聚合 + Agent 30 秒扫描缓存 + WebUI 下拉选母盘）、ZFS 克隆细节（同数据集 reflink、跨数据集 / 版本过低 / xfs/ext4 回退全量拷贝、`fs_type` 上报）、建盘按 `role.disk` + `enabled` 调度；设计原则补 `state/settings.json`；WebUI 各条目同步（批量模式、MAC 编辑、单盘删除、Agent 两步注册编辑、自动注册开关、母盘下拉）
- **壁垒文章移出文档站，转为 GitHub 仓库展示**——《我们已经攻克的壁垒 / Barriers We Have Broken Through》经 `git mv` 从 `docs/{zh/}guide/exploration/` 移至仓库根目录（`Barriers_zh-CN.md` / `Barriers.md`，保留 Git 历史）；中英侧边栏条目移除，文档站内不再生成与引用该页面；README 两版官方文档列表新增根目录文件入口（原 Exploration 条目描述中的 Barriers 字样移除）
- **壁垒文章内容更新（中英同步）**——新增「控制面与基础设施攻坚」分组 6 条（第 10–15 条）：dnsmasq 文件级 bind mount 的 inode 陷阱（rename 原子写换 inode 致 HUP 失效，改截断写保持 inode）、LIO 与 stgt 的 iSCSI root 连接符差异（`iscsi-sep` 按后端投影 + `isset` 守卫）、真实 iPXE 固件 `${mac:hexraw}` 展开为空（改 `${mac}` 后端归一化）、Zero-touch 自动注册静默失效（controller_ip 改 `${next-server}` 零硬编码）、WebUI 白屏 null 解引用（角色计算延后至空态分支后）、确认弹窗被容器 overflow 裁剪（改 fixed 全屏遮罩）；引言同步补控制面基建维度
---

## 2026-08-01

### 新增

- Control Plane：`POST /workers/{worker_id}/luns/disk` —— 给指定 Worker 创建系统盘 LUN（母盘克隆 / 空白盘），端点位于 `/luns/` 命名空间，为数据盘（`/luns/data`）与多系统盘预留
- Control Plane：`PUT /workers/{worker_id}/default-os` —— 设置 Worker 默认启动配置，三个字段可设可清、可组合（详见下方"变更"）
- Control Plane：`GET/POST/DELETE /agents/{agent_id}/luns` 与 `POST /agents/{agent_id}/luns/scan` —— Agent iSCSI LUN/target 直管（列出 / 创建磁盘 / 创建 CD / 删除 / 扫描）
- Agent：`/lun/scan` 端点与扫描镜像目录重建 target 能力
- 配置文件：`iscsi-server/.env.example` 模板（补齐 `IPXE_*` 变量说明）

### 变更

- **创建 Worker 流程重构为两步（存储与身份分离）**：
  - `POST /workers` 只注册身份（hostname + MAC 绑定），不再接受 `disk` 字段；台账 `os` 字段移除，`state=registered`
  - 系统盘须另调 `POST /workers/{worker_id}/luns/disk` 创建，`os` 改为必填，写入 `disk.os` 台账（决定 IQN 后缀与文件名）；创建后 `state` 转 `ready`
  - 建盘 `os` 严格校验 ∈ `{windows, ubuntu, debian, centos, esxi}`（menu.ipxe 操作系统项）
  - Windows ISO 安装光驱仍随 `POST /workers` 的 `windows_iso` 创建，CD IQN 后缀固定为 `windows.iso`
- **默认启动配置模型**：
  - `PUT /workers/{worker_id}/default-os` 支持 `os`（默认系统，须与已挂系统盘一致）、`menu_default`（严格校验 menu.ipxe 主菜单 item ID）、`menu_timeout`（非负整数）；传 `null` 清除对应项
  - `/boot-vars` 推导链：`default_os` > `boot.menu_default` > `exit`；`boot.menu_default` 登记后随时可改（解决 WebUI 无法操作的问题）
  - 操作日志统一为 `worker.boot.set`（changes 明细）
- 时区处理全链路本地化：
  - `control_plane/app/state.py` 与 `iscsi-server/agent/app/main.py`：日志时间戳由 `datetime.now(timezone.utc)` 改为 `datetime.now().astimezone()`（跟随容器 TZ）
  - 两个 Dockerfile 安装 `tzdata` 并设 `ENV TZ=Asia/Shanghai`；`TZ` 环境变量写入 `control_plane.env`、`.env.example`、`iscsi-server/.env`、`.env.example`，docker-compose 不再写死
  - WebUI `Operations.jsx` 与 nginx njs `file-list.js` 改为本地时间展示
- 文档：`Control_Plane_API_Docs.md` 全面同步（两步创建流程、default-os 端点与合法值表、测试顺序、实现边界）

### 修复

- `DELETE /workers/{worker_id}` 在 Worker 无系统盘（`disk=None`）时不再崩溃（原 `_delete_target(record["disk"])` 会 TypeError）
- 日志时间与宿主机不一致问题（根因：代码写死显式 UTC，`/etc/localtime` 挂载对其无效）

---

## 2026-08-02

### 新增

- Control Plane：**/boot-vars 自动注册（Zero-touch Provisioning）**——新 MAC 请求时自动按顺序分配 hostname（`worker-%02d`，扫描台账 + dhcp 绑定最大序号 +1）、写入台账与 dhcp 绑定并 reload，返回 `menu-default=reboot` 短超时循环重启，等待管理员建盘 + 设置 `default_os` 后自动进入系统；新增环境变量 `IPXE_CP_AUTO_REGISTER`（默认 `true`）与 `IPXE_CP_AUTO_BOOT_TIMEOUT`（默认 `1`，单位毫秒）

### 变更

- **/boot-vars 行为调整**：
  - 身份识别改为 hostname 优先（hostname 未命中或未传时退回 MAC 反查）；无系统盘 Worker 不再返回空脚本，`menu_default` 兜底由 `exit` 改为 `reboot`（未配置默认启动时短超时循环重启；`exit` 仅显式设置时返回）
  - 无系统盘时不返回 `base_iqn`/`iscsi_server`（iPXE 沿用 `boot.ipxe.cfg` 静态默认值），`menu_timeout` 在 reboot 循环中固定用 `IPXE_CP_AUTO_BOOT_TIMEOUT`
- **WebUI Agent LUN 直管界面**：
  - Agents 页面卡片可点击，跳转新增 `agents/:id` 页面（`AgentLuns.jsx`）：列出该 Agent 全部 iSCSI LUN（DISK/CD 类型识别、backing、绑定状态），支持直接创建磁盘（母盘克隆 / 空白盘）、创建 CD（ISO）、删除、扫描目录重建 target，不依赖 Worker
  - 删除已绑定 Worker 的 LUN 时，确认框提示绑定关系（“删除后该 Worker 将无法启动”）并可选同时删除 backing 文件；列表中标黄显示“绑定: worker-xx”
- **WebUI 创建 Worker 流程改为两步（与后端对齐）**：
  - Workers 页创建表单只注册身份：worker_id + MAC（必填）+ Windows ISO（可选），不再传已废弃的 `os`/`disk` 字段；hostname 默认取 worker_id（不再单独填写）；列表 OS 列改读 `disk.os`
  - Worker 详情页新增“创建系统盘（第二步）”表单：系统（严格五选：windows/ubuntu/debian/centos/esxi）+ 磁盘类型（空白盘/母盘克隆）+ 大小/母盘名 + 存储节点；无盘 Worker 才显示，创建成功后状态转 `ready` 并刷新详情
- **系统盘模型升级为多盘（`disk` 单字段 → `disks` 数组）**：
  - 一个 Worker 可挂多个系统的系统盘（同一 `os` 至多一个，重复创建返回 `409`）；`POST /workers/{worker_id}/luns/disk` 不再限制单盘，创建表单在 Worker 详情页始终可用
  - 旧台账单盘字段 `disk` 自动迁移并入 `disks`（首次追加新盘时完成），读取全链路兼容
  - `/boot-vars` 选盘：`default_os` 对应的系统盘，未设时取第一块；`GET /workers/{worker_id}/status` 的 `actual.disk` 改为 `actual.disks` 数组（每项含 `os`）；`DELETE /workers/{worker_id}` 删除全部系统盘
  - WebUI：Workers 列表 OS 列显示全部系统（逗号分隔）、详情页展示每块盘卡片（含 os）、Agent LUN 页绑定检测覆盖全部盘
- **WebUI 默认启动配置表单**：Worker 详情页新增“默认启动配置”区块——展示当前 `default_os`/`boot.menu_default`/`boot.menu_timeout`，表单对接 `PUT /workers/{worker_id}/default-os`（os 仅可选已挂载系统盘，menu_default 为 menu.ipxe 主菜单 11 项，均可选“— 清除 —”，menu_timeout 支持“清除超时设置”复选框），保存后刷新台账与 /boot-vars 代码块
- **开发环境**：项目根新增 Python 虚拟环境 `.venv`（安装 control_plane 与 iscsi-server agent 的 requirements），`.gitignore` 增加 `.venv/`、`venv/`、`__pycache__/`、`*.py[cod]`
- **文档站首页定制（中英双语）**：新增 `docs/.vitepress/theme/`（`index.js` 引入 `custom.css`）——整体黑白极简配色（浅色模式近黑 `#18181b`、深色模式近白 `#f4f4f5`，按钮文字反色适配），hero 大字等宽字体纯黑白（无渐变无动画），hero 背景仅留极淡网格，副标语中性灰；**首页内容由 6 个 feature 卡片改为朴素列表**（无圆点、细分隔线），中英文内容对齐当前版本（零接触自动注册 / 一机多系统 / 中心控制面 + Web UI / 秒级启动 / 拒绝黑盒 / 100% 纯开源工具链）
- **文档：新增《Windows 无盘快速部署（母盘克隆）》快速部署栏目**——`docs/zh/guide/windows-quick-deploy.md` 全流程（Controller 双编排部署 → 存储节点 Agent 部署 → 母盘制备/上传 → WebUI 秒级克隆 → 默认启动），文档站侧边栏新增「快速部署」分组；定位与环境部署文档与原理向实战文档区分
- **文档：文档站重构为「原理探索」+「快速部署专题」双专栏**——侧边栏新增「原理探索」分组（前言与第一～三章归入，记录 iPXE 无盘技术原理）、原「快速部署」分组更名「快速部署专题」，导航「实战指南」更名「原理探索」；新增《第四章：Debian 系 iBFT 无盘启动——母盘克隆的优雅解法》（`docs/zh/guide/debian-12-ibft.md`）：iBFT 六环链路（sanboot 写表 → ISCSI_IBFT_FIND 发现 → iscsi_ibft 导出 sysfs → initramfs ISCSI_AUTO → iscsistart -b 登录 → root=UUID 挂根）、内核配置与 open-iscsi 源码证据（hook 只拷 iscsistart/initiatorname.iscsi/iscsi.initramfs、node.startup 与 iBFT 无关）、母盘四步构建配方与 initrd 三件套验证、0x7f22208e 踩坑（固件可移动介质契约需 ESP 的 BOOTX64.EFI）、Debian 系通用性论证
- **文档：《Windows 无盘快速部署》新增 4.3 节「真实硬件制备母盘（备选路径）」**——目标硬件含专有驱动（特殊网卡 / RAID / HBA）时可在同型号真实机器上安装一次即得母盘；盘转换三种方式（disk2vhd + qemu-img / 拔盘 dd / Live 环境 dd）；驱动真实匹配、克隆零驱动问题，命名与克隆契约与虚拟机母盘完全一致
- **文档：新增《Debian 无盘快速部署（母盘克隆）》快速部署专题第二篇**——`docs/zh/guide/debian-quick-deploy.md` 全流程（环境准备与 Windows 篇共用清单 → 母盘制备（UEFI+GPT 前提 / 虚拟机与真实硬件双路径 / 四步配方 / BOOTX64.EFI / initrd 三件套验证 / 转换命名）→ 上传 → 自动注册 → WebUI 克隆（IQN `worker-xx.debian`）→ 默认启动 → 验收 iBFT）；FAQ 覆盖 0x7f22208e、VFS 根挂载失败、root=UUID 疑虑；侧边栏「快速部署专题」追加条目
- **文档：第四章《Debian 系 iBFT 无盘启动》新增 4.5 节「真实硬件制备（备选路径）」**——真实硬件安装后应用四步配方即得母盘，dd 全盘转换，契约与虚拟机母盘一致；原 4.5/4.6 顺延为 4.6/4.7
- **文档：中文文档站目录按专题分文件夹**——`docs/zh/guide/` 下新建 `exploration/`（原理探索：第一～四章）与 `quick-deploy/`（快速部署专题：Windows/Debian 两篇），前言保留根目录；`docs/.vitepress/config.mts` 侧边栏与 `docs/zh/index.md` 首页链接全部更新；README.zh-CN.md / README.md 文档站链接同步更新（zh README 同时收录第四章与快速部署两篇）
- **文档：环境部署独立成篇，快速部署两篇收窄为母盘专题**——新增《项目环境部署》（`docs/zh/guide/quick-deploy/environment-deploy.md`）：部署拓扑与 Controller / 存储节点部署（原 Windows 篇第 1~3 步）平台无关化，附部署核对清单与两篇母盘入口；Windows 篇删除环境部署部分、步骤重编号（原第 4~9 步 → 第 1~6 步，4.1~4.3 → 1.1~1.3）；Debian 篇环境准备清单改为指向新文；侧边栏「快速部署专题」置顶新篇，README.zh-CN.md 快速部署列表同步
- **文档：《Debian 无盘快速部署》更名《Debian 系无盘快速部署（母盘克隆）》**——正文同步覆盖 Debian 系发行版：1.1 安装小节与 1.2 真实硬件路径补充 Ubuntu 20.04/22.04/24.04，1.4 补充 Ubuntu 安装器源路径 `\EFI\ubuntu\`，1.6 命名表新增 Ubuntu 示例（`_tpl_ubuntu_24.04.img`，克隆时 OS 选 `Ubuntu`、IQN 后缀 `.ubuntu`）；侧边栏、README.zh-CN.md、环境部署篇引用同步（URL 保持不变）
- **文档：《Debian 系无盘快速部署》新增「支持范围」小节**——基于官方包库与内核配置查证：open-iscsi 在 Debian 11~13 与 Ubuntu 22.04/24.04/26.04 全系存在且自带完整 initramfs 集成（hooks/iscsi + local-top/iscsi，支持 `iscsi_auto`）；内核 `CONFIG_ISCSI_IBFT=m` 强制开启 `CONFIG_ISCSI_IBFT_FIND=y`（kernelconfig.io）；Ubuntu 的 `iscsi_ibft`/`iscsi_tcp`/`ib_iser` 模块位于基础 `linux-modules` 包（noble `generic.inclusion-list` 证实，最小安装自带，无需 linux-modules-extra）；矩阵标注 Debian 12 已实测、其余为同链路机制支持，并排除已 EOL 的 Debian 10 / Ubuntu 20.04
- **文档：英文文档站结构与中文站对齐**——侧边栏改为双专栏（Exploration：Foreword + Ch1~Ch4；Quick Deploy：Environment Setup + Windows/Debian-family Master Image），导航新增「Quick Deploy」；新增 5 个英文占位页（Ch3 `docs/guide/debian-12.md`、Ch4 `docs/guide/debian-12-ibft.md`、快速部署 3 篇 `docs/guide/quick-deploy/*.md`），每页含内容骨架与中文版链接（标注 translation in progress）；英文首页 hero 主按钮改为 Quick Deploy（与中文对齐）；README.md 文档章节补全为双专栏 7 条并改用英文站链接
- **文档：README.zh-CN.md 重构 + 路线图独立成文件**——新增 `ROADMAP.md`（路线图 Phase 1~4 + 近期规划，其中 CLI/reconcile/一键部署脚本从 README「控制面推进中」迁入）；README.zh-CN.md 重组为清晰分层（简介 → 架构总览（角色 + 设计原则 + 流量，合并原「项目总览」与「架构定义」）→ 核心能力 → 控制面能力详解（原「当前进展」拆出，消除与核心能力重复）→ 快速开始 → 官方文档 → 项目结构（补 ROADMAP.md）→ 攻坚记录 → 路线图（改为链接）→ 参与贡献 → License → 成长轨迹）；英文 README.md 保留其英文版 Roadmap 不动

### 修复

- WebUI 错误提示显示 `[object Object]`：FastAPI 422 的 `detail` 是校验错误数组，`api/client.js` 现在逐条拼接为 `字段: 错误信息` 文本
- **Agent LUN 直管页面按后端角色禁用创建按钮**：LIO 后端（`role.cd: false`）的“创建光驱 (ISO)”按钮置灰不可点，并显示提示“LIO 后端不支持 ISO 光驱”（hover 亦有 title）；同理 `role.disk: false` 时禁用“创建磁盘”；表单渲染双保险（无 role 配置的旧数据默认放行）；后端 `POST /agents/{id}/luns/cd` 与 `/luns/disk` 同步新增角色校验（400 拒绝，不再透传到 Agent）
- **Agent LUN 页白屏修复**：上一版将 `agent.role` 角色计算放在组件首次渲染（`agent` 为 null）即执行，导致 `TypeError: Cannot read properties of null (reading 'role')`，React 整树卸载白屏（API 请求都来不及发出）；现改为在 `if (!agent) return` 空态分支之后计算，并顺带修复 `Button` 组件不透传 `title` 等剩余属性（hover 提示此前未生效）
- **Zero-touch 自动注册不生效（worker 拿不到 hostname、不重启）**：根因是 `tftp/boot.ipxe.cfg` 的 `set controller_ip 192.168.1.5` 仍是模板默认值，与实际网段（192.168.80.x）不符——iPXE 请求 `http://${controller_ip}:4839/boot-vars` 不可达后 `|| goto vars-done` 静默跳过，后端从未收到请求（台账/dhcp 绑定始终为空），`menu-default` 由 menu.ipxe 兜底为 `exit`、`menu-timeout` 兜底为 0，菜单不自动选择也不重启；已改为 `set controller_ip ${next-server}`（同机部署下 next-server 即 DHCP 服务器 IP，与 dnsmasq 网段、agents.yml `iscsi_server` 一致，零硬编码，换 IP 无需改脚本），并清理了排查期间用假 MAC 触发自动注册产生的测试 worker（worker-00 及 dhcp 绑定，需重启 dnsmasq 容器彻底清除内存态）
  - **mac 传参由 `${mac:hexraw}` 改为 `${mac}`**：真实 iPXE 设备实测 `${mac:hexraw}` 修饰符展开异常（mac 参数为空导致后端不识别），带冒号格式 `${mac}` 一切正常；后端 `_normalize_boot_mac` 会剥离冒号/横线/点号归一化，两种格式均可识别
  - **自动注册编号从 `worker-01` 开始**：`_next_auto_hostname` 初始序号由 -1 改为 0（原逻辑第一个分配 `worker-00`），并将已注册的 worker-00 无缝改名为 worker-01（台账 + dhcp 绑定已同步，重启 dnsmasq 生效）
- **README.zh-CN.md 全面更新**：项目结构对齐当前代码（移除已删除的 `iscsi-target-gen.sh`，补齐 `CHANGELOG.md`、`assets/`、`iscsi-server/.env`、`webui/deploy/nginx/njs`、`docs/.vitepress` 等）；新增“核心能力”章节（零接触注册 / 一机多系统 / 中心控制面 + Web UI / 秒级启动 / 拒绝黑盒 / 纯开源）；功能介绍对齐当前实现（Zero-touch 自动注册、两步创建 + 多系统盘、默认启动配置、Agent LUN 直管与角色模型、WebUI 两步流程与 LUN 直管页）；Roadmap 勾选已完成的中心控制面与 Controller 容器化；文档章节补充第三章 Debian 12；移除表情符号；License 链接修正
- **Control_Plane_API_Docs.md 同步更新**：`/boot-vars` 的 iPXE 接入代码块与推荐传参改为 `${mac}`（注明 hexraw 修饰符在部分固件展开异常、chain 失败静默兜底与派生变量重建）；自动注册编号示例改为 `worker-01` 起；`IPXE_CP_AUTO_BOOT_TIMEOUT` 单位明确为毫秒；示例 IP 由模板值 192.168.1.5 改为实际网段 192.168.80.3；Agent LUN 直管补充 `role.disk`/`role.cd` 角色校验（400）；实现边界将多系统盘移入已支持列表

---

## 2026-08-03

### 新增

- Control Plane：`DELETE /workers/{worker_id}/luns/disk/{os}` —— 删除 Worker 的单个系统盘（iSCSI target）：`delete_file` 参数控制是否同时删除 backing `.img` 文件（`false` 仅删 target、文件保留可重新挂载），`ignore_missing_target` 在 Agent 侧 target 已不存在时忽略 404 继续完成台账清理；操作日志新增 `worker.disk.delete`（started/succeeded/failed）
- WebUI：Worker 详情页「系统盘」每张磁盘卡片新增**删除系统盘**按钮——ConfirmAction 确认弹窗（可勾选「同时删除 .img 磁盘文件」与「忽略已不存在的 Target」），删除中按钮禁用并显示「删除中...」，成功后自动刷新台账与启动变量
- Control Plane：`/boot-vars` 新增 `iscsi_sep` 字段——iSCSI root **连接符**（`${iscsi-server}` 与 `${base-iqn}` 之间的分隔字段），**按系统盘所在 Agent 的后端类型生成**：stgt 后端为 `:::1:`（lun 占位 1），LIO 后端为 `::::`（空占位，解决 LIO 后端 iSCSI 连接参数不兼容问题）；只投影差异连接符本身，root-path 拼装（`iscsi:${iscsi-server}${iscsi-sep}${base-iqn}:${hostname}.<os>`）由 iPXE 侧静态完成；后端类型优先读 `agents.yml` 该 Agent 的 `tags`（含 `lio`/`stgt` 标记，离线零成本），未标记时查询 Agent `/capabilities` 的 `backend` 字段（Agent 自报），查询失败默认 stgt 格式兼容

### 变更

- **文档：README 全面入口式重构（对齐 Docker/K8S 风格），控制面详解与攻坚记录迁入文档站**——README.md / README.zh-CN.md 精简为入口式结构（定位 → 架构三角色 → 核心能力 6 条 → 快速开始（clone + compose up + 端口）→ 官方文档链接 → 路线图（指向 ROADMAP.md）→ 参与贡献 → License → Star History），删除原「控制面能力详解」「我们已经攻克的壁垒」「详细项目结构」全文；文档站新增《控制面能力详解》（中英：设计原则 + 核心能力 8 项 + WebUI 能力 + 文件浏览器）与《我们已经攻克的壁垒》（中英：Linux 引导链 / Ubuntu / Windows 分组 9 条），VitePress 侧边栏注册（中英 Exploration 分组）；参与贡献章节新增 AI 辅助开发态度声明（不反对 AI 生成代码——项目本身由 Qwen/Codex/DeepSeek 协同完成，但贡献者必须自行理解整体架构：控制面/数据面分离、iPXE 引导链、动态变量传递链、文件即真相、iSCSI 会话保活；无法清晰阐述设计逻辑的 PR 拒绝合并，鼓励先提 Issue/Idea）
- **文档：`Control_Plane_API_Docs.md` 同步系统盘删除接口**——接口概览表新增 `DELETE /workers/{worker_id}/luns/disk/{os}` 条目，新增 7.4 章节（参数说明 + 保留 .img / 同时删除 .img 两个 curl 示例）
- **iPXE 脚本使用 `${iscsi-sep}` 变量**：`menu.ipxe` 全部系统项与安装项的 root-path 改为 `iscsi:${iscsi-server}${iscsi-sep}${base-iqn}:${hostname}.<os>`（原 `base-iscsi` 移除）；`boot.ipxe.cfg` 兜底值改为 `set iscsi-sep :::1:` + chain 后 `isset ${iscsi-sep} || ...` 守卫（不覆盖 `/boot-vars` 已下发的 LIO 格式）；WebUI `buildBootVarsCode` 展示 `set iscsi-sep`；API 文档 5 章节（字段来源表 + iPXE/JSON 示例 + 接入方式）与文档站控制面详解/Windows/Debian 文档同步更新

---

## 2026-08-03

### 新增

- Control Plane：`POST /workers/luns/disk/batch` — 批量给多个 Worker 创建系统盘（母盘克隆 / 空白盘），请求体 `targets` 逐项指定 `{worker_id, agent}` 存储节点分配（须已分配，不存在默认公共分配）；与单盘一致同一 `os` 至多一块、已存在自动跳过（不算失败）；逐项独立执行，单项失败不影响其余，返回 `succeeded` / `skipped` / `failed` 汇总；**创建成功的 Worker 自动将 `default_os` 设为本次批量系统**（批量部署直接进入默认启动，无需再调 `PUT /workers/{id}/default-os`；单盘接口不自动设置，审计记录 `worker.boot.set`）
- WebUI Workers 页新增「批量创建系统盘」模式：
  - 仅批量模式下每行出现勾选框，已拥有系统盘的 Worker 行标浅黄提醒（可正常勾选，重复 `os` 由后端自动跳过）；点击勾选单个，Shift+点击标定终点、中间自动勾选（范围选择基于当前筛选结果顺序）
  - 左侧常驻侧边栏（悬浮于视口左侧、不挤占原列表宽度）：已选 Worker 计数 + 批量系统盘参数（系统 / 空白盘或母盘克隆 / 大小或母盘名，**不含存储节点**）+「开始批量创建」按钮与结果汇总（成功 / 跳过 / 失败明细）
  - 右侧常驻侧边栏（悬浮于视口右侧、不挤占原列表宽度）：列出全部存储节点（role=disk），每个节点一个可拖拽标签框，内含「参与均摊」勾选 +「接管所选 Worker」按钮（已选 Worker 统一改派给该节点，覆盖之前单独指定）与已分配计数；节点列表底部新增「均摊分配所选 Worker」——勾选 ≥2 个节点后，已选 Worker 按参与节点轮流平均分配（覆盖之前分配）；拖动节点标签放到某行 = 该 Worker 单独指定该存储节点；行的「存储节点」列展示分配结果并可单独取消分配

### 变更

- 文档：`Control_Plane_API_Docs.md` 接口概览表新增批量创建条目，新增 7.1.3 章节（请求体字段表 + curl + 返回示例）

---

## 2026-08-03

### 新增

- **首个发行版（v0.1.0）发布准备 — 环境配置与注释收尾**：
  - `iscsi-server/.env` 与 `control_plane/config/agents.yml`（含真实部署 token）解除 git 跟踪并加入 `.gitignore`，仓库只保留 `*.example` 模板
  - 新增 `webui/app/.env.example`（VITE_CP_TOKEN 构建期变量说明）
  - `control_plane.env(.example)` / `iscsi-server/.env(.example)` 补齐分组注释（文件路径、dnsmasq 联动、启动行为、Token 同步说明）
  - `control_plane/config/agents.yml.example` 补齐字段注释（base_url / iscsi_server / token 占位 / role / tags / enabled）
  - 根 `docker-compose.yml`：各服务补齐职责注释，移除无人引用的误导性 `networks.ipxe` 段；`iscsi-server/docker-compose.yml` 补齐后端与 Agent 注释、清理行尾空格
  - `iscsi-server/agent/Dockerfile` 移除开发期对话遗留注释，改为规范说明
  - README（中英）快速开始补全配置步骤：`cp *.env.example` 准备流程、API 鉴权 Token 同步、存储节点独立部署指引
  - 文档收敛：删除冗余的 `iscsi-server/Agent_API_Docs.md`，保留更全面的 `API_Reference.md`（补入 Token 常量时间比对细节）
- Control Plane：`POST /workers/delete/batch` — 批量删除 Worker（请求体 `worker_ids` + `delete_disk` / `ignore_missing_target`）：每项独立执行（单项失败不影响其余，不存在的计入 failed），返回 `succeeded`/`failed` 汇总；成功项统一保存台账、统一 reload 一次 dnsmasq（优于逐删逐 reload）；审计逐项 `delete_worker`
- WebUI Workers 页新增独立「批量删除 Worker」模式（与批量创建互斥）：工具栏独立按钮进入/退出，勾选后左侧栏确认删除（含「同时删除 .img」/「忽略缺失 target」选项，与详情页一致）→ 结果汇总展示，成功后清空勾选并刷新
- Control Plane：`POST /agents/probe` — 探测 Agent 并自动推导注册参数（预览，不写文件）：调 `/healthz` + `/capabilities`，推导 `role`（disk 恒真 + cd 取 capabilities）/ `tags`（storage + backend）/ `iscsi_server`（回退 base_url 主机名），返回 backend / base_iqn / clone 等能力供确认；Agent 不可达或 token 错误返回 502，审计记录 `agent.probe`
- Control Plane：`POST /agents` — 注册新 Agent 写入 `agents.yml`，注册后立即生效；请求体含 `id` / `base_url`（须 http(s)://） / `token`（支持 `${ENV}` 占位）/ `iscsi_server` / `role`（disk/cd）/ `tags` / `enabled`；重复 id 返回 409，审计记录 `agent.register`；`AgentRegistry` 新增 `add()`（yaml 写回保持 `agents:` 顶层结构）
- WebUI Agents 页新增「+ 添加 Agent」入口（两步流程）：填 Agent ID / API 地址 / Token 点「探测」→ 后端自动获取后端类型 / 角色 / 标签 / 数据面地址等参数并在预览区展示（可修改，含只读能力标签）→ 点「添加」完成注册并刷新列表；地址变更后旧探测结果自动失效

### 变更

- 文档：《项目环境部署》1.3 节固件获取方式更新——不再下载解压 `tftp.zip`，改为从 [boot.ipxe.org](https://boot.ipxe.org/) 直接下载官方 release 固件（`undionly.kpxe`，以及 `x86_64-efi/` 下的 `ipxe-legacy.efi` / `ipxe.efi` / `snponly.efi`），全部统一放入 `tftp/` 根目录（不保留官网 `x86_64-efi/` 子目录，`wget` 默认只取 URL 末尾文件名）；`ipxe.efi` / `ipxe-legacy.efi` 为 UEFI 引导异常时的备选固件（改 `dnsmasq.conf` 的 efi64 引导文件）；补充 memdisk 说明——取自 SYSLINUX 发行包 `bios/memdisk/memdisk`，仅「iPXE 直接引导 ISO」的旧方式需要，常规无盘启动（iSCSI sanboot）不需要；中英文档同步

---

## 2026-08-04

### 新增

- Control Plane：`PUT /agents/{agent_id}` — 更新已有 Agent 配置（id 不可改，走路径参数）：`base_url` / `token` / `iscsi_server` / `role` / `tags` / `enabled` 全量覆盖写回 agents.yml，保存后立即生效；`token` 传空字符串 = 保持原值（API 不回显 token，前端无法回填）；`enabled=false` 停用（不再参与建盘/挂载调度与存活探测）；不存在返回 404，审计记录 `agent.update`；`AgentRegistry` 新增 `update()`（原 token 保留）
- Control Plane：`POST /agents/probe` 新增可选 `agent_id` 参数 — 编辑场景 token 留空时自动沿用注册表中该 Agent 的 token 探测（未知 id 忽略）
- WebUI Agents 页每张卡片右上角新增「编辑」按钮 — 点击后在列表上方弹出遮罩弹层（fixed 覆盖，不挤占原有布局；点遮罩空白处或「取消」关闭），编辑表单复用添加的两步探测流程：id 只读展示（走路径参数）、Token 留空保持不变（placeholder 提示，探测沿用注册表原值）、新增「启用（参与调度）」复选框，探测成功后方可保存，保存后刷新列表；停用的 Agent 卡片显示「停用」徽章
- Agent：`GET /masters` — 列出存储节点 `DISK_DIR` 下 `*_tpl_*` 母盘（新增 `MasterScanner` 后台 daemon 线程，每 30 秒周期扫描并带锁缓存 `{name, size, mtime}`，识别文件名含 `_tpl_` 标记的镜像；纯读接口，Bearer 鉴权，不写操作日志）
- Control Plane：`GET /masters` — 聚合列出全部启用磁盘角色 Agent 的母盘清单（遍历 `agents.yml` 中 `enabled` + `role.disk` 节点，逐台调用 Agent `list_masters()`；单台失败返回 `error` 字段并记审计 `master.list`（failed）不阻塞整体，全部失败 502 / 部分成功 200 / 无候选空列表）；`AgentClient` 新增 `list_masters()`
- WebUI：母盘克隆下拉选择——Workers 批量创建与 Worker 详情页「创建系统盘」的母盘名由手工输入改为下拉选择（数据来自 Control Plane 聚合的母盘清单）：批量模式下拉为母盘名去重选项（不绑定存储节点，选择后不自动接管，节点分配由均摊 / 接管 / 拖拽侧边栏决定），支持多存储节点均摊克隆；提交时校验目标节点本地均有该母盘——均摊激活（≥2 个节点参与均摊）时校验全部参与均摊节点，否则校验实际分配节点，缺失时列出缺失节点并阻止提交（可移除缺失节点的「参与均摊」勾选，或先在对应节点上传该母盘后再提交），克隆在各节点本地完成；详情页按所选存储节点过滤母盘、切换节点自动清空已选

### 变更

- 文档：`iscsi-server/API_Reference.md` 与 `control_plane/Control_Plane_API_Docs.md` 同步母盘清单接口——接口总览表新增 `GET /masters` 条目并新增独立章节（响应结构 `{agents: [{agent, iscsi_server, masters, error?}]}`、字段说明、失败容错语义）；Agent 侧 `API_Reference.md` 新增 `## 12. GET /masters（母盘清单）`，原编号顺延
- 文档：《项目环境部署》第 2 步新增 2.1「准备 img 存储目录」小节（原 2.1–2.5 顺延为 2.2–2.6）：明确 `iscsi-server/docker-compose.yml` 中 `- /pool1/iscsi_img:/home/iscsi_img` 卷映射须将宿主机侧路径改为存储节点实际存放 img 文件的目录（`ipxe-iscsi` 与 `ipxe-agent` 两处一致，容器内 `/home/iscsi_img` 不变）；存储目录文件系统强烈建议 btrfs（母盘克隆走 reflink/FICLONE 秒级完成，ext4/xfs 等不支持时回退全量拷贝，克隆时间随母盘大小线性增长）；新增单台 iSCSI 服务器硬件瓶颈表（网卡速率 / 硬盘 IO / 内存 CPU）与按并发 Worker 规模扩容存储节点建议（10GbE 约支撑 10–20 个并发 Worker）；中英文档同步
- 文档：Windows / Debian 系无盘快速部署「WebUI 秒级克隆」步骤的母盘名改为下拉选择说明——母盘列表由 WebUI 自动扫描存储节点生成（数据来自 Control Plane 聚合的 `GET /masters` 母盘清单，文件名须含 `_tpl_` 标记），无需手工输入；中英文档同步
- 文档：文档站中英文首页「核心能力」更新——以 README 六条为基础结构：原「秒级启动」改写为「母盘克隆秒级交付」（btrfs reflink 秒级克隆 + 支持矩阵同步为 Debian 11/12/13、Ubuntu 22.04/24.04/26.04、Windows 11 23H2/24H2/25H2），新增「批量部署」「Agent 直管与在线编辑」两条，原「中心控制面 + Web UI」并入后者；补齐「文件即真相」；中英文同步
- 文档：Windows / Debian 系无盘快速部署「第 5 步:设置默认启动」由可选步骤改为常规流程——设置默认系统后开机自动直达系统（无需在 iPXE 菜单手动选择）；仅需配置「默认系统(OS)」一个字段，下拉选项来自该 Worker 已挂载的系统盘（即刚克隆出的盘）；「默认菜单项(Menu Default)」保持默认（重启）不动——推导链 `default_os > boot.menu_default > reboot` 中 `default_os` 优先命中，未配置的菜单项维持重启兜底；中英文档同步
- 文档：Debian 系无盘快速部署「支持范围」注明桌面/服务器版本无差别——Ubuntu 不区分 Desktop / Server 版本，桌面环境（GNOME / KDE / XFCE 等）任意选择，不影响无盘启动；Debian 同理，按常规方式正常安装的系统均支持，无需担心桌面环境影响；1.1 安装步骤同步补充说明；中英文档同步
- 文档：IQN 契约表述修正——`tftp/boot.ipxe.cfg` 的 `base-iqn` 仅为静态兜底值（占位符），Worker 启动时 iPXE 经 `/boot-vars` 按系统盘所在存储节点获取实际 `base-iqn`（盘 IQN 前缀，源自该节点 `IPXE_IQN_BASE`）并覆盖；各存储节点 `IPXE_IQN_BASE` 对自身承载的盘是权威值，无需与 `boot.ipxe.cfg` 静态值一致；《项目环境部署》2.3 与快速部署「环境准备」/FAQ 同步修正；中英文档同步
- 文档：IQN 机制表述复核修正——架构文档（中英）1.5 节第 2 步补充 `base-iqn` 静态兜底 + `/boot-vars` 按系统盘所在存储节点动态覆盖机制（原按纯静态配置推演，缺覆盖环节），第 3 步 root-path 拼装改为与 `menu.ipxe` 一致的 `iscsi:${iscsi-server}${iscsi-sep}${base-iqn}:${hostname}.<os>` 变量格式（原硬编码 `::::`）；Control Plane 文档（中英）`/boot-vars` 返回变量列表补入 `base-iqn`；中英文档同步

---

## 2026-08-05

### 变更

- 文档：README 两版与文档站中英文首页 tagline、ROADMAP 的定位表述对齐《我的云原生定义》宣言——由「企业级无状态（Stateless）计算节点交付平台 / Enterprise-grade diskless computing platform / 云原生的无状态计算基础设施底座」统一改为「真正的云原生实现：把无状态贯彻到算力层本身，算力不绑定任何具体硬件，可丢弃、可替换、可瞬间重建」；路线图愿景改为「贯穿所有计算层的云原生元协议——同一套无状态语义自相似地嵌套于物理机与 hypervisor 每一层，层层皆云」；中英文同步
- 文档：新增仓库根目录 `Manifesto_zh-CN.md`（《我的云原生定义》宣言，由《我的云原生定义.md》重命名，移除文首对话残留）与英文全量翻译版 `Manifesto.md`（九章完整翻译，与中文版互为镜像）；README 中英文版定位段后新增宣言引用

---

## 2026-08-07

### 新增

- Agent：母盘克隆新增 ZFS 支持——存储目录位于 ZFS（OpenZFS ≥ 2.2）且母盘与克隆盘在同一数据集时，`FICLONE` 文件级 reflink 秒级克隆（与 btrfs 同路径，零额外磁盘占用）；ZFS < 2.2 或跨数据集（`st_dev` 不同）时自动回退全量拷贝，并在日志中给出明确诊断（区分「版本过低」与「跨数据集」两类原因）；新增 `_fs_type()`（解析 `/proc/self/mounts` 最长挂载点匹配）与 `_same_fs()`（`st_dev` 比较）
- Agent：`GET /capabilities` 新增 `fs_type` 字段（存储目录文件系统类型：btrfs / zfs / xfs / ext4 ...），`clone` 描述按文件系统类型区分（ZFS 标注 OpenZFS ≥ 2.2 与同数据集约束；xfs 标注需 reflink 特性；其余标注仅全量拷贝）；控制面 `GET /agents` 随 `capabilities` 透传
- Control Plane：`GET/PUT /settings/auto-register` —— 全局自动注册开关运行时切换：环境变量 `IPXE_CP_AUTO_REGISTER` 降级为**启动默认值**，运行时状态持久化到 `state/settings.json`（重启保留、优先于环境变量、立即生效）；关闭后新 MAC 不再自动注册（已注册 Worker 不受影响）；切换写入操作日志（`settings.auto_register`）
- WebUI：Workers 页面工具栏新增「自动注册」开关按钮（状态点指示开/关、点击即切换、加载/切换失败显示错误信息）；i18n 中英文案同步
- 文档：控制面 API 参考中英两版新增 5.1 章节 `GET/PUT /settings/auto-register`（含两种配置方式对比表：环境变量 vs 运行时 API），第 3 节端点概览与第 5 节配置表同步更新；《项目环境部署》中英两版新增 1.4.1「自动注册开关」小节（部署时环境变量固定 vs 部署后 WebUI/API 运行时切换，含手动注册提醒）
- 文档：新增仓库根目录 `AI_POLICY_zh-CN.md` / `AI_POLICY.md`（《对 AI 辅助的态度》中英双语立场声明——「总设计师与施工队」边界、语法归 AI / 架构归人脑、PR #3 自动化扫描器误报真实案例记录）；README 两版「参与贡献 · 关于 AI 辅助」段落末尾引用（中文完整引用；英文正式引用，含中文原文互指）

### 变更

- 文档：《项目环境部署》2.1「准备 img 存储目录」存储文件系统建议由「强烈建议 btrfs」扩展为「btrfs 或 ZFS（OpenZFS ≥ 2.2）」，补充 ZFS 文件级 reflink 的同一数据集约束与 ZFS < 2.2 / 跨数据集回退语义；文档站中英文首页「母盘克隆秒级交付」与控制面文档（中英）的 btrfs 表述同步扩展；`iscsi-server/API_Reference.md` `/capabilities` 章节同步 `fs_type` / `clone` 字段说明；中英文档同步
- WebUI：Agents 页面 Agent 卡片新增「文件系统」展示（`capabilities.fs_type`，等宽字体），注册/编辑探测结果新增 `fs_type` 标签；Control Plane `POST /agents/probe` 返回新增 `fs_type` 字段（透传 Agent `/capabilities`）；`Control_Plane_API_Docs.md` 两处示例同步
- 文档：API 文档迁入文档站——`control_plane/Control_Plane_API_Docs.md` 与 `iscsi-server/API_Reference.md` 移至 `docs/zh/guide/api/`（`control-plane-api.md` / `agent-api.md`，标题中文化），文档站新增「API 参考 / API Reference」栏目（中英侧边栏 + 导航栏），英文版为占位页（结构骨架 + 指向中文权威版，不全量翻译）；原文件删除，文档站成为唯一权威；README 两版官方文档列表新增 API 参考链接，快速开始端口区强调 Control Plane API 为开放 REST 接口、第三方系统与自动化脚本可直接调用
- 文档：两份 API 文档与 README 两版强调 **API 优先（API-first）调用准则**——控制面全部能力以 REST API 为第一接口，WebUI 本身只是该 API 的一个客户端；第三方系统与自动化脚本与 WebUI 平等，一律优先调用 Control Plane API（Agent API 为控制面与存储节点间的内部契约，不作为第三方入口）；英文占位页同步
- 文档：README 两版官方文档列表重构——原理探索系列（第一～四章 + 控制面能力详解 + 已攻克的壁垒）折叠为单个「原理探索 / Exploration」入口（指向专栏首页前言），「快速部署手册」与「API 参考」置顶为直达入口
- 文档：措辞修正——README 英文版 `copy-paste runbooks`、中文快速部署文档「可照抄」、英文 API 占位页 `copy-paste curl` 统一改为中性表述（step-by-step / 可直接执行 / directly executable）
- 文档：控制面 API 参考**中英两版**（7.0/7.3 章节）补强「默认启动系统」概念与字段语义——新增「是干什么的」说明（多盘模型下决定 iPXE 菜单超时后自动选中的启动项 + `/boot-vars` 默认启动盘投影；`os` 是菜单项 ID 而非盘名，合法值同建盘 7.1 枚举、不区分大小写）；`menu_timeout` 补 `0` = 无限等待永不自动选择（iPXE 官方语义）；7.0 修正错误示例（注册后无盘实际返回 `menu-default reboot` + 1ms，而非 exit/5000）并区分已配置/未配置两种超时默认值；7.3 与 7.0 `boot` 字段为同一台账字段的覆盖关系；英文版第 3 行残留 `copy-paste` 措辞一并修正
- 文档：README 两版徽章行首新增 **Cloud Native - True Cloud Native** 徽章（定位宣言的直观呈现）
- Control Plane：`SetWorkerDefaultBootRequest` 注释推导链修正（`exit` → `reboot`，与 `_menu_default_for` 实际行为一致）

### 修复

- 文档：API 文档端口号修正——`docs/zh/guide/api/agent-api.md` 全部 curl 示例与 Base URL 由错误的 `localhost:4841` 改为 `4840`（iscsi-server compose 实际映射 `4840:8080`），`control-plane-api.md` 的 `GET /agents` 返回示例 base_url 同步修正；全仓库 4841 零残留
- WebUI：capLabels 克隆方式文案映射缺失 ZFS/xfs/仅全量拷贝新文案（ZFS 支持上线后 UI 直接显示英文原文）——补齐映射并将匹配逻辑改为前缀匹配（动态文案 `full copy only (reflink unsupported on <fs>)` 归并到静态条目）

---

## 2026-08-09

### 新增

- WebUI：添加 / 编辑 Agent 表单新增「iSCSI 数据面地址（可选）」折叠填写框——位于探测前的表单区（Agent ID / API 地址 / Token 之后），默认折叠，点击标题展开/收起（▶ 箭头旋转指示）；编辑模式该 Agent 已配置数据面地址时默认展开；探测成功后自动展开，展示探测推导的地址（base_url 主机名，Worker 侧常不可达），便于现场改为存储节点局域网 IP；中英文案同步

### 变更

- WebUI：探测结果区不再重复显示「iSCSI 数据面地址」输入框（该参数统一收敛到探测前的折叠框）；探测填充逻辑调整——探测前手填的数据面地址优先保留，不再被探测推导值（base_url 主机名）覆盖

---

## 2026-08-10

### 新增

- **Control Plane：`PUT /workers/{worker_id}/mac` —— 修改 Worker 的 MAC 地址绑定**（hostname 不变）：校验新 MAC 格式与占用（已被其他 hostname 绑定返回 `409`），更新 `dnsmasq/dhcp-hosts.conf` 并 HUP 重载（保持 inode 不变，重载立即可见）；审计记录 `worker.mac.update`（含 `old_mac`/`new_mac`/`changed`/`client`，即修改历史，`GET /operations` 可查），MAC 相同时 `changed=false` 不触发重载
- **WebUI：Worker 详情页「身份」卡片新增 MAC 绑定修改**——MAC 行显示当前绑定值 +「修改 MAC」按钮，编辑态提供输入框 / 保存 / 取消，保存失败显示错误（含 409 占用提示），并展示审计提示文案；对接 `PUT /workers/{worker_id}/mac`，保存后刷新台账

### 变更

- **README 中英双版新增「云原生固件仓库」小节**——在「核心能力」与「快速开始」之间引导读者了解配套固件仓库 [iPXE-Stateless](https://github.com/dutyc/ipxe-stateless)：固件本身无状态（DHCP 取配置、链式加载、无盘进系统）、仓库亦无状态（只维护补丁与构建资产、可随新基线重建）、全系无状态适配（RTL8125 native 驱动 / snponly 本地引导兜底 / debug 构建修复）
- **禁用 dnsmasq 容器的 8080 web 管理面板**：`jpillora/dnsmasq` 镜像默认 `ENTRYPOINT=webproc`，会在 8080 启动 web 管理面板（host 网络下直接占用宿主 8080）；compose 覆盖 `entrypoint: ["/usr/sbin/dnsmasq"]` 直接运行 dnsmasq 二进制，完全绕过 webproc；文档验证方式同步改为检查 67/69 UDP 端口监听（`ss -lunp`）

### 修复

- **Control Plane：dnsmasq 主机名绑定写入后不生效（重启 Worker / 手动 `killall -HUP dnsmasq` 均无效，重建容器才生效）**：根因是 `DnsmasqHosts._write_lines` 复用 `_atomic_write_text` 的 rename 原子写（mkstemp + os.replace），每次写入都更换文件 inode；而 `dhcp-hosts.conf` 在 docker-compose 中以**文件级 bind mount**（`./dnsmasq/dhcp-hosts.conf:/etc/dnsmasq.d/dhcp-hosts.conf`）挂载进容器，挂载锁定的是写入瞬间的 inode——rename 后容器内仍指向旧 inode，dnsmasq 永远读不到新绑定，只有重建容器重新挂载才生效；已改为直接截断写原文件（保持 inode 不变），文件级挂载语义不再被破坏，HUP 重载恢复有效
- **WebUI：删除确认框被容器边界裁剪（只能看到一点点）**：根因是 `ConfirmAction` 确认框用 `position: absolute` 在触发按钮下方展开，而 Worker 详情页系统盘卡片（`.detail-card`）设置了 `overflow: hidden`，展开部分被裁剪；批量删除侧边栏（230px 宽 + `overflow-y: auto`）同样存在该隐患。已将 `ConfirmAction` 改为**固定遮罩层 + 居中弹窗**（`position: fixed` 覆盖全屏，`z-index: 200`），不再依赖触发按钮的定位上下文，任何容器都无法裁剪；同时修复点击 disabled 按钮也会弹出确认框的问题（trigger 内 Button 禁用时不弹窗）