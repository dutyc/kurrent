---
title: "系统盘与凭据"
---

# 系统盘与凭据

系统盘由 Worker 持有。建盘、批量建盘、默认盘、删盘与 NVMe-oF 凭据端点如下。

## POST /workers/{worker_id}/luns/disk

### 说明

给指定 Worker 创建系统盘 LUN。系统盘按 (os, os_version) 分类，一个 Worker 可挂多个系统的盘（同一系统同一版本至多一块，不同版本可并存——同系统多版本靠盘级 `os_tag` 随机标识区分）。

控制面会：

1. 校验 Worker 存在且尚未挂载该 (os, os_version) 的盘（已存在时返回 `409`）
2. 生成盘级随机标识 `os_tag`（12 位 hex，数据面唯一键）——决定盘 NQN 后缀与文件名
3. 选择存储 Agent（`disk_agent` 指定；不传则交给放置策略：候选须启用且数据面就绪，`type=master` 时还要求该节点本地存在这块母盘——无可用候选返回 `503`，`detail.excluded` 逐节点说明原因）
4. 拼接盘 NQN（`base-nqn:worker-id.os.<os_tag>`）与 backing filename（`worker-id.os.<os_tag>.img`）
5. 调用 Agent 创建磁盘 target（母盘克隆或空白盘）
6. 更新 `state/workers.yml` 中该 Worker 的 `disks` 台账（追加到数组），首次建盘时 `state` 由 `registered` 转为 `ready`；**该盘同时写入 `default_disk`**（Worker 只有这一块盘，无需再调 default-disk）
7. **建盘即签发**：盘落在 nvmet 后端节点时，若该 Worker 尚无认证密钥，控制面就地生成 DHHC-1 密钥并推送（审计 `credential.issue`，见本节）——已有密钥不轮换（按 Worker 跟盘，多盘共用一把）；非 nvmet 后端不签发（DHHC-1 是 NVMe-oF 专属，iSCSI 走 CHAP）

端点位于 `/luns/` 命名空间下，为将来数据盘（`/luns/data`）预留。

多盘场景下，默认启动哪个盘由 `PUT /workers/{worker_id}/default-disk` 的 `disk`（os_tag）决定（第二块及以后的盘不会自动改写默认盘）。

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
| `os` | 是 | 系统备注（人类理解用，决定盘 NQN/文件名中的 os 段）。小写归一后校验：首字符为字母或数字，其余仅小写字母、数字、点、下划线、短横线，最长 32 字符；不做发行版枚举 |
| `os_version` | 否 | 版本备注（`''` = 无版本，默认）；同一 (os, os_version) 至多一块，不同版本可并存 |
| `disk_agent` | 否 | 指定存储 Agent；不传时由控制面放置（候选含数据面就绪与母盘本地存在性校验） |

### 从母盘克隆

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

### 创建空白盘

#### curl

```bash
curl -s -X POST "$BASE_URL/workers/worker-01/luns/disk" \
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
      "agent": "a1b2c3d4e5f6",
      "nqn": "nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",  # NVMe-oF 数据面标识（权威，NQN 不用 IQN 定义；后缀带 os_tag）
      "iqn": "iqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",  # iSCSI 数据面标识（由 NQN 派生）
      "filename": "worker-01.ubuntu.0d26b6f33a89.img",
      "backing": "/home/iscsi_img/worker-01.ubuntu.0d26b6f33a89.img",
      "os": "ubuntu",
      "os_version": "22.04",
      "os_tag": "0d26b6f33a89",
      "remark": "",
      "source": {
        "type": "master",
        "name": "_tpl_ubuntu_2204.img"
      }
    }
  ],
  "cd": null,
  "default_disk": "0d26b6f33a89",
  "worker_id": "worker-01",
  "mac": "00:0c:29:b9:8b:2d",
  "bound_device": "00:0c:29:b9:8b:2d",
  "readiness": "ready"
}
```

