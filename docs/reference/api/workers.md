---
title: "Worker"
---

# Worker

Worker 是计算身份。Worker 的创建、查询、状态、删除、换绑与批量操作端点如下。

## POST /workers

### 说明

注册一台 Worker 的**身份**：hostname 绑定。**存储与身份分离**——本接口不创建任何系统盘，系统盘须另调 `POST /workers/{worker_id}/luns/disk`（见[系统盘](./disks.md)）。

`mac` 现为**可选**：

- 不传 `mac` → 纯空转 Worker（仅 hostname 绑定，不授权任何设备），后续可用 `POST /devices/{mac}/bind` 绑定设备
- 传 `mac` → 设备须已在设备池中（`state=pooled`），校验后直接绑定（一对一授权）；设备池外或已绑定 → `409`——**先注册，后绑定**

控制面会：

1. 校验 `worker_id`、`hostname`；传了 `mac` 时校验 `mac`
2. 写入 `state/workers.yml`（`disks` 为空数组，`state=registered`）
3. 传了 `mac` 时绑定设备（写 `state/devices.yml` 的 `bound_worker_id` + 写 `dnsmasq/dhcp-hosts.conf`；dnsmasq 自监听该文件变化，无需 HUP）

### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `worker_id` | 是 | Worker 编号。会自动转为小写。允许字母、数字、点、下划线、短横线 |
| `mac` | 否 | Worker 网卡 MAC 地址，格式如 `00:0c:29:b9:8b:2d`。传入时设备须已在设备池中（见[设备池](./devices.md)），本调用同时完成绑定 |
| `hostname` | 否 | 主机名。不传时默认等于 `worker_id` |
| `arch` | 否 | 架构。不传时默认 `x86_64` |
| `boot` | 否 | iPXE 菜单默认项与超时配置；不传则由 `/boot-vars` 按默认启动盘和全局默认值推导。与 `PUT /workers/{worker_id}/default-disk` 写的是同一组台账字段，后设覆盖先设 |

### `boot` 字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `menu_default` | 否 | iPXE 主菜单默认项（菜单超时后自动选中启动），合法值见[系统盘](./disks.md)合法值表；不区分大小写，如 `config`、`reboot`、`exit` |
| `menu_timeout` | 否 | iPXE 菜单超时，单位毫秒，如 `5000`；传 `0` 表示菜单无限等待、永不自动选择 |

不传 `boot` 时：

- `menu_default` 默认使用 `default_disk`（首块盘自动设为默认；多盘用[设置默认盘](./disks.md)指定）；未设置时默认 `reboot`（循环重启等待配置，见[引导链](./boot.md)）；
- `menu_timeout` 已配置默认启动时默认使用 `spec.boot.menuTimeoutMs`（当前 `5000`）；处于 `reboot` 循环时固定用 `spec.boot.autoBootTimeoutMs`（当前 `1`，见[引导链](./boot.md)）。

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

创建系统盘后，调用 `PUT /workers/{worker_id}/default-disk`（见[系统盘](./disks.md)）设置默认启动盘（os_tag 引用具体盘），`menu-default` 随即切换为通用 OS 项 `boot-os`。

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

如需默认进入 iPXE 配置界面，可以这样传：

```json
{
  "worker_id": "worker-01",
  "mac": "00:0c:29:b9:8b:2d",
  "boot": {
    "menu_default": "config",
    "menu_timeout": 3000
  }
}
```

## PUT /workers/{worker_id}/mac

### 说明

修改指定 Worker 的 **MAC 地址绑定**（hostname 不变），内部映射为**设备换绑**。绑定关系以设备台账 `state/devices.yml` 为权威，`dnsmasq/dhcp-hosts.conf` 只是派生投影。控制面会：

