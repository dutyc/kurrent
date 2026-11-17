---
title: "引导与设备准入"
---

# 引导与设备准入

设备启动时用到以下端点：引导链状态、启动变量投影、注册窗口与挑战-证明。

## GET /boot-assets

### 说明

引导链状态（需管理面认证：`human` 或 `automation`）。控制面解析 `dnsmasq.conf` 的 `dhcp-boot` 与 `boot.ipxe → boot.ipxe.cfg → menu.ipxe` 脚本链，回答三件事：

- UEFI 固件是否就位且具备引导脚本依赖的能力
- 脚本链是否齐全
- 菜单是否可解析

只做状态判定，不展开菜单项，也不做目录浏览。控制面只读挂载 `./tftp`（`dnsmasq.conf` 缺失时返回 `404`）。

### 返回字段

| 字段 | 说明 |
|---|---|
| `firmware[]` | `dhcp-boot=tag:efi64` 声明的 UEFI 固件：`mode`（固定 `uefi`）、`label`、`file`、`path`、`host_path`、`status`（ok/incomplete/missing/empty/rejected）、`size`、`mtime`；文件可读时附 `version` 与 `capabilities[]`（`id`/`label`/`present`） |
| `stage2[]` | 脚本链文件（`boot.ipxe`、`boot.ipxe.cfg`、`menu.ipxe`）：`file`、`path`、`host_path`、`status`、`size`、`mtime` |
| `menu` | `status`（ok/unavailable）与 `items`（可选项计数，跳过 `--gap` 行） |
| `secureboot` | 可选 Secure Boot 产物：`present`、`status`（ok/incomplete/missing）、`artifact`、`path`、`artifact_sha256`、`artifact_size`、`manifest_present`、`manifest_matches`、`signer`、`built_at`、`boot_https_port`、`port_matches`、`boot_ca_present`、`boot_ca_sha256`、`boot_ca_matches` |

固件 `status` 的取值含义：

- `incomplete`：固件文件存在但缺少 `HTTPS` / `efi_nvs` / `akgen` / `akpub` / `quote` / `TOFU` 中的任一项——stock iPXE 同样无法完成 TPM 证明。
- `empty`：零字节文件。
- `missing`：文件不存在。
- `rejected`：路径越出 tftp 根，控制面拒绝且不会读取该路径。

`host_path` 是相对部署根的宿主路径（`tftp/...`），用于补文件。`menu.status=ok` 要求脚本中至少有一个 `menu` 段和一条非 `--gap` 的 `item`。

`secureboot.status=ok` 要求同时满足：签名镜像存在、manifest 哈希一致、固件内嵌的引导端口与部署的 `KURRENT_BOOT_HTTPS_PORT` 一致、boot CA 证书就位，且 manifest 记录的 CA 指纹与控制面当前 boot CA 一致。任一项不一致时固件会拒绝本控制面：端口错则拨不通，锚错则握手被拒。没有 `tftp/secureboot/` 产物时 `present=false`，不影响其余检查。

### curl

```bash
curl --cert client.crt --key client.key https://<host>/api/cp/boot-assets
```

---

## GET /boot-vars

### 说明

给 iPXE 启动脚本读取 per-worker 启动变量。该接口不鉴权，只暴露受控内网启动所需变量。NVMe-oF 凭据（`nbft_secret` / `nbft_tls_psk`）**只在 TPM 证明通过后注入**（`mac` + 一次性 `nonce` + 对该 nonce 的 PCR quote，见本节）。

> **注意**：该端点**只读**（启动变量投影，无写副作用）。注册窗口期注册（新 MAC 携 TPM AK 入设备池）已收敛到 `GET /devices/report`（见[设备池](./devices.md)），由 `boot.ipxe.cfg` 在请求 `/boot-vars` 之前先 `chain`。

**限速**：按来源 IP 滑窗限速（默认 240 次/分钟，`KURRENT_CP_RATE_LIMIT_BOOT_VARS` 可覆盖，`<=0` 关闭）。超限返回 `429` + `Retry-After` 并写 `boot_rate_limited` 告警。证明校验是这里最贵的操作，限速只抑制扫描，不作身份边界。

控制面按以下顺序识别设备并投影启动变量：

