---
title: "Agent 与存储"
---

# Agent 与存储

存储节点以 Agent 注册。Agent 台账、母盘标签、LUN/target 与母盘清单端点如下。

## GET /agents

### 说明

列出 `config/agents.yml` 里配置的 Agent。默认会实时访问 Agent 的 `/healthz` 和 `/capabilities`。

`health` 三态：

- `ok`：健康。
- `degraded`：agent 在线但数据面未就绪——nvmet 后端为宿主服务不可达或 configfs 未就绪，具体原因见 `error` 字段。
- `error`：agent 不可达或能力探测失败。

`live=false` 时不探测、无 `health` 字段。

身份分层：`id` 为机器身份，只接受 12 位小写十六进制（与盘 `os_tag` 同构；由控制面在签发 bootstrap token 时分配、join 写入节点状态根下的 `storager/state/agent.id`，enroll 自动登记；手工登记见本节）。`remark` 为备注（人类识别用，昵称归控制面台账，WebUI 卡片展示、编辑表单或通过「更新 Agent」修改）。

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
    "id": "a1b2c3d4e5f6",
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
      "backend": "nvmet",
      "cd": false,
      "persistent": "host-native configfs (nvmet-host service)",
      "configfs": "/sys/kernel/config/nvmet",
      "port": {
        "port_id": 1,
        "trtype": "tcp",
        "trsvcid": "4421",
        "tsas": "tls1.3",
        "ready": true
      },
      "base_nqn": "nqn.2026-07.com.kurrent",
      "fs_type": "btrfs",
      "clone": "reflink (FICLONE; xfs requires the reflink feature enabled) -> shutil.copy fallback",
      "empty_disk": "truncate (sparse)"
    }
  }
]
```

---

## POST /agents

### 说明

注册新 Agent：写入 `config/agents.yml`，注册后立即生效（建盘/挂载调度即会纳入该 Agent）。同一 `id` 重复注册返回 `409`。

**推荐流程**：先在 WebUI（或 `POST /agents/probe`，见本节）填写 API 地址并探测，自动获取角色 / 标签 / 数据面地址等参数，确认后调用本接口完成注册；也可直接全参数提交。

### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `id` | 是 | 机器身份：12 位小写十六进制；昵称写 `remark` |
| `base_url` | 是 | Agent 控制面 API 地址，须以 `http://` 或 `https://` 开头，末尾 `/` 自动去除 |
| `storager_ip` | 否 | 数据面地址（业务网段 IP，协议中立）。缺省时回退为 `base_url` 的主机名 |
| `role` | 否 | 角色：`disk`=可建系统盘（存储节点），`cd`=可挂载 ISO（光驱节点）；默认 `{disk: false, cd: false}` |
| `tags` | 否 | 自由标签数组（如 `storage` / `rack-a3`），**纯人工标注 / 展示用**；不参与 `/boot-vars` 连接符推导（后端类型由 `/boot-vars` 实时查 Agent `/capabilities` 判定，见[引导链](./boot.md)） |
| `enabled` | 否 | 是否启用。`false` = **排空**：不再接任何新增盘（自动挑选 / 显式点名 / 直接对该节点建 LUN 一律拒绝），存量盘照常服务、已引导设备不受影响；默认 `true` |
| `remark` | 否 | 备注（人类识别用，展示性质）；默认空串。昵称归控制面台账，enroll/renew 不写入，注册后手工或通过「更新 Agent」设置 |

### curl

```bash
curl -s -X POST "$BASE_URL/agents" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "c3d4e5f60123",
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
  "id": "c3d4e5f60123",
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

## POST /agents/probe

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
| `agent_id` | 否 | 可选；探测不使用该字段 |

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
  "backend": "nvmet",
  "fs_type": "btrfs",
  "base_nqn": "nqn.2026-07.com.kurrent",
  "clone": "reflink (FICLONE; xfs requires the reflink feature enabled) -> shutil.copy fallback",
  "empty_disk": "truncate (sparse)",
  "persistent": "host-native configfs (nvmet-host service)"
}
```

### 错误返回

| 状态码 | 场景 |
|---|---|
| `400` | `base_url` 非 http(s) 开头 |
| `502` | Agent 不可达（`/healthz` 失败）或 `/capabilities` 调用失败（如 mTLS 客户端证书非控制面 CA 签发） |

---

## PUT /agents/{agent_id}

### 说明

更新已有 Agent：覆盖 `config/agents.yml` 中对应条目，保存后立即生效（建盘/挂载调度即用新配置）。`id` 不可改（走路径参数）。