1. 校验 Worker 存在（不存在时返回 `404`）
2. 校验新 MAC 格式（非法返回 `400`）与设备状态：新 MAC 须已登记，且为池中设备（`pooled`）或已绑定到本 Worker（幂等，`changed=false`）；未登记、已吊销或已绑定其他 Worker 返回 `409`
3. 从设备台账反查当前绑定到该 Worker 的旧设备（**不依赖 dnsmasq 绑定**）：旧设备回池，新设备绑定到该 Worker
4. 写 `dnsmasq/dhcp-hosts.conf`：该 hostname 已有绑定则替换，没有则补写（dnsmasq 自监听文件变化，无需 HUP）；投影写入失败则回滚台账
5. **审计记录 `worker.mac.update`（含 `old_mac` / `new_mac` / `changed` / `client`）**——即修改历史，可通过 `GET /operations` 查询；台账未发生绑定变更（如重复设置同一 MAC）时 `changed=false`，此时只做投影补写

> **注意**：`dnsmasq/dhcp-hosts.conf` 是设备台账的派生视图（`MAC -> hostname`），不是 MAC 的权威来源。即使绑定文件缺失或与台账不一致，本端点也能按台账完成换绑并补写投影。

改完后 Worker 需重新获取 DHCP 租约（重启网卡 / 重新 PXE 启动）才会使用新 MAC 对应身份。

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

返回该 Worker 的完整台账（`mac` 字段来自设备台账的绑定反查）。

### 常见错误

| HTTP 状态码 | 常见原因 |
|---:|---|
| `400` | `mac` 格式非法 |
| `401` | 缺少 Token 或 Token 错误 |
| `404` | Worker 不存在 |
| `409` | 新设备未登记 / 已吊销 / 已绑定其他 Worker / dnsmasq 投影冲突（该 MAC 已绑定其他 hostname） |
| `500` | 设备台账或 dnsmasq 绑定写入失败（台账已回滚） |

---

## POST /workers/batch

### 说明

批量创建 Worker（**逐项独立**，单项失败不影响其余；**幂等**，重复执行不产生重复 Worker）。

按 `name_prefix` + 序号生成 `worker_id`（`worker-01`、`worker-02` …，序号从 `01` 起，位宽随 `count` 自适应——`count=100` 时生成 `worker-001` … `worker-100`）。

- 不传 `macs` → 全部为**纯空转** Worker（仅 hostname 绑定，不授权任何设备），后续可用 `POST /devices/{mac}/bind` 绑定
- 传 `macs`（须与 `count` 等长）→ 逐项校验设备池并直接绑定（语义同 `POST /workers` 传 `mac`：设备须 `state=pooled`，池外 / 已绑定 → 该项 `failed` 且**该项不创建**，可修正后重试）

### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `count` | 是 | 创建数量，1–100 |
| `name_prefix` | 否 | Worker 编号前缀，默认 `worker-`。生成的 `worker_id` 须合法（允许字母、数字、点、下划线、短横线），非法前缀整批返回 `400` |
| `macs` | 否 | MAC 地址数组（格式 `00:0c:29:b9:8b:2d`），提供时长度必须等于 `count`，逐项校验并直接绑定 |
| `arch` | 否 | 架构。不传时默认 `x86_64` |
| `boot` | 否 | iPXE 菜单默认项与超时配置，字段同 `POST /workers` |

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

## GET /workers

### 说明

列出当前所有 Worker 台账。返回结果中的 `mac` 字段来自设备台账（`state/devices.yml` 中绑定到该 Worker 的设备）的反查。`dnsmasq/dhcp-hosts.conf` 只是派生投影。

### curl

```bash
curl -s "$BASE_URL/workers" \
  -H "Authorization: Bearer $TOKEN"
```

### 成功返回示例

```json
[
  {
    "hostname": "worker-01",
    "arch": "x86_64",
    "state": "ready",
    "disks": [
      {
        "agent": "a1b2c3d4e5f6",
        "nqn": "nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",
        "iqn": "iqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",
        "filename": "worker-01.ubuntu.0d26b6f33a89.img",
        "backing": "/home/iscsi_img/worker-01.ubuntu.0d26b6f33a89.img",
        "os": "ubuntu",
        "os_version": "22.04",
        "os_tag": "0d26b6f33a89",
        "remark": "",
        "source": {
          "type": "empty",
          "size": "40G"
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
]
```

---

## GET /workers/{worker_id}

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

## GET /workers/{worker_id}/status

### 说明

查询 Worker 的台账信息，并实时检查：