1. `mac` → `state/devices.yml`（设备台账）→ `bound_worker_id` → `state/workers.yml`
2. `hostname` → `state/workers.yml`（hostname 或 worker_id；未传 MAC 或台账未命中时的回退）
3. `config/agents.yml`（默认启动盘的 Agent 数据面地址）

然后返回该 Worker 的数据面地址、启动盘 NQN、默认菜单项和菜单超时。

默认返回 iPXE 脚本片段，兼容性最好，可以直接被 iPXE `chain` 执行。加 `format=json` 时返回 JSON，方便人工调试。

### 字段来源

`/boot-vars` 返回的是 inventory 的投影：

| 返回字段 | 来源 |
|---|---|
| `worker_id` | 识别到的 Worker 标识（绑定权威优先；池中未绑定设备与空脚本不返回） |
| `disk_nqn` | 默认启动盘（同选盘规则）的完整盘 NQN（盘记录 `nqn`，数据面唯一标识，控制面权威值）——固件原样拼进 `sanboot nvme://<ip>:<数据面端口>/${disk-nqn}`，不再用 hostname/os/os-tag 重拼；盘记录缺 `nqn` 或 Worker 无系统盘时不返回 |
| `base_iqn` | `workers.yml` 中该 Worker 默认启动盘（`default_disk` 对应的盘，未设时取第一块）的 `iqn` 去掉最后一个 `:` 后的前缀——盘标识权威 = NQN，IQN 由盘 NQN 派生；Worker 无系统盘时**不返回**（iPXE 沿用 `boot.ipxe.cfg` 静态默认值） |
| `storager_ip` | 默认启动盘（同上选盘规则）的 `agent` -> `agents.yml` 中该 Agent 的 `storager_ip`（数据面地址，供 NVMe-oF 引导使用）；无系统盘时不返回 |
| `iscsi_sep` | iSCSI root **连接符**（`${storager-ip}` 与 `${base-iqn}` 之间的分隔字段）。**仅 stgt / LIO 后端返回**：stgt 后端为 `:::1:`（lun 占位 1），LIO 后端为 `::::`（空占位）；后端类型**实时查询 Agent `/capabilities` 的 `backend` 字段**（节点自报，能力即真相），未登记 / 查询失败 / 取值非法时后端视为未知并记录 warning，不返回该字段（不按 stgt 回退）；**不由 `agents.yml` 的 `tags` 推导**（tags 只作人工标签，否则标签与实况漂移时会按过期值拼出错连接符）；**nvmet 后端不下发**；无系统盘时不返回。当前引导脚本不消费该字段，保留给 iSCSI 兼容后端的 API 客户端 |
| `nbft_secret` | 该 Worker 的 NVMe-oF 认证密钥（DHHC-1，`state/credentials.yml` 按 worker_id 索引，见[系统盘](./disks.md)）；**已绑定 Worker、密钥库有条目且 TPM 证明通过时注入**，无密钥 / 未绑定 / 未证明 / 请求被拒时不返回。固件侧消费：menu 拼 `nvme://...?secret=${nbft-secret}`（nvmet 后端下无密钥即 `/boot-vars` 整体不下发，menu 因缺变量而拒绝引导） |
| `nbft_tls_psk` | NVMe/TCP TLS 的预共享密钥：**认证密钥的纯函数**（`HMAC-SHA256(raw_key, b"kurrent-nvme-tls-psk-v1")` → 64 hex，见[系统盘](./disks.md)，无独立存储字段——轮换/吊销认证密钥即原子轮换/吊销 PSK）。**须同时满足「已绑定且有密钥」「TPM 证明通过」「后端为 nvmet」「该 Agent 上报的数据面端口就绪」才下发**，任一不满足即整个引导变量不下发（拒绝引导并记审计）——加密是默认行为，无明文回退。固件用它在该端口完成自身握手，并随 NBCT 表交给 initramfs，由其在同一端口以 `--tls` 重连 |
| `nbft_tls_port` | 数据面端口（固件段与内核段共用；实时取 Agent `/capabilities.port.trsvcid`——能力即真相，同 `iscsi_sep`/`backend` 的实时判定口径）；与 `nbft_tls_psk` 同条件下发。menu 在 sanboot URI 上拼 `&tls_psk=${nbft-tls-psk}&tls_trsvcid=${nbft-tls-port}` |
| `hostnqn` | 该 Worker 的发起端 Host NQN（盘 NQN 前缀 + `:host.<worker_id>`，与盘 NQN 同域、与 nvmet-host 登记的 host 条目一致）；**默认盘带 NQN 时注入**。固件侧消费：iPXE nvmetcp 默认 hostnqn 为 `nqn.2014-08.org.ipxe:<uuid>`（无 UUID 回退 `:ipxe`），与登记值不匹配时严格模式认证必败，须以本字段覆盖（固件补丁 `0005` 支持 `hostnqn` 设置） |
| `os` | 默认启动盘（`default_disk` 对应的盘，未设时取第一块）的 os 名（盘记录权威）——菜单只保留一个通用 OS 项（`boot-os`），本变量仅用于菜单标签展示；数据面路径由 `disk_nqn` 决定。Worker 无系统盘时不返回（reboot 循环等待建盘） |
| `os_version` | 默认启动盘的版本备注（盘记录 `os_version`，`''` = 无版本）——空值不下发，menu.ipxe 的 `os-label` 拼接自适应（有版本显示 `os version`）。Worker 无系统盘时不返回 |
| `menu_default` | 推导链：`workers.yml` 的 `default_disk`（首块盘自动设为默认；多盘用[设置默认盘](./disks.md)指定，os_tag 引用具体盘）> `boot.menu_default`（显式配置）> `reboot`（未配置时循环重启等待）。有默认盘时固定为 `boot-os`（唯一 OS 项）；未设默认盘时使用 `boot.menu_default`（`config` / `shell` / `reboot` / `exit`）；都未配置时为 `reboot` |
| `menu_timeout` | 已配置默认启动时：`boot.menu_timeout` > `spec.boot.menuTimeoutMs`（默认 5000）；处于 `reboot` 循环时：固定用 `spec.boot.autoBootTimeoutMs`（默认 1）。单位均为毫秒 |