### 成功返回示例（empty 空白盘）

```json
{
  "hostname": "worker-01",
  "arch": "x86_64",
  "state": "ready",
  "disks": [
    {
      "agent": "a1b2c3d4e5f6",
      "nqn": "nqn.2026-07.com.kurrent:worker-01.ubuntu.5f1c2a3b4d5e",
      "iqn": "iqn.2026-07.com.kurrent:worker-01.ubuntu.5f1c2a3b4d5e",
      "filename": "worker-01.ubuntu.5f1c2a3b4d5e.img",
      "backing": "/home/iscsi_img/worker-01.ubuntu.5f1c2a3b4d5e.img",
      "os": "ubuntu",
      "os_version": "",
      "os_tag": "5f1c2a3b4d5e",
      "remark": "",
      "source": {
        "type": "empty",
        "size": "40G"
      }
    }
  ],
  "cd": null,
  "default_disk": "5f1c2a3b4d5e",
  "worker_id": "worker-01",
  "mac": "00:0c:29:b9:8b:2d",
  "bound_device": "00:0c:29:b9:8b:2d",
  "readiness": "ready"
}
```

### 批量创建系统盘（POST /workers/luns/disk/batch）

批量部署场景：同一套盘参数应用到多个 Worker。**定位方式二选一**（同时给 → `400`）：

- `targets`：**逐项点名**存储节点（显式覆盖，原有调用方式）；
- `workers` + `placement`：只声明要给哪些 Worker 建盘，**存储节点由控制面放置**。

放置（`workers` 方式）的语义——放置是系统决策，调用方给意图与约束：

- **候选节点** = 磁盘角色 ∧ 启用（非排空）∧ 有 `base_url` ∧ **数据面就绪**（`healthz.backend_ready` 非 `false`）∧ 在 `placement.candidates` 内（留空 = 不限制）∧（`type=master` 时）**母盘在该节点本地存在**——克隆在节点本地完成，节点没有这块母盘就不可能建成；
- **分配** = 现有盘数最少优先（均摊），同负载按注册顺序；
- `placement.pins` 逐 Worker 钉死（显式覆盖）：被钉的节点**不要求在 `candidates` 内**，但须自身可用，否则 `503`；
- 无任何可用候选 → `503`，`detail.excluded` 逐节点给出被排除的原因（角色不符 / 排空 / 不可达 / 数据面未就绪 / 缺母盘）。

`dry_run=true` 只返回计划（`plan` / `warnings`）不建盘。**预览与执行共用同一份放置实现**，所以 `plan` 里的落点即随后执行的结果（逐行 `action` 为 `create` / `skip` / `error`）。

注意 `targets` 方式的计划**不探测节点**（只按台账判断 create/skip/error），`workers` 方式的候选则在放置阶段逐个探过。

与单盘一致：`master` 走母盘克隆、`empty` 建空白盘；同一 (os, os_version) 至多一块，已存在则**自动跳过**（不算失败）。**创建成功的 Worker 自动将 `default_disk` 设为本次批量盘的 `os_tag`**——批量部署直接进入默认启动，无需再调 `PUT /workers/{worker_id}/default-disk`（单盘接口在首块盘时同样自动设置）。落在 nvmet 后端节点的 Worker 同样**建盘即签发**认证密钥（各 Worker 密钥互不相同，一次到位，见本节建盘流程与「NVMe-oF 认证凭据」）。

逐项独立执行，单项失败不影响其余，返回 `succeeded` / `skipped` / `failed` 汇总。目标 Agent 不存在或已停用（排空）的项进入 `failed`（`agent not found` / `agent ... disabled`），其余项照常。

#### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `type` | 是 | `master` 或 `empty` |
| `os` | 是 | 系统备注（同一批次所有 Worker 相同，决定盘 NQN/文件名中的 os 段；小写归一后校验：首字符为字母或数字，其余仅小写字母、数字、点、下划线、短横线，最长 32 字符） |
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
    "placement": { "candidates": ["a1b2c3d4e5f6", "b2c3d4e5f601"] },
    "dry_run": true
  }'