- `dnsmasq/dhcp-hosts.conf` 中是否存在该 Worker 的 `MAC,hostname` 投影（缺失只表示派生物不一致，绑定权威仍是设备台账）
- Agent 上对应的 disk target 是否存在（按盘 NQN / 派生 IQN 匹配）
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
        "agent": "a1b2c3d4e5f6",
        "nqn": "nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",
        "iqn": "iqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",
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
          "nqn": "nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",
          "iqn": "nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",
          "luns": [
            {
              "lun": 1,
              "backing": "/home/iscsi_img/worker-01.ubuntu.0d26b6f33a89.img"
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

## GET /workers/{worker_id}/disks/status

### 说明

逐盘运行时状态：导出在线、凭据生效、活跃会话、盘占用（逻辑大小 + 物理分配）。盘可能分布在不同 Agent，按盘所属 Agent 分组取状态；凭据为 Worker 级关联展示。进入 WebUI 的 disks Tab 时调用。

### Path 参数

| 参数 | 必填 | 说明 |
|---|---:|---|
| `worker_id` | 是 | Worker 编号 |

### curl

```bash
curl -s "$BASE_URL/workers/worker-01/disks/status" \
  -H "Authorization: Bearer $TOKEN"
```

### 成功返回示例

```json
{
  "worker_id": "worker-01",
  "credential": { "worker_id": "worker-01", "exists": true, "secret_hash": "d3f4a9c1b2e3d4e" },
  "disks": [
    {
      "os_tag": "0d26b6f33a89",
      "os": "ubuntu",
      "nqn": "nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",
      "iqn": "iqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",
      "exported": true,
      "hosts": ["nqn.2026-07.com.kurrent:host.worker-01"],
      "controllers": ["nqn.2026-07.com.kurrent:host.worker-01"],
      "size": 42949672960,
      "physical_bytes": 12884901888,
      "error": null
    }
  ]
}
```

### 字段说明

- `exported`：子系统已挂到数据面端口（导出生效）。
- `hosts`：已登记 DHHC-1 认证的 Host NQN（准入白名单）。
- `controllers`：当前活跃的 NVMe/TCP 会话（设备正连接）。
- `size` / `physical_bytes`：盘文件逻辑大小与物理分配（`st_blocks×512`；reflink 共享会重复计数，不代表独占空间）。
- `error`：该盘所属 Agent 不可达时的错误；盘文件缺失时占用字段为 `null`。

## DELETE /workers/{worker_id}

### 说明

删除 Worker。控制面会：

1. 从 `workers.yml` 找到该 Worker 的 disk/cd 台账
2. **吊销凭据推送**：向持有其盘的已登记 Agent 推送吊销（Agent 摘除 hosts 登记并清缓存）——删除前唯一一次凭据推送
3. **联动解绑设备**：所有 `bound_worker_id` 为该 Worker 的设备回池（`state=pooled`、`bound_worker_id=null`，设备**不吊销**）——先解绑落盘，解绑失败则中止删除
4. 如果存在 cd target，先删 cd
5. 再删 disk target
6. 从 `workers.yml` 删除该 Worker
7. **清空该 Worker 的凭据痕迹**：删掉 `state/credentials.yml` 的密钥条目与该 Worker 的推送台账（审计 `credential.forget`）。Worker 已不存在，若留下孤儿条目它既不可见（`GET /credential` 对不存在的 Worker 返回 404）也不可删，且同 id 的 Worker 重建时会**静默继承旧密钥**并把旧钥推给新机器的盘
8. 从 `dnsmasq/dhcp-hosts.conf` 删除 `mac,hostname` 这一行（dnsmasq 自监听文件变化，无需 HUP）

`POST /workers/delete/batch` 同样联动解绑并清理凭据痕迹。

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

## POST /workers/delete/batch

### 说明

批量删除 Worker。每项独立执行，**单项失败不影响其余**，返回 `succeeded` / `failed` 汇总。

每个 Worker 的处理与单删（`DELETE /workers/{worker_id}`）一致（删 CD/系统盘 target → 移台账 → 移除 dnsmasq 绑定），全部成功项统一保存台账并**只 reload 一次** dnsmasq。不存在的 Worker 计入 `failed`（`worker not found`）。

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