查找 Worker 的规则（**绑定权威优先**）：

```text
mac -> devices.yml（设备台账）-> bound_worker_id -> workers.yml
mac 未传或未命中 -> hostname -> workers.yml（hostname 或 worker_id）
mac 未命中且 hostname 也未命中 -> dnsmasq 投影（仅存量/无台账回退）
未识别且 mac 已传 ->
  - 设备在池中（pooled）-> reboot 循环（menu-default=reboot + 短超时），等待绑定
  - 设备已吊销（revoked）-> 空脚本（菜单停留）
  - 未知 MAC + 注册窗口开启 + 额度未用尽 + 携带有效 AK 且 sb=1 -> 入设备池（见下「注册窗口期注册」），返回 reboot 循环
  - 未知 MAC + 窗口关闭 / 额度用尽 / 无 AK / Secure Boot 未开启 -> 空脚本（菜单停留；不入池、不写台账）
```

### 默认启动项规则

默认启动项由 `/boot-vars` 按以下顺序推导：

```text
default_disk（首块盘自动设为默认；多盘用[设置默认盘](./disks.md)指定）-> boot.menu_default（显式配置）-> reboot（未配置）
```

- 只有一块系统盘时无需额外设置：建盘即写入 `default_disk`。多盘并存时调用 `PUT /workers/{worker_id}/default-disk` 指定默认启动盘（os_tag 引用具体盘）：

```text
disk=<os_tag> -> menu_default=boot-os, os=<该盘 os>, os-version=<该盘 os_version（如有）>, disk_nqn=<该盘 nqn>
```

- MAIN MENU 只保留一个通用 OS 项 `boot-os`，由默认盘推导；`os` / `os-version` 仅作菜单展示，启动身份由 `disk_nqn` 承载

- 也可以显式清除 `default_disk`（`PUT /workers/{worker_id}/default-disk`，`{"disk": null}`），改用 `boot.menu_default` 指定 iPXE 菜单默认项（如配置界面 `config`、退出 `exit`）
- 两者都没有时，`menu_default` 返回 `reboot`（短超时循环重启，等待管理员建盘 / 设置默认盘；`exit` 仅出现在显式设置时）