```

#### 计划返回（dry_run）

```json
{
  "dry_run": true,
  "plan": [
    { "worker_id": "worker-01", "agent": "a1b2c3d4e5f6", "action": "create" },
    { "worker_id": "worker-02", "agent": "b2c3d4e5f601", "action": "create" },
    { "worker_id": "worker-03", "agent": "a1b2c3d4e5f6", "action": "skip",
      "reason": "already has a ubuntu system disk" }
  ],
  "warnings": [
    { "agent": "e5f601234567", "reason": "does not have master _tpl_ubuntu_2204.img" }
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
      { "worker_id": "worker-01", "agent": "a1b2c3d4e5f6" },
      { "worker_id": "worker-02", "agent": "a1b2c3d4e5f6" },
      { "worker_id": "worker-03", "agent": "e5f601234567" }
    ]
  }'
```

#### 返回示例

```json
{
  "succeeded": [
    { "worker_id": "worker-01", "agent": "a1b2c3d4e5f6", "iqn": "iqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89" },
    { "worker_id": "worker-03", "agent": "e5f601234567", "iqn": "iqn.2026-07.com.kurrent:worker-03.ubuntu.a1b2c3d4e5f6" }
  ],
  "skipped": [
    { "worker_id": "worker-02", "reason": "already has a ubuntu 22.04 system disk" }
  ],
  "failed": [
    { "worker_id": "worker-04", "agent": "a1b2c3d4e5f6", "error": "worker not found: worker-04" }
  ]
}
```

---

## 注册与建盘常见错误

本节汇总 Worker 注册与系统盘创建（含批量）的常见错误。

| HTTP 状态码 | 常见原因 |
|---:|---|
| `400` | 参数格式错误；`os` 为空或含非法字符；`type=master` 却没传 `name`；`type=empty` 却没传 `size` |
| `401` | 缺少 Token 或 Token 错误 |
| `404` | 创建系统盘时 Worker 不存在；`disk_agent` 指定的 Agent 不存在 |
| `409` | `worker_id` 已存在；`hostname` 已存在；MAC 已绑定；Worker 已有该 (os, os_version) 系统盘（同版本重复创建）；Agent 上 IQN 已存在；backing 文件已存在；目标 Agent 已停用（排空：`agent ... disabled: not accepting new disks`） |
| `500` | dnsmasq 绑定写入失败；写文件失败；其他未预期错误 |
| `503` | Agent 不可达 |

---

## PUT /workers/{worker_id}/default-disk

### 说明

**「默认启动盘」是干什么的**：一台 Worker 可以挂多块系统盘（同一系统同一版本至多一块，如 `ubuntu 22.04` + `windows`，同系统不同版本可并存）。每次开机，iPXE 菜单在超时后会自动选中某一项启动——默认情况下选本端点的默认启动盘（`boot-os`），它同时决定 `/boot-vars` 投影哪块盘的连接信息（`disk_nqn` / `storager_ip` 取默认启动盘，见[引导链](./boot.md)）；设置了 `menu_default` 覆盖时，自动选中该项，而投影的连接信息仍取默认盘。

不设置时菜单自动选 `reboot`，配合 1 毫秒超时循环重启，等待管理员完成配置，避免静默进错系统。

**注意**：`disk` 是盘级随机标识 `os_tag`（建盘时生成，12 位 hex，数据面唯一键），并非系统名；同系统多版本共存时，只有 os_tag 能精确定位要默认启动的**具体盘**。

`/boot-vars` 的 `menu_default` 推导链：

```text
boot.menu_default（本端点 menu_default 字段，显式覆盖，优先）-> default_disk（本端点 disk 字段）-> reboot（未配置，循环重启等待）
```

请求体三个字段可单独或组合传，至少传一个；传 `null`（或空字符串）表示清除对应项。

可重复调用，后设覆盖先设——与注册时传入的 `boot`（见[Worker](./workers.md)）写的是同一组台账字段。

要求：

- 设置 `disk`：Worker 必须已有该 `os_tag` 对应的系统盘（`POST /workers/{worker_id}/luns/disk` 创建的某块盘），否则返回 `400` 并列出当前系统盘；多盘模型下用 `os_tag` 精确匹配要默认启动的盘
- 设置 `menu_default`：只接受当前主菜单可手工选择的 item ID：`config` / `shell` / `reboot` / `exit`（严格校验，防止 iPXE `choose --default` 落空）；`boot-os` 由默认盘推导，不接受手工设置
- 设置 `menu_timeout`：非负整数；清除后恢复默认 `spec.boot.menuTimeoutMs`

### Path 参数

| 参数 | 必填 | 说明 |
|---|---:|---|
| `worker_id` | 是 | Worker 编号 |

### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `disk` | 否 | 默认启动盘的 `os_tag`（12 位 hex，精确引用具体盘），须与该 Worker 已挂盘一致；传 `null` 清除 |
| `menu_default` | 否 | iPXE 主菜单默认项（菜单超时后自动选中），见下方合法值表；不区分大小写（自动转小写）；传 `null` 清除 |
| `menu_timeout` | 否 | 菜单超时毫秒数，非负整数；传 `0` 表示菜单无限等待、永不自动选择（等人工按键）；传 `null` 清除，恢复默认 `spec.boot.menuTimeoutMs`（当前 `5000`） |

### `menu_default` 合法值（menu.ipxe 主菜单 item ID）

| 类别 | 合法值 |
|---|---|
| 通用 OS 项 | `boot-os` 由默认盘配置推导，**不接受手工设置** |
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
curl -s -X PUT "$BASE_URL/workers/worker-01/default-disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "menu_default": "config",
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

---

## DELETE /workers/{worker_id}/luns/disk/{os_tag}

### 说明

删除指定 Worker 的单个系统盘（按盘级随机标识 `os_tag`，12 位 hex）。控制面会：

1. 校验 Worker 存在且已挂载该 `os_tag` 对应的盘（不存在时返回 `404`）
2. 调用该盘所在 Agent 删除 target（nvmet 子系统 / iSCSI target）
3. 从 `state/workers.yml` 的 `disks` 数组中移除该盘记录
4. 联动清理：被删盘若为默认启动盘（`default_disk` 等于该盘 `os_tag`），一并清除 `default_disk` 与同名（同 os）的 `boot.menu_default`（防止 iPXE 启动到已删除的系统盘）；**若删后只剩一块盘，该盘成为默认启动盘**
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

返回该 Worker 的完整台账（`disks` 已不含被删系统盘；若为默认盘，`default_disk`/`boot.menu_default` 已被清除，只剩一块盘时该盘自动成为默认启动盘；无盘时 `state=registered`）。

### 常见错误

| HTTP 状态码 | 常见原因 |
|---:|---|
| `400` | `os_tag` 非法（非 12 位 hex） |
| `401` | 缺少 Token 或 Token 错误 |
| `404` | Worker 不存在，或该 Worker 没有此 `os_tag` 对应的盘 |

---

## POST /workers/luns/disk/batch-delete

### 说明

批量删除系统盘：`targets` 逐项点名 `{worker_id, os_tag}`，或 `workers` 删除这些 Worker 的全部盘，二选一。`dry_run=true` 只返回计划不删盘；`delete_file` / `ignore_missing_target` 与单删一致。逐项独立执行，返回 `succeeded` / `skipped` / `failed` 汇总（盘或 worker 已不存在按 skipped）。

### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `targets` | 二选一 | 逐项 `{worker_id, os_tag}` |
| `workers` | 二选一 | Worker 列表，删除其全部系统盘 |
| `delete_file` | 否 | 同时删除盘 `.img` 文件，默认 false |
| `ignore_missing_target` | 否 | 容忍 Agent 侧 target 已缺失，默认 false |
| `dry_run` | 否 | 只返回计划，默认 false |

### curl

```bash
curl -s -X POST "$BASE_URL/workers/luns/disk/batch-delete" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"workers": ["worker-01", "worker-02"], "dry_run": true}'
```

### 成功返回示例

```json
{
  "succeeded": [{ "worker_id": "worker-01", "os_tag": "0d26b6f33a89", "os": "ubuntu" }],
  "skipped": [{ "worker_id": "worker-02", "os_tag": null, "reason": "worker not found" }],
  "failed": []
}
```

`dry_run=true` 时返回 `{"dry_run": true, "plan": [{worker_id, os_tag, action, reason}]}`，`action` 为 `delete` / `skip` / `error`。

## PUT /workers/default-disk/batch

### 说明

批量设默认启动盘：`targets` 逐项 `{worker_id, os_tag}`，或 `workers` + 按 `os`[/`os_version`] 匹配各自一块盘，二选一。只改 `default_disk`；菜单覆盖（`menu_default` / `menu_timeout`）不在批量范围——已有 `menu_default` 覆盖的 Worker 仍按覆盖项启动，需先清除覆盖（`PUT /workers/{worker_id}/default-disk` 传 `{"menu_default": null}`）。`dry_run=true` 只返回计划。已是默认按 skipped，匹配失败/盘不存在按 failed。

### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `targets` | 二选一 | 逐项 `{worker_id, os_tag}` |
| `workers` | 二选一 | Worker 列表；`os` 必填用于匹配 |
| `os` | workers 模式必填 | 系统名（台账备注），匹配该 Worker 的盘 |
| `os_version` | 否 | 版本；空表示不限版本（要求该 os 下唯一） |
| `dry_run` | 否 | 只返回计划，默认 false |

### curl

```bash
curl -s -X PUT "$BASE_URL/workers/default-disk/batch" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"workers": ["worker-01", "worker-02"], "os": "ubuntu", "os_version": "24.04"}'
```

### 成功返回示例

```json
{
  "succeeded": [{ "worker_id": "worker-01", "os_tag": "0d26b6f33a89" }],
  "skipped": [{ "worker_id": "worker-02", "os_tag": "1a2b3c4d5e6f", "reason": "already default" }],
  "failed": [{ "worker_id": "worker-03", "os_tag": null, "error": "no disk for ubuntu 26.04" }]
}
```

## NVMe-oF 认证凭据（/workers/{worker_id}/credential）

### 说明

NVMe-oF 连接认证密钥库（按 Worker 跟盘）：密钥按 `worker_id` 索引，绑定到该 Worker 的设备共享同一密钥（换绑即换钥）。密钥为 **DHHC-1** 格式：`DHHC-1:<两位数字类型>:<base64(密钥 + CRC32 小端 4 字节)>`，密钥体 32 字节（SHA-256）或 64 字节（SHA-512）。

**签发时机：建盘即签发**——在 nvmet 后端节点上创建系统盘时（建盘流程第 7 步，单盘与批量相同），控制面为尚无密钥的 Worker 就地生成 DHHC-1 密钥并推送（审计 `credential.issue`）：子系统与它配套的认证密钥同时就位，批量建盘 N 台即 N 份密钥一次到位，无需逐台手工设置。已有密钥的 Worker 建盘**不轮换**（按 Worker 跟盘，多盘共用一把）。非 nvmet 后端（stgt / lio）不签发——DHHC-1 是 NVMe-oF 专属。因此本节的手工端点用途收窄为**显式轮换 / 吊销**（吊销即明确「不用认证」，其条目被删，不会被后续建盘复活）。

凭据写入后控制面会**推送期望状态给持有该 Worker 系统盘的 Agent**（审计 `credential.push`，不记密钥本体）。推送体含明文密钥与**由它派生的 `tls_psk`**：Agent 除登记 hosts 外，还转调宿主服务 `PUT /subsystems/{nqn}/tls` 把 PSK 注入内核全局 `.nvme` keyring；吊销时一并摘除。

Agent 转调存储节点 nvmet 宿主服务登记 hosts（子系统 NQN = 盘 NQN——盘标识权威 = NQN，IQN 由 NQN 派生（`iqn.` + nqn[4:]），不作子系统 NQN 使用；NVMe Base Spec §7.9 要求 `nqn.` 前缀）。**Host NQN = worker 派生** `nqn.<…>:host.<worker_id>`，base 取盘 NQN 前缀 = 节点侧 `spec.agent.nqnBase`，由 `/boot-vars` 注入固件覆盖其内置默认值（固件默认按 SMBIOS UUID 派生，不覆盖必认证失败）。

**推送对账**：每次推送的逐 Agent 结果记录在 `state/credential-push.yml`（键 = `worker_id/agent_id`；含所推密钥的审计哈希与单调 `generation`，不含明文）。控制面对账线程周期（默认 60s，`KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL` 秒，0 = 禁用）比对「期望 × 记录」：密钥哈希一致且**逐对应用已确认**才跳过，否则幂等重放——覆盖推送失败未重试、进程崩溃缝隙（台账已改未推送）与吊销未送达三类静默缺口。

`ok` 不代表 HTTP 200：Agent 必须回报 `tls_expected`（子系统数 × host 数）与 `tls_applied`，二者相等才算送达。旧 Agent 不回报应用结果、只部分应用或目标数不一致一律记 `failed`（fail-closed，不会把「已下发」误当「已生效」）。Agent 回报中 `tls_unavailable`（目标端 TLS 端口未就绪，409）覆盖全部目标时记 `degraded`——认证密钥已写、传输加密 PSK 未生效，仍不能引导；对账持续重推，端口就绪后自动转 `ok`。

台账为 `ok` 且哈希一致时，对账仍经 Agent 的凭据状态查询回查宿主内核 `.nvme` keyring：设置态要求每个（子系统, host）对的 PSK 都在位，吊销态要求全部不在位，否则重推——宿主机重启会清空 keyring，不能只信「上次推送成功」的台账。

每次推送分配单调 `generation`：Agent 拒绝迟到的旧值，台账也拒绝旧 generation 的回写（乱序/并发重放不会用旧密钥覆盖新密钥）；吊销删除条目后 generation 墓碑保留。删除子系统时宿主服务同步摘除该子系统名下全部 PSK，服务启动时清理 subsystem 已不存在的孤儿 key。

无密钥且无推送历史的对**不推送**、不记台账、不记审计（避免 Agent 离线时出现「同步失败」的虚假故障）——nvmet 建盘已自动签发，实际只剩非 nvmet 后端的盘（DHHC-1 不适用，推送只会得 `n/a`）；一旦有过推送记录，之后的吊销即使未送达也持续重放。Agent 登记被删（登记删除语义）→ 记录剪除，重新登记后自动重推。对账重放仅在状态或密钥变化时记审计（去噪）；用户动作触发的推送照常每次记录。

推送结果取值：

| 结果 | 含义 |
|---|---|
| `ok` | 已送达（Agent 缓存与宿主服务均已同步且逐对应用） |
| `degraded` | 部分生效（认证密钥已写、目标端 TLS 未启用，PSK 未生效） |
| `failed` | 未送达（Agent 不可达 / 宿主同步失败，对账持续重试） |
| `n/a` | 目标 Agent 非 nvmet 后端（凭据不适用，非故障） |
| `pending` | 尚无记录或记录为旧值（待对账重放） |
| `none` | 无同步目标（无 NVMe-oF 盘、凭据从未设置无推送历史，或盘所在 Agent 未登记） |

触发推送的操作：**建盘即签发**（nvmet 后端，见上）、设置/删除凭据、设备绑定/解绑/换绑（`PUT /workers/{worker_id}/mac`）、删盘（含批量）、删除 Worker。

**定期轮换**：对账线程每个周期先检查 DHHC-1 密钥年龄（`rotated_at`，缺省回落到 `updated_at` / `created_at`），超过 `KURRENT_CP_CREDENTIAL_ROTATION_HOURS`（默认 24 小时，`<=0` 关闭）即轮换：生成新密钥 → 推送到该 Worker 的全部目标 Agent → **全部送达后**才在 `state/credentials.yml` 切换（`rotated_at` / `updated_at` 更新，审计 `credential.rotate`）。任一目标失败或降级（TLS 未生效）则把已送达目标回推旧密钥、台账不动，下个周期重试；没有 nvmet 目标的条目跳过。切换前 `/boot-vars` 投影的仍是旧密钥，宿主改新密钥的窗口只覆盖推送本身。

静态存储边界：密钥明文按链路必要范围落盘四处——控制面 `state/credentials.yml`、Agent 容器凭据缓存（含派生的 TLS PSK，均 0600）、存储节点 configfs（内核 hosts 矩阵 `dhchap_key`）、存储节点内核全局 `.nvme` keyring（TLS PSK，运行态）。静态保护责任归部署方（磁盘/宿主持久化加密、root 访问边界）。密钥经 mTLS 组件通道传输，不进入浏览器存储与服务端日志。数据面加密（NVMe/TCP TLS 1.3，固件段与内核段共用端口 `4421`）的投影见[引导链](./boot.md)。

**凭据边界（谁下发、谁自持）**：无盘引导链下发的凭据只有两类——DHHC-1 认证密钥，与由它派生的 TLS-PSK；外加**非秘密**的一次性 nonce（`GET /devices/challenge`，防重放）。设备身份私钥**不下发**：TPM 内的 AK 私钥不可导出，控制面只记录 AK 公钥（`identity.ak_public`）与 PCR 基线。未签名调试固件对控制面 HTTPS 的 TOFU 指纹是首次连接自行固定，不是下发；签名固件的引导 HTTPS 只信任构建时嵌入的 boot CA。组件自举用的 bootstrap token 与 mTLS 证书走 `/enroll`（管理面），与本节引导链凭据不共用通道。iSCSI/stgt 后端认证走 CHAP，不在本链路。

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
      {"agent_id": "d4e5f6012345", "result": "ok", "updated_at": "2026-10-05T15:30:00+08:00"}
    ]
  }
}
```

| HTTP 状态码 | 常见原因 |
|---:|---|
| `422` | DHHC-1 自检失败（前缀/类型位数/base64/长度 36/68/CRC 终值任一不符） |
| `404` | Worker 不存在 |

### DELETE /workers/{worker_id}/credential

删除密钥（吊销该 Worker 名下全部绑定设备的认证资格与传输加密 PSK，下次启动拒绝引导）：

```bash
curl -X DELETE "$BASE_URL/workers/worker-01/credential" -H "Authorization: Bearer $TOKEN"
```

成功返回 `{"worker_id": "worker-01", "deleted": true, "push": {...}}`（`push.result=ok` 表示吊销已送达；未送达会被对账重放，不会静默滞留）；Worker 或条目不存在 → `404`。

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
      {"agent_id": "d4e5f6012345", "result": "ok", "updated_at": "2026-10-05T15:30:00+08:00"}
    ]
  }
}
```

无条目时 `exists=false` 且无 `secret_hash`，`push` 投影仍返回（吊销送达状态可见）。

CLI：`kurrent workers credential show|set|revoke <worker_id>`；WebUI：Worker 详情独立「NVMe-oF 凭据」页（该 Worker 有 NVMe-oF 盘时才出现）。

---