适用场景：停用 / 启用节点（排空）、数据面地址迁移、备注与角色调整。

**自动登记节点（tags 含 `auto`）的 `base_url` 由节点声明管理**（声明权威，见[身份与 PKI](./identity.md)）：传现值可通过（WebUI 编辑保存的常规形态），传不同值返回 `409`。节点搬家请修改节点 `storager/kurrent.yaml` 的 `spec.agent.advertiseUrl` 后重新 `kurrent join`，renew 自动对齐；手工登记节点仍可在控制面改地址。

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
curl -s -X PUT "$BASE_URL/agents/c3d4e5f60123" \
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
  "id": "c3d4e5f60123",
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

> **编辑探测**：编辑场景建议先调 `POST /agents/probe` 验证地址可达再保存——探测经控制面 mTLS 客户端证书鉴权（身份绑定组件 CA），无需 token。自动登记节点的地址只读，探测按现值执行。

---

## 母盘标签（/agents/{agent_id}/masters/{master_name}/tag）

### 说明

母盘标签 = 控制面登记台账（`state/masters.yml`），os / os_version 为**备注性质**（便于人类理解，不做白名单校验）：

- OS 不设合法集合：建盘 `os` 为自由字符串，同系统多版本靠盘级 `os_tag`（12 位 hex 随机标识，数据面唯一键）区分，母盘标签只负责把母盘名 ↔ 系统/版本关联起来
- **登记不校验母盘存在性**（Agent 可离线，台账即权威）；`/masters` 聚合时对已登记条目附加 `os` / `os_version` 字段，未登记不附加
- 建盘选母盘时 WebUI 自动带出标签（备注可再改）；清除标签仅影响后续建盘

### PUT（登记 / 更新）

| 字段 | 必填 | 说明 |
|---|---:|---|
| `os` | 是 | 系统备注（自由字符串，小写归一；人类理解用，不是数据面标识） |
| `os_version` | 否 | 版本备注，默认 `''`（无版本） |

#### curl

```bash
curl -s -X PUT "$BASE_URL/agents/a1b2c3d4e5f6/masters/_tpl_ubuntu_2204.img/tag" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "os": "ubuntu",
    "os_version": "22.04"
  }'
```

#### 成功返回

```json
{ "agent": "a1b2c3d4e5f6", "name": "_tpl_ubuntu_2204.img", "os": "ubuntu", "os_version": "22.04" }
```

### DELETE（清除）

```bash
curl -s -X DELETE "$BASE_URL/agents/a1b2c3d4e5f6/masters/_tpl_ubuntu_2204.img/tag" \
  -H "Authorization: Bearer $TOKEN"
```

返回 `{ "agent": ..., "name": ..., "removed": true|false }`（`removed=false` = 原本就没有标签）。

### 常见错误

| HTTP 状态码 | 常见原因 |
|---:|---|
| `400` | `os` 为空 / 非法字符；`os_version` 含非法字符 |
| `401` | 缺少 Token 或 Token 错误 |
| `404` | Agent 不存在 |

## DELETE /agents/{agent_id}

### 说明

删除 Agent 台账（`config/agents.yml` 条目）并清理该 agent 的母盘标签（`state/masters.yml` 中 masters 下对应条目）。**仅清控制面记录，不拦截、不触碰节点上的证书/盘文件**。

语义（声明权威的推论）：删除是**软删除**——节点仍带声明在跑，下次 renew 会按声明自动重新登记（重新加入无需重新签发 token）；真正下线一台节点须在**节点侧**操作（停容器 / 撤声明）。

附带效应：登记删除后，仍挂在该节点上的盘在节点重新登记前无法投影引导地址（登记即认知），响应以 `disks_hosted` / `affected_workers` 告知（仅提示，不拦截）。

### curl

```bash
curl -s -X DELETE "$BASE_URL/agents/a1b2c3d4e5f6" \
  -H "Authorization: Bearer $TOKEN"
```

### 成功返回

```json
{ "deleted": "a1b2c3d4e5f6", "master_tags_removed": false, "disks_hosted": 3, "affected_workers": ["worker-01", "worker-02"] }
```

### 常见错误

| HTTP 状态码 | 常见原因 |
|---:|---|
| `401` | 缺少 Token 或 Token 错误 |
| `404` | Agent 不存在 |

## Agent LUN/target 管理（NVMe-oF 主路径）

### 说明