### 注册窗口期注册（Zero-touch Provisioning）

新设备开机时没有身份，iPXE 先 `chain` `/devices/report`（11 字段指纹 + TPM AK 与 Secure Boot 状态，见[设备池](./devices.md)）再请求 `/boot-vars`。

**注册只在窗口期**：仅当注册窗口开启、放行额度未用尽、上报携带有效 TPM AK（`ak_public` / `ak_name` / `ak_type`）且自报 `sb=1` 时，未知 MAC 才被收进设备池。窗口关闭后未知 MAC 的上报不入池、不写台账，只留拒绝审计（无窗口外注册通道，代码层不可配永久）。无 TPM 或 Secure Boot 未开启的设备不入池。

1. `GET /devices/report`：未知 MAC + 窗口开启 + 额度未用尽 + 有效 AK + `sb=1` → 写入 `state/devices.yml`（`state=pooled`，指纹入库，`identity` 记录 AK，`source=ipxe`）；不满足时只记审计 `device.register rejected`（`no_tpm` / `sb_disabled` / `window_closed`）
2. `GET /boot-vars`：MAC 在池中未绑定 → 返回 `menu-default=reboot` + 短超时，循环重启等待管理员绑定
3. 管理员将设备绑定到 Worker（WebUI / API，单绑与批量绑定（见[设备池](./devices.md)））后，下次启动即按 Worker 配置正常引导

存量设备 AK 认领：已入池设备在窗口期内带 AK 上报即写入 `identity`（认领完成）。AK 不一致拒绝覆盖（仅审计 `device.claim rejected`，恢复走删登记流程）。认领只看窗口期、不受放行额度约束。

认领完成后设备须再通过一次 TPM 证明，首次成功即记录 PCR 基线（见本节）。

控制项：

| 项 | 默认 | 说明 |
|---|---:|---|
| 注册窗口 | 关闭 | 部署期用 `POST /settings/registration-window` 开启（TTL 1-60 分钟硬上限，到期自动关闭），见本节|
| 放行额度 | 不限 | 开窗时可选 `max_devices`（1-1000 台），窗口期内自动入池设备数达到上限即停止放行，见本节|
| 证明强制 | 开启 | `GET/PUT /settings/enforcement`：默认开启，`/boot-vars` 只向 TPM 证明通过的已绑定设备下发；显式关闭仅用于过渡/排障，见本节|
| `spec.boot.autoBootTimeoutMs` | `1` | reboot 循环的菜单超时（毫秒） |

注册与认领有操作日志（`device.register` / `device.claim` / `device.ak.register`），失败不写台账并返回空脚本，下次请求重试，不影响 iPXE 引导。

### 防冒领（绑定即认证）

请求带 `mac` 时，控制面按设备台账校验该设备**绑定到的 Worker**（`bound_worker_id`，绑定权威）。不符合（绑定其他 Worker / 未绑定 / 未知设备）→ **拒绝下发**：返回空脚本，不泄露启动变量。

不带 `mac`（仅 hostname）的请求无法校验身份：**证明强制（默认开启）下直接拒绝**（`missing_mac`）；显式关闭时只下发菜单/导航变量，绝不注入凭据。

这使设备↔Worker 绑定与 TPM 证明共同成为开机时的认证边界：只有绑定的设备、并在证明通过后，才能拿到该 Worker 的启动配置与数据面凭据（如 `disk_nqn` / `nbft_secret` / `nbft_tls_psk`）。

### Query 参数

| 参数 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `mac` | 否 | 无 | MAC 地址。后端自动剥离 `:` / `-` / `.` 后归一化，带冒号（`00:0c:29:b9:8b:2d`）与 `mac:hexraw`（`000c29b98b2d`）格式都支持 |
| `hostname` | 否 | 无 | 主机名，如 `worker-01` |
| `format` | 否 | `ipxe` | `ipxe` 或 `json` |
| `nonce` | 否 | 无 | 挑战-响应一次性 nonce（`/devices/challenge` 签发，见本节）；证明强制（默认开启）下缺失 → `quote_invalid` 拒绝 |
| `quote` | 否 | 无 | TPMS_ATTEST 原始字节 hex（不含 TPM2B_ATTEST 的 2 字节长度前缀）：对 PCR 0/2/4/7（SHA-256）的 quote，extraData 必须等于 `nonce` 字符串的 ASCII 字节（见本节）；缺失或格式非法 → `quote_invalid` 拒绝 |
| `quote_sig` | 否 | 无 | AK 私钥对 quote 的签名（hex；ECC P-256 为 DER，RSA-2048 为 PKCS#1 v1.5）；验签失败 → `quote_invalid` 拒绝 |

