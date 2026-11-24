---
title: "设备池"
---

# 设备池

设备台账是绑定与启动授权的底层实体。设备池的查询、导入、绑定、注销与证明基线端点如下。

## 设备池（设备台账）

设备台账（`state/devices.yml`）是三层实体模型（设备 / Worker / 系统盘）的底层实体。窗口期注册与手动导入只入设备池，不产生引导权限；设备绑定到 Worker 后才有身份。

绑定关系权威在设备侧（`bound_worker_id`），Worker 侧只投影不存储。

**绑定语义**：设备 ↔ Worker 严格**一对一**。绑定走 `POST /devices/{mac}/bind`；`force=true` 原子换绑（预校验 → 新绑定落盘 → 旧绑定清除 → 失败回滚）。

解绑（`DELETE /devices/{mac}/bind`）后设备回池，Worker 的系统盘保留。删除 Worker 联动解绑（见 [Worker](./workers.md)）。`POST /workers` 传 `mac` 也直接完成绑定。

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
    identity:                     # TPM 身份（窗口期带 AK 上报或认领后填充；证明强制依赖）
      kind: tpm_ak
      ak_public: 3059301306072a8648ce3d0201...   # AK 公钥 SPKI DER hex
      ak_name: 000b6f2a...                        # TPM2B_NAME hex（alg + 公钥摘要）
      ak_type: ecc_p256                           # ecc_p256 | rsa2048
    pcr_baseline:                 # 首次证明通过时记录
      selection: [0, 2, 4, 7]
      digest_sha256: 9f2c...7b1d
      recorded_at: 2026-08-15T10:05:00+08:00
    attestation:                  # 最近一次证明结果
      state: proven               # unproven | proven | mismatch
      last_at: 2026-08-15T12:00:00+08:00
      last_reason: null           # ak_unregistered | quote_invalid | nonce_invalid | pcr_mismatch | baseline_approved
    source: ipxe                  # ipxe（自动入池）| manual（手动录入/导入）
    boot_firmware: snponly        # 首跳固件：snponly（默认，UEFI SNP）| full（全驱动）
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

### GET /devices

设备池列表，`state` 过滤（`all` / `pooled` / `bound` / `revoked`，默认 `all`）。

**curl**：

```bash
curl "$BASE_URL/devices?state=pooled" \
  -H "Authorization: Bearer $TOKEN"
```

**成功返回**：数组，每项为一条设备记录（见上「记录结构」）。

### GET /devices/{mac}

单设备详情（绑定 Worker、指纹、首/末次上报）。`mac` 用带冒号格式（`00:0c:29:b9:8b:2d`）。

**404**：设备不存在。

### POST /devices

手动注册设备：MAC（+可选 UUID/型号/序列号）入池。

**请求体**：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `mac` | 是 | str | MAC 地址 |
| `uuid` | 否 | str | SMBIOS UUID（双因子，可选） |
| `manufacturer` / `product` / `serial` | 否 | str | 申报信息，仅作台账初始值，设备上报后以申报值为准更新 |
| `boot_firmware` | 否 | str | 首跳固件：`snponly`（默认，UEFI SNP）或 `full`（全驱动）；决定该设备 DHCP 首跳拿哪份固件 |

**成功返回（201）**：设备记录（`state=pooled`，`source=manual`）。

**409**：设备已存在（含已吊销——吊销设备不可重新注册）。

### POST /devices/import

批量导入设备清单（MAC 清单预导入）：逐项独立，重复跳过，非法/吊销计 `failed`。

**请求体**：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `entries` | 是 | array | 清单数组，每项同 `POST /devices` 请求体（`mac` 必填） |

**成功返回**：

| 字段 | 说明 |
|---|---|
| `created` | 本次新增入池的条目列表（每项 `{mac}`） |
| `skipped` | 已存在（pooled/bound）跳过项及原因 |
| `failed` | 非法 MAC / 已吊销项及原因 |

### DELETE /devices/{mac}

注销设备（吊销）：`pooled` → `revoked`，设备保留在台账（审计保留）。

吊销同时在该设备的 dnsmasq 绑定文件中写入 `MAC,ignore`，清除其静态绑定，使其后续不再获得 DHCP 租约。引导链的第一道拒绝发生在网络中继层，而不是等到无鉴权端点。

写文件失败不影响吊销结果，但会记 `device.dhcp_block` 错误事件。

**409**：设备已绑定 Worker（须先解绑）或已吊销。

### DELETE /devices/revoked

清除已吊销设备（`revoked` → 删除条目）；`dry_run=true` 只返回将被清除的 MAC，不改台账。

只清理已吊销项，`pooled` / `bound` 不受影响；无可清除项时返回空列表（幂等）。用于清理探测残留或误吊销的设备。清除条目时同步删除该 MAC 在 dnsmasq 绑定文件中的 `MAC,ignore` 封禁行（`dry_run` 不改任何文件）。