控制面 可以直接管理任意 Agent 上的 LUN/target。请求经 控制面 转发到 Agent（mTLS，控制面组件客户端证书绑定内部 CA），因此调用方只需持有 控制面 凭据，无需直接接触 Agent。

数据面后端由节点声明 `spec.agent.backend` 决定：`nvmet`（默认、主路径）的 target 标识是**盘 NQN**；`stgt` / `lio` 是 iSCSI 兼容后端，target 标识是 IQN。

为兼容两套数据面，本组端点沿用 `iqn` 作为请求键：Agent 在 nvmet 后端内部将它还原为 NQN（同后缀前缀变换），列表响应同时返回 `nqn`（nvmet）或 `iqn`（stgt/lio）。

与 Worker 生命周期接口（`POST /workers`、`DELETE /workers/{worker_id}`）的区别：

- Worker 接口面向**台账**：自动生成盘 NQN 与派生 IQN、写 `state/workers.yml`、维护 dnsmasq 派生投影；
- LUN 管理接口面向**数据面直管**：不写任何台账，直接操作 Agent 上的 target，适合母盘管理、手工排障、ISO 临时挂载等场景。

所有接口都需要管理面认证（见[认证与授权](./auth.md)）。Agent 不存在时返回 `404 agent not found`；Agent 不可达时返回 `503`；Agent 侧的业务校验错误（如 NQN 前缀不匹配、文件已存在、target 已存在）会透传其状态码与 `detail`：

```json
{"agent": "a1b2c3d4e5f6", "error": "iqn base mismatch: ..."}
```

### GET /agents/{agent_id}/luns

列出指定 Agent 上的全部 target/LUN。nvmet 后端的 target 标识为 NQN（`nqn` 字段；`iqn` 键保留历史接口契约，与 `nqn` 同值）；stgt 带 `tid` 字段，LIO 为 `targetcli` 解析结果。控制面 原样透传。

#### Path 参数

| 参数 | 必填 | 说明 |
|---|---:|---|
| `agent_id` | 是 | Agent 标识，对应 `config/agents.yml` 的 key |

#### curl

```bash
curl -s "$BASE_URL/agents/a1b2c3d4e5f6/luns" \
  -H "Authorization: Bearer $TOKEN"
```

#### 成功返回示例（nvmet）

```json
[
  {
    "nqn": "nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",
    "iqn": "nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",
    "luns": [
      {
        "lun": 1,
        "backing": "/home/iscsi_img/worker-01.ubuntu.0d26b6f33a89.img"
      }
    ]
  }
]
```

stgt/lio 后端的条目不返回 `nqn`，形如：

```json
[
  {
    "tid": 1,
    "iqn": "iqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",
    "luns": [
      {
        "lun": 0,
        "backing": "/home/iscsi_img/worker-01.ubuntu.0d26b6f33a89.img"
      }
    ]
  }
]
```

### POST /agents/{agent_id}/luns/disk

在指定 Agent 上创建磁盘 LUN。传 `master` 走母盘克隆（优先 btrfs / ZFS(≥2.2) reflink 秒级），传 `size` 建空白盘（sparse）。

错误：Agent 未配置 `role.disk` 时返回 `400 agent ... not configured for disk role`；已停用（排空）时返回 `409 agent ... disabled: not accepting new LUNs`；nvmet 数据面下母盘为 512 字节扇区布局的 GPT 时返回 `400`（并给出 4K 重建指引，见本节 `gpt` 字段）。

#### Path 参数

| 参数 | 必填 | 说明 |
|---|---:|---|
| `agent_id` | 是 | Agent 标识 |

#### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `iqn` | 是 | target 标识的 iSCSI 形态（盘 IQN，由盘 NQN 派生），必须以该 Agent 的派生 IQN base（`nqn_to_iqn(spec.agent.nqnBase)`）为前缀；nvmet 后端内部还原为 NQN |
| `filename` | 否 | backing 文件名；不传时由 Agent 按 IQN 自动生成 |
| `master` | 条件必填 | 母盘文件名（存在 `DISK_DIR` 下），与 `size` 二选一 |
| `size` | 条件必填 | 空白盘大小，如 `40G`，与 `master` 二选一 |

#### curl

```bash
# 从母盘克隆
curl -s -X POST "$BASE_URL/agents/a1b2c3d4e5f6/luns/disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "iqn": "iqn.2026-07.com.kurrent:worker-02.ubuntu.0d26b6f33a89",
    "master": "_tpl_ubuntu_2204.img"
  }'

# 建空白盘
curl -s -X POST "$BASE_URL/agents/a1b2c3d4e5f6/luns/disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "iqn": "iqn.2026-07.com.kurrent:worker-02.ubuntu.5f1c2a3b4d5e",
    "filename": "worker-02.ubuntu.5f1c2a3b4d5e.img",
    "size": "40G"
  }'
```