`mac` 和 `hostname` 至少建议传一个。iPXE 端推荐两个都传：

```text
/boot-vars?mac=${mac}&hostname=${hostname}
```

> **注意**：规范上 `${mac:hexraw}` 与 `${mac}` 等价（后端统一归一化），但部分真实 iPXE 固件对 `hexraw` 修饰符展开异常（可能为空）。实测必须使用带冒号的 `${mac}`。

### iPXE 格式 curl

```bash
curl -s "$BASE_URL/boot-vars?mac=000c29b98b2d&hostname=worker-01"
```

成功返回示例：

```ipxe
#!ipxe
# boot vars for worker-01
set disk-nqn nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89
set base-iqn iqn.2026-07.com.kurrent
set storager-ip 192.168.80.3
set nbft-secret DHHC-1:01:<base64>   # 仅 NVMe-oF 认证启用时返回（见「系统盘与凭据」）
set nbft-tls-psk <64 hex>            # 认证密钥派生，TLS 1.3 预共享密钥
set nbft-tls-port 4421
set hostnqn nqn.2026-07.com.kurrent:host.worker-01
set os ubuntu
set menu-default boot-os
set menu-timeout 5000
```

已注册但未配置默认启动（无系统盘 / 显式清除了 `default_disk` / 未显式设 `boot.menu_default`）时返回：

```ipxe
#!ipxe
# boot vars for worker-01
set menu-default reboot
set menu-timeout 1
```

新 MAC（窗口期注册）与完全无法识别时，窗口关闭或无 AK 则返回空脚本：

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
  "worker_id": "worker-01",
  "disk_nqn": "nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",
  "base_iqn": "iqn.2026-07.com.kurrent",
  "storager_ip": "192.168.80.3",
  "nbft_secret": "DHHC-1:01:<base64>",
  "nbft_tls_psk": "<64 hex>",
  "nbft_tls_port": "4421",
  "hostnqn": "nqn.2026-07.com.kurrent:host.worker-01",
  "os": "ubuntu",
  "menu_default": "boot-os",
  "menu_timeout": 5000
}
```

已注册但未配置默认启动时返回：

```json
{
  "worker_id": "worker-01",
  "menu_default": "reboot",
  "menu_timeout": 1
}
```

设备在池中但未绑定 Worker 时只返回菜单变量（无 `worker_id`）：

```json
{
  "menu_default": "reboot",
  "menu_timeout": 1
}
```

无法识别且未注册（窗口关闭 / 无 AK）时返回：

```json
{}
```

### iPXE 接入方式

`tftp/boot.ipxe.cfg` 中，未签名调试脚本的信任根链在挑战-应答握手后**经 HTTPS（nginx 443，TOFU 固定叶证书指纹）**拉取该端点（见本节）；签名固件经 4443 拉取，只信任构建时嵌入的 boot CA。

```ipxe
# 阶段 3：挑战-证明——非秘密 nonce 绑定本次引导
:challenge
chain --autofree ${https-url}devices/challenge?mac=${mac} && goto quoted || goto bootvars

:quoted
quote ${nonce} || goto bootvars
chain --autofree ${https-url}boot-vars?mac=${mac}&hostname=${hostname}&nonce=${nonce}&quote=${quote}&quote_sig=${quote-sig} || goto vars-done
goto vars-done

# 降级路径（challenge 失败：设备未登记 AK，或旧固件）：
# 证明强制默认开启，无 quote 会被 quote_invalid 拒绝；仅在显式关闭时放行
:bootvars
chain --autofree ${https-url}boot-vars?mac=${mac}&hostname=${hostname} || goto vars-done

