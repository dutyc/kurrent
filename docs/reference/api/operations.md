---
title: "运维与验证"
---

# 运维与验证

操作日志端点如下，后附一条从健康检查到建盘启动的调用顺序。

## GET /operations

### 说明

读取控制面的操作流水。这个文件是 `state/operations.jsonl` 的增量查询接口。

### Query 参数

| 参数 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `since` | 否 | `0` | 只返回 `id > since` 的记录 |
| `limit` | 否 | `1000` | 最多返回多少条 |
| `mac` | 否 | — | 仅返回该设备（MAC）的操作（规范化后按 `mac` 字段过滤，先过滤再计 `limit`），用于设备绑定记录查看 |
| `op` | 否 | — | 仅返回该操作类型（如 `platform.enroll`、`device.bind`），先过滤再计 `limit` |
| `order` | 否 | `asc` | `asc` 按写入顺序返回；`desc` 只取最新一页并按新到旧返回（概览类查询用），`next_cursor` 为该页最新条目的 `id` |

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

读取最新一页（新到旧）：

```bash
curl -s "$BASE_URL/operations?limit=10&order=desc" \
  -H "Authorization: Bearer $TOKEN"
```

按操作类型过滤（平台注册记录）：

```bash
curl -s "$BASE_URL/operations?op=platform.enroll&order=desc" \
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
      "agent": "a1b2c3d4e5f6",
      "iqn": "iqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89"
    }
  ]
}
```

---

## 典型测试顺序

以下调用顺序覆盖从健康检查到建盘启动的主要端点（示例使用 `worker-01` 与设备 `00:0c:29:b9:8b:2d`）。

### 查服务存活

```bash
curl -s "$BASE_URL/healthz"
```

### 查引导链状态与启动变量投影

```bash
curl -s "$BASE_URL/boot-assets" -H "Authorization: Bearer $TOKEN"
curl -s "$BASE_URL/boot-vars?mac=000c29b98b2d&hostname=worker-01"
curl -s "$BASE_URL/boot-vars?mac=000c29b98b2d&hostname=worker-01&format=json"
```

### 查 Agent 配置和能力

`config/agents.yml` 由控制面容器内部读取。Agent 如果和控制面在同一台宿主机上，不能写 `http://localhost:4840`——容器里的 `localhost` 指向控制面容器自己。

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

### 准备设备并注册 Worker

设备必须先进入设备池（自动入池见[引导链](./boot.md)，手动登记如下）：

```bash
curl -s -X POST "$BASE_URL/devices" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "mac": "00:0c:29:b9:8b:2d"
  }'
```

再注册 Worker 并直接绑定该设备：

```bash
curl -s -X POST "$BASE_URL/workers" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "worker_id": "worker-01",
    "mac": "00:0c:29:b9:8b:2d"
  }'
```

此时 Worker 已绑定设备，但还没有系统盘（`state=registered`，`disks` 为空数组，`readiness=partial`）。

### 给 worker-01 创建系统盘（空白盘）

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

系统盘创建完成后 `state` 由 `registered` 转为 `ready`；首块盘自动写入 `default_disk`，响应中的 `os_tag` 即该盘的默认启动标识。

### 设置默认启动配置（可选）

首块盘已自动设为默认盘，通常无需再调用。多盘并存或需要覆盖菜单项/超时时：

```bash
# disk = 建盘响应中的 os_tag（12 位 hex 随机标识，精确引用具体盘）
curl -s -X PUT "$BASE_URL/workers/worker-01/default-disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "disk": "5f1c2a3b4d5e"
  }'
```

此时 `/boot-vars` 的 `menu-default` 返回 `boot-os`（MAIN MENU 的 OS 项已收敛为唯一通用项）。菜单项与超时的设置示例见[系统盘](./disks.md)。

### 查询 Worker 台账

```bash
curl -s "$BASE_URL/workers/worker-01" \
  -H "Authorization: Bearer $TOKEN"
```

### 查询实时状态

```bash
curl -s "$BASE_URL/workers/worker-01/status" \
  -H "Authorization: Bearer $TOKEN"
```

### 删除 Worker，但保留空白盘文件

```bash
curl -s -X DELETE "$BASE_URL/workers/worker-01?delete_disk=false" \
  -H "Authorization: Bearer $TOKEN"
```

删除会联动解绑设备（回池，不吊销）并清理该 Worker 的凭据条目；保留 `.img` 正好适合“空白盘制作完成后，人工改名为母盘”的工作流。

---