#### 成功返回示例

```json
{
  "iqn": "iqn.2026-07.com.kurrent:worker-02.ubuntu.5f1c2a3b4d5e",
  "backing": "/home/iscsi_img/worker-02.ubuntu.5f1c2a3b4d5e.img"
}
```

### POST /agents/{agent_id}/luns/cd

在指定 Agent 上创建 CD（ISO 虚拟光驱）LUN。仅 `role.cd` 为 true 的 Agent 支持。

错误：nvmet 后端不支持 CD（返回 `400 nvmet backend does not support cd`）；未配置 cd 角色时返回 `400 agent ... not configured for cd role`；已停用（排空）时返回 `409 agent ... disabled: not accepting new LUNs`。

#### 请求体字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `iso` | 是 | ISO 文件名（存在于 `DISK_DIR` 下） |
| `iqn` | 否 | target IQN；不传时由 Agent 按派生 IQN base（`nqn_to_iqn(spec.agent.nqnBase)`）:iso文件名 自动生成 |

#### curl

```bash
curl -s -X POST "$BASE_URL/agents/f60123456789/luns/cd" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "iso": "tools.iso"
  }'
```

### DELETE /agents/{agent_id}/luns

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
curl -s -X DELETE "$BASE_URL/agents/a1b2c3d4e5f6/luns?iqn=iqn.2026-07.com.kurrent:worker-02.ubuntu.0d26b6f33a89" \
  -H "Authorization: Bearer $TOKEN"

# 连 backing 文件一起删，target 已不存在也继续
curl -s -X DELETE "$BASE_URL/agents/a1b2c3d4e5f6/luns?iqn=iqn.2026-07.com.kurrent:worker-02.ubuntu.0d26b6f33a89&delete_file=true&ignore_missing=true" \
  -H "Authorization: Bearer $TOKEN"
```

#### 成功返回示例

```json
{
  "deleted": "iqn.2026-07.com.kurrent:worker-02.ubuntu.0d26b6f33a89",
  "delete_file": false
}
```

忽略缺失时返回：

```json
{
  "deleted": "iqn.2026-07.com.kurrent:worker-02.ubuntu.0d26b6f33a89",
  "delete_file": true,
  "ignored_missing": true
}
```

### POST /agents/{agent_id}/luns/scan

触发 Agent 扫描镜像目录，为缺失的 `.img` 文件重建 target（文件即真相）；母盘（`*_tpl_*`）由母盘扫描器单独管理，跳过。

nvmet 的子系统在 configfs 中持久，通常全部跳过；stgt 返回重建结果。`stgt` 会为 `.iso` 建 CD target，`nvmet` / `lio` 跳过 ISO。

#### curl

```bash
curl -s -X POST "$BASE_URL/agents/a1b2c3d4e5f6/luns/scan" \
  -H "Authorization: Bearer $TOKEN"
```

#### 成功返回示例

```json
{
  "created": [
    {
      "iqn": "iqn.2026-07.com.kurrent:worker-02.ubuntu.0d26b6f33a89",
      "cd": false
    }
  ],
  "skipped": []
}
```

---

## GET /masters（母盘清单）

### 说明

聚合列出全部**启用磁盘角色**（`enabled=true` 且 `role.disk=true`）Agent 上的母盘清单。母盘由存储节点 Agent 的后台扫描线程周期扫描（默认每 30 秒），识别 `DISK_DIR` 下文件名含 `_tpl_` 标记（大小写不敏感）的镜像文件（如 `_tpl_ubuntu_2204.img`）。

供 WebUI 母盘克隆下拉列表选盘。**与创建 Worker 的 API 无联动**——纯只读查询，不改任何状态，不写台账。

母盘清单项带 `gpt` 字段（GPT 扇区布局探测：`4k` / `512` / `none`）。nvmet 数据面下 **`512` 布局母盘不可克隆**——file backend 按底层 FS 块（典型 4K）导出逻辑块，512 字节扇区 GPT 被发起端按 4K 解释、EDK2 找不到 ESP（实测引导失败）。Agent 建盘时直接拒绝（见本节），前端据此标注。

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
      "agent": "a1b2c3d4e5f6",
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