:vars-done
isset ${iscsi-sep} || set iscsi-sep :::1:
isset ${hostname} && set initiator-iqn ${base-iqn}:${hostname} || set initiator-iqn ${base-iqn}:${mac}
```

`${quote}` / `${quote-sig}` 是固件写入的 iPXE 变量（变量名用连字符），对应请求参数 `quote` / `quote_sig`。成功后返回的 `disk-nqn` / `storager-ip` 可能覆盖静态默认值，派生变量在 `:vars-done` 重建。`base-iqn` 用于派生 `initiator-iqn`，`iscsi-sep` 的默认值当前不参与菜单拼装。

`menu.ipxe` 主引导项用 NVMe-oF 拼装：`set root-path nvme://${storager-ip}:${nbft-tls-port}/${disk-nqn}`，已注入 `nbft-secret` 时附加 `?secret=`，再附上 `&tls_psk=…&tls_trsvcid=…`。`nbft-tls-port` / `nbft-tls-psk` 为必需项——缺失即目标端加密未就绪，该项直接 `goto failed`，不再拼明文 URI。

### Agent 数据面地址

`/boot-vars` 返回的是 Worker 连接存储的 **数据面地址**（供 NVMe-oF 引导使用），不是 Agent HTTP API 地址。建议在 `config/agents.yml` 里显式配置：

```yaml
agents:
  a1b2c3d4e5f6:
    base_url: http://host.docker.internal:4840
    storager_ip: 192.168.80.3
```

如果没有配置 `storager_ip`，控制面会退回使用 `base_url` 的 host 部分；但当 `base_url` 是 `host.docker.internal` 时，这个值不适合给物理 Worker 使用。

### 注册窗口与证明强制（/settings/registration-window、/settings/enforcement）

#### 说明

注册窗口是部署期的受控注册通道：仅窗口开启期间，新设备上报（`GET /devices/report`）可携 TPM AK 自动注册入池并完成 AK 认领。窗口关闭后未知设备的上报不入池、不写台账，只留拒绝审计（无窗口外注册通道）。

TTL 与额度上限来自声明 `spec.registration`（`windowMaxTtlMinutes` 默认 60、`windowMaxDevices` 默认 1000；不可配置为永久），到期自动关闭（懒计算，状态以查询结果为准）。

可另设**放行额度**（`max_devices`，台）：窗口期内自动入池的设备数达到上限即停止放行（不主动清窗口记录）。额度只约束未知设备自动入池——AK 认领与管理员手动登记/导入不受其约束；已放行数派生自设备台账，不另存计数器。

全部端点需 `Authorization: Bearer`。

证明强制默认开启：`/boot-vars` 对已绑定设备执行注入四条件第 4 条，失败原因码如下：

| 原因码 | 触发条件 |
|---|---|
| `missing_mac` | 缺 `mac` |
| `ak_unregistered` | 台账无 AK |
| `quote_invalid` | 缺 `nonce`/`quote`/`quote_sig`，或 quote 结构、签名无效 |
| `nonce_invalid` | nonce 重放或 extraData 不匹配 |
| `pcr_mismatch` | PCR 摘要与平台基线不符 |

显式关闭仅用于过渡/排障：此时无 AK 设备可拿到菜单变量，但**凭据一律不注入**（凭据只认证明）。无效 proof 任何模式下都不放行。

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
| `limits` | object | 当前声明的上限：`ttl_minutes_min` / `ttl_minutes_max` / `max_devices`（WebUI 按它收敛输入） |

`admitted` 为**派生值**，不存计数器：窗口期内设备台账中 `source == "ipxe"` 且 `first_seen >= opened_at` 的条目数。因此管理员手动登记（`POST /devices`，`source=manual`）与清单导入不占额度，窗口开启前的存量设备（含仅认领 AK 者）也不计入。

```json
{"open": true, "opened_at": "2026-08-21T10:00:00+08:00", "ttl_minutes": 30, "closes_at": "2026-08-21T10:30:00+08:00", "remaining_seconds": 1799, "max_devices": 20, "admitted": 3, "remaining_devices": 17, "quota_exhausted": false, "limits": {"ttl_minutes_min": 1, "ttl_minutes_max": 60, "max_devices": 1000}}
```