`mac` 可重复给出以限定范围（形如 `?mac=02:00:00:00:00:01&mac=02:00:00:00:00:aa`；未吊销的 MAC 忽略），缺省清除全部 revoked。

```bash
curl -s -X DELETE "$BASE_URL/devices/revoked?dry_run=true" -H "Authorization: Bearer $TOKEN"
curl -s -X DELETE "$BASE_URL/devices/revoked?mac=02:00:00:00:00:01&mac=02:00:00:00:00:aa" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{ "purged": ["02:00:00:00:00:01", "02:00:00:00:00:aa"], "count": 2, "dry_run": false }
```

### GET /devices/report

iPXE 设备信息上报入口（**不鉴权**，由 `boot.ipxe.cfg` 在请求 `/boot-vars` 之前先 `chain`）：更新指纹 + `last_seen`；未知 MAC 仅在注册窗口开启、放行额度未用尽、带有效 TPM AK 且 Secure Boot 开启时入池。

已有设备的 `uuid` 只允许补空，不能被上报值覆盖（绑定比对依据不由无鉴权端点改写）。

**返回空脚本体**（`#!ipxe\n`，`chain` 可直接消费，无脚本副作用；空 body 会被部分 iPXE 报 EOF，故统一返回合法空脚本）。

| 参数 | 必填 | 说明 |
|---|---|---|
| `mac` | 是 | MAC，支持带冒号（`${mac}`）与 hex 无分隔（`${netX/mac}`）格式 |
| `uuid` / `manufacturer` / `product` / `serial` / `cpumodel` / `mem-type` / `chip` / `busid` | 否 | 字符串字段，空值容忍 |
| `mem-total` / `mem-speed` | 否 | 整数，兼容 `0x` hex 与十进制，归一化十进制存储 |
| `ak_public` | 否 | AK 公钥 SPKI DER hex（`ecc_p256` 或 `rsa2048`），注册/认领用；缺失或非法 → 无身份 |
| `ak_name` | 否 | TPM2B_NAME hex（68 hex：算法前缀 + 公钥摘要），与 `ak_public` 配对；缺失或非法 → 无身份 |
| `ak_type` | 否 | `ecc_p256` / `rsa2048`；携带 `ak_public` 时必须同时提供且与实际算法一致，缺失或不符 → 无身份 |
| `sb` | 否 | Secure Boot 状态自报，`1` = 开启；未知 MAC 入池要求 `sb=1`（启动期不信任自报值，证明才是判据） |

行为：

- 已注册设备：更新指纹（非空字段覆盖）+ `last_seen`，`state` 不变；窗口开启且带有效 AK 时写入 `identity`（AK 认领，不受放行额度约束），AK 不一致 → 拒绝覆盖（仅审计 `device.claim rejected`）
- 吊销设备：忽略（不更新、不复活）
- 未知 MAC + 注册窗口开启 + 额度未用尽 + 有效 AK + `sb=1`：入池（`state=pooled`，`identity` 写入，`source=ipxe`，审计 `device.ak.register`）
- 未知 MAC + 窗口关闭 / 额度用尽 / 无有效 AK / `sb!=1`：不入池、不写台账（审计 `device.register rejected`，reason=`window_closed` / `no_tpm` / `sb_disabled`；注册只在窗口期且受放行额度约束，见[引导链](./boot.md)）

**限速**：按来源 IP（nginx 覆盖写入 `X-Real-IP`）滑动窗口限速，默认 120 次/分钟。超限返回 `429`（带 `Retry-After`）并写 `boot_rate_limited` 告警。阈值可用 `KURRENT_CP_RATE_LIMIT_REPORT` 覆盖，`<=0` 关闭。限速只是扫描抑制，不是身份边界。

**curl**（模拟 iPXE 上报；引导链走**裸路径**，不经 `/api/cp` 前缀，且无鉴权）：

```bash
curl "https://<host>/devices/report?mac=000c29b98b2d&uuid=4c4c4544-...&manufacturer=ASUSTeK%20COMPUTER%20INC.&product=ROG%20Zephyrus%20G15&cpumodel=Intel(R)%20Core(TM)%20Ultra%207%20155H&mem-total=0x8000&mem-type=DDR5&mem-speed=5600&chip=RTL8125&busid=0110ec8125&ak_public=<SPKI-DER-hex>&ak_name=<TPM2B-NAME-hex>&ak_type=ecc_p256&sb=1"
```

### POST /devices/{mac}/bind

绑定设备到 Worker（一对一授权）。设备或 Worker 已绑定时默认 **409**。

`force=true` 执行**原子换绑**：预校验 → 新绑定落盘 → 旧绑定清除（旧设备回池）→ 失败时恢复台账快照并尽力恢复 dnsmasq。