#### POST /settings/registration-window

开启注册窗口（状态码 201）。已开启 → 409（先关闭再开）；过期残留记录可直接重开（覆盖），重开后额度按新的开启时刻重新派生。

写入操作日志（`settings.registration_window`）。

**请求体**：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `ttl_minutes` | 是 | int | 1 到 `spec.registration.windowMaxTtlMinutes`（默认 60，超出返回 400） |
| `max_devices` | 否 | int \| null | 放行额度（台），1 到 `spec.registration.windowMaxDevices`（默认 1000，超出返回 400）；省略/`null` = 不限，仍受 TTL 约束 |

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

**响应**：同 GET（开启后的窗口状态，含 `limits`）。

#### DELETE /settings/registration-window

提前关闭注册窗口（从未开启/无记录 → 409）。写入操作日志（`settings.registration_window`）。

```bash
curl -X DELETE "$BASE_URL/settings/registration-window" \
  -H "Authorization: Bearer $TOKEN"
```

**响应**：`{"open": false}`

#### GET/PUT /settings/enforcement

查询/切换 TPM 证明强制开关（持久化到 `state/settings.json`，重启保留）。

**请求体（PUT）**：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `enabled` | 是 | bool | `true` = 开启强制（无 AK / 无 quote / 证明失败设备拒绝引导） |

```bash
curl -X PUT "$BASE_URL/settings/enforcement" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

**响应**：`{"enabled": bool}`（GET/PUT 相同）。PUT 写入操作日志（`settings.enforcement`）。

### GET /devices/challenge

挑战端点（**不鉴权**）：为 `/boot-vars` 证明链路签发一次性 nonce（短 TTL、绑定 MAC、防重放，nonce 本身无秘密）。

设备不存在、未登记 AK 或已吊销 → **404**（无法走证明链路，且不给吊销设备签发 nonce）。

**Query 参数**：

| 参数 | 必填 | 说明 |
|---|---|---|
| `mac` | 是 | MAC 地址（归一化规则同 `/boot-vars`） |

**成功返回（200，`text/plain`）**：`#!ipxe` 脚本体——iPXE 用 `chain` 直接消费，执行后 `${nonce}` 即为 64 hex 字符一次性 nonce：

```ipxe
#!ipxe
set nonce 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
```

**错误**：`400`（非法 MAC）；`404`（设备不存在/未登记 AK）；`429`（同一来源 IP 每分钟超过阈值，带 `Retry-After`）。

**限速**：与 `/devices/report` 一样按来源 IP 滑窗限速（默认 120 次/分钟，`KURRENT_CP_RATE_LIMIT_CHALLENGE` 可覆盖，`<=0` 关闭）；超限写 `boot_rate_limited` 告警。

**挑战-证明链路（与 `/boot-vars` 配合，TPM 证明）**：

1. `GET /devices/challenge?mac=<mac>` → 执行响应脚本后取得 `${nonce}`
2. 固件用 TPM AK 对 PCR 0/2/4/7（SHA-256）做 quote，`extraData` 原样填入 `${nonce}` 字符串的 ASCII 字节（64 个 hex 字符），不做 hex 解码或其他编码变换。输出 `quote`（TPMS_ATTEST 原始字节 hex，不含 TPM2B 长度前缀）与 `quote_sig`（AK 私钥对 quote 字节的签名 hex）。PCR 读取与 quote 在同一次 TPM 调用内完成。
3. `GET /boot-vars?mac=&hostname=&nonce=&quote=&quote_sig=` → 校验 quote 结构（magic `0xFF544347`、type `TPM_ST_ATTEST_QUOTE`、PCR 选择固定为 0/2/4/7 且仅 SHA-256）、AK 签名、`extraData == nonce`、nonce 未被消费，最后比对 PCR 摘要与平台基线。全部通过才下发凭据（注入四条件第 4 条，见本节）；任何一步失败 → 空脚本（拒绝下发，审计 `boot_vars.credential` 与 `device.attest`，reason 见本节）。

quote 校验先于 nonce 消费：格式或签名错误不会烧掉本次 nonce，设备可修正后重试；nonce 被消费后重放返回 `nonce_invalid`。