幂等：同一设备重复绑定同一 Worker 返回 `200` 且不改动。

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

绑定成功后控制面按设备的 `boot_firmware` 写 dnsmasq 静态绑定：`snponly` 写 `MAC,hostname`（走 `snponly.efi`），`full` 写 `MAC,set:full,hostname`（走 `ipxe.efi` 全驱动）。换绑到另一台设备时投影随新设备的 `boot_firmware` 重写。

**curl**：

```bash
curl -X POST "$BASE_URL/devices/00:0c:29:b9:8b:2d/bind?worker_id=worker-01&force=false" \
  -H "Authorization: Bearer $TOKEN"
```

**成功返回（200）**：设备记录（`state=bound`，`bound_worker_id=worker-01`）。

### DELETE /devices/{mac}/bind

解绑设备：回池（`state=pooled`、`bound_worker_id=null`），移除 dnsmasq 绑定（dnsmasq 自监听文件变化）。Worker 的系统盘保留（其 `readiness` 降级为 `partial`/`idle`）。

**409**：设备未绑定。**404**：设备不存在。

**curl**：

```bash
curl -X DELETE "$BASE_URL/devices/00:0c:29:b9:8b:2d/bind" \
  -H "Authorization: Bearer $TOKEN"
```

### POST /devices/bind/batch/preview

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

### POST /devices/bind/batch

批量绑定**执行**（幂等，逐项独立，单项失败不影响其余）。请求体同批量绑定预览。归类：

- `succeeded`：本次绑定成功（申报值与上报值不符时带 `fingerprint_mismatch` 标记）
- `skipped`：已绑定（同 Worker）/ 设备已绑定其他 Worker / Worker 已绑定 / 清单内重复 MAC
- `failed`：设备不存在（池外——先经 `POST /devices` 或批量导入入池）/ MAC 非法

**审计**：除 `device.bind.batch` 汇总外，每个 `succeeded` 项另逐条记录 `device.bind`（`mac` / `worker_id`），保证设备绑定历史（`GET /operations?mac=`）完整；`skipped` / `failed` 仅在汇总计数。

**成功返回**：`{succeeded: [...], skipped: [...], failed: [...]}`。已完成的清单重跑全部 `skipped`。

**curl**：

```bash
curl -X POST "$BASE_URL/devices/bind/batch" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"mode":"manifest","pairs":[{"mac":"00:0c:29:b9:8b:2d","worker_id":"worker-01"}]}'
```

## PUT /devices/{mac}/boot-firmware

切换设备首跳固件（`snponly` / `full`），需 `Authorization: Bearer`。写入设备台账；设备已绑定时同步重投影该 Worker 的 dnsmasq 静态绑定（`full` → `MAC,set:full,hostname`），下次 DHCP 首跳即按新固件下发。

**请求体**：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `boot_firmware` | 是 | str | `snponly`（默认，UEFI SNP）或 `full`（全驱动） |

```bash
curl -s -X PUT "$BASE_URL/devices/00:0c:29:b9:8b:2d/boot-firmware" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"boot_firmware": "full"}'
```

**成功返回（200）**：更新后的设备记录。写入操作日志 `device.boot_firmware`（含新旧值与来源地址）。

**错误**：MAC 非法 `400`；请求体非法 `422`；未知设备 `404`；设备已吊销或绑定的 Worker 记录缺失 `409`；dnsmasq 重投影失败 `500`（台账已回滚为原值）。

---

## POST /devices/{mac}/approve-baseline

批准设备最近一次证明上报的 PCR 基线（**管理员**，需 `Authorization: Bearer`）。

固件或启动配置变化后，quote 的 PCR 摘要与既有基线不符，`/boot-vars` 按 `pcr_mismatch` 拒绝；该摘要写入设备的 `attestation.pending_digest` 等待批准。管理员核对变更后调用本端点，把待批摘要写为新的 `pcr_baseline`，设备下次启动按新基线校验。

无请求体：批准对象固定为该设备最近一次上报的待批摘要，避免管理员手输摘要出错。

**错误**：未知设备 `404`；设备已吊销 `403`；没有待批摘要（如尚未发生不匹配）`409`。

```bash
curl -s -X POST "$BASE_URL/devices/00:0c:29:b9:8b:2d/approve-baseline" \
  -H "Authorization: Bearer $TOKEN"
```

**成功返回（200）**：更新后的设备记录（`pcr_baseline` 为新摘要，`attestation.state=unproven`、`last_reason=baseline_approved`、`pending_digest` 已清除）。写入操作日志 `device.pcr_baseline.approve`（含摘要与来源地址）。

AK 本身不可在线更换（AK 私钥不可导出）：设备恢复或更换主板走删除登记 + 注册窗口重新入池的管理员流程。

---
