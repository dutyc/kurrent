---
title: "Agents and Storage"
---

# Agents and Storage

Storage nodes register as Agents. The endpoints below cover the Agent ledger, master tags, LUNs/targets and the master image list.

## GET /agents

### Description

Lists Agents configured in `config/agents.yml`. By default, it also probes each Agent’s `/healthz` and `/capabilities` in real time.

`health` has three states: `ok` (healthy); `degraded` (the agent is up but the data plane is not ready — for the nvmet backend this means the host service is unreachable or configfs is not ready; the reason is in `error`); `error` (agent unreachable or capability probing failed). With `live=false` no probing is done and `health` is absent.

Identity layering: `id` is the machine identity and accepts only 12 lowercase hex characters (same form as the disk `os_tag`; allocated by the control plane at bootstrap-token issuance and written by join to `storager/state/agent.id` under the node state root, auto-registered at enroll; manual registration uses `POST /agents`); `remark` is a human label owned by the control-plane ledger, shown on the WebUI card and editable in the edit form or via `PUT /agents/{agent_id}`.

### Query Parameters

| Parameter | Required | Default | Description |
|---|---:|---|---|
| `live` | No | `true` | Whether to probe Agent status and capabilities in real time. |

### curl

Live probe:

```bash
curl -s "$BASE_URL/agents?live=true" \
  -H "Authorization: Bearer $TOKEN"
```

Configuration only, no probing:

```bash
curl -s "$BASE_URL/agents?live=false" \
  -H "Authorization: Bearer $TOKEN"
```

### Example Success Response

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
    "remark": "primary storage node",
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

### Description

Registers a new Agent: writes to `config/agents.yml` and takes effect immediately (the Agent will be included in disk creation / mount scheduling). Duplicate `id` registration returns `409`.

**Recommended flow**: first fill in the API address in the WebUI (or use `POST /agents/probe`, see this section) and probe to automatically obtain role, tags, data-plane address, etc. After confirming, call this endpoint to complete registration. Direct full-parameter submission is also supported.

### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `id` | Yes | Machine identity: 12 lowercase hex characters; put descriptive names in `remark`. |
| `base_url` | Yes | Agent Control Plane API address. Must start with `http://` or `https://`. Trailing `/` is removed automatically. |
| `storager_ip` | No | Data-plane address (business network IP, protocol-neutral). Falls back to the hostname of `base_url` if omitted. |
| `role` | No | Roles: `disk` = can create system disks (storage node), `cd` = can mount ISOs (optical drive node). Default `{disk: false, cd: false}`. |
| `tags` | No | Free-form tag array (e.g., `storage` / `rack-a3`), for **human annotation / display only**. It does not participate in `/boot-vars` separator derivation (the backend type is resolved by `/boot-vars` querying the Agent’s `/capabilities` in real time, see [Boot chain](./boot.md)). |
| `enabled` | No | Whether the Agent is enabled. `false` = **drain**: no new disks accepted (always 409), existing disks keep serving and booted devices are unaffected. Default `true`. |
| `remark` | No | Human label (display-only), empty string by default. Nicknames belong to the control-plane ledger: enroll does not write it, so set it after registration by hand or via `PUT /agents/{agent_id}`. |

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
    "remark": "storage node 02"
  }'
```

### Success Response (201)

```json
{
  "id": "c3d4e5f60123",
  "base_url": "http://host.docker.internal:4840",
  "storager_ip": "192.168.1.6",
  "role": {"disk": true, "cd": false},
  "enabled": true,
  "tags": ["storage"],
  "remark": "storage node 02"
}
```

### Error Responses

| Status Code | Scenario |
|---|---|
| `400` | Invalid `id` format / `base_url` does not start with http(s) |
| `409` | Agent `id` already exists |

---

## POST /agents/probe

### Description

Probes an Agent and auto-derives registration parameters (**read-only preview, no files are written**): calls the Agent’s `/healthz` and `/capabilities` over mTLS (the Control Plane’s client certificate, bound to the internal CA), and derives:

| Parameter | Derivation Rule |
|---|---|
| `role.disk` | Always `true` (Agent is a storage node) |
| `role.cd` | From `capabilities.cd` |
| `tags` | `["storage"]` (human tags only; the backend type is **not written into tags** — it is returned separately as the `backend` field and resolved by `/boot-vars` querying `/capabilities` in real time) |
| `storager_ip` | Falls back to `base_url` hostname if not provided |

### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `base_url` | Yes | Agent Control Plane API address. Must start with `http://` or `https://`. |
| `agent_id` | No | Optional; the probe does not use this field. |

### curl

```bash
curl -s -X POST "$BASE_URL/agents/probe" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"base_url": "http://host.docker.internal:4840"}'
```

### Success Response

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

### Error Responses

| Status Code | Scenario |
|---|---|
| `400` | `base_url` does not start with http(s) |
| `502` | Agent unreachable (`/healthz` failed) or `/capabilities` call failed (e.g., mTLS client certificate not issued by the Control Plane CA) |

---

## PUT /agents/{agent_id}

### Description

Updates an existing Agent: overwrites the corresponding entry in `config/agents.yml` and takes effect immediately (disk creation / mount scheduling uses the new configuration). The `id` cannot be changed (taken from the path parameter).

Use cases: disable/enable a node (drain), data-plane address migration, remark and role adjustments. **For auto-registered nodes (tags containing `auto`), `base_url` is managed by the node declaration** (declaration authority, see [Identity and PKI](./identity.md)): submitting the current value succeeds (the regular shape of a WebUI edit save); submitting a different value returns `409` — after a node move, edit `spec.agent.advertiseUrl` in the node’s `storager/kurrent.yaml` and re-run `kurrent join`; renewal syncs it automatically. Manually registered nodes can still change the address from the control plane.

### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `base_url` | Yes | Agent Control Plane API address. Must start with `http://` or `https://`. Trailing `/` is removed automatically. **Not editable for auto-registered nodes** (managed by the node declaration, see the description). |
| `storager_ip` | No | Data-plane address. Falls back to the hostname of `base_url` if omitted. |
| `role` | No | Roles: `disk` = can create system disks, `cd` = can mount ISOs. Default `{disk: false, cd: false}`. |
| `tags` | No | Free-form tag array (human annotations only). |
| `enabled` | No | Whether the Agent is enabled. `false` = **drain**: no new disks accepted (always 409), existing disks keep serving and booted devices are unaffected. Default `true`. |
| `remark` | No | Human label (display-only). Full-overwrite semantics — an empty string clears it. |

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
    "remark": "standby storage node"
  }'
```

### Success Response (200)

```json
{
  "id": "c3d4e5f60123",
  "base_url": "http://host.docker.internal:4840",
  "storager_ip": "192.168.1.8",
  "role": {"disk": true, "cd": false},
  "enabled": true,
  "tags": ["storage"],
  "remark": "standby storage node"
}
```

### Error Responses

| Status Code | Scenario |
|---|---|
| `400` | `base_url` does not start with http(s) |
| `404` | Agent `id` not found |
| `409` | Changing `base_url` of an auto-registered node (tags contain `auto`; the address is managed by the node declaration — the error message explains how to change it) |

> **Probing during edit**: In edit scenarios, it is recommended to first call `POST /agents/probe` to verify the address is reachable before saving. The probe authenticates with the Control Plane’s mTLS client certificate (identity bound to the component CA) — no token is required. For auto-registered nodes the address is read-only and the probe runs against the current value.

---

## Master Tags (/agents/{agent_id}/masters/{master_name}/tag)

### Overview

Master tags are control-plane annotations (`state/masters.yml`) — `os` / `os_version` are **annotation-only** (for human understanding, no whitelist validation):

- There is no valid OS set: disk creation `os` is a free string, and multiple versions of the same OS are distinguished by the disk-level `os_tag` (a 12-hex random identifier, the data-plane unique key). Master tags only associate a master name with an OS/version.
- **Tagging does not verify that the master exists** (an Agent may be offline; the ledger is authoritative); `/masters` attaches `os` / `os_version` to tagged entries only.
- When creating a disk from a master, the WebUI auto-fills the tag (annotation, editable); clearing a tag only affects future disk creation.

### PUT (register / update)

| Field | Required | Description |
|---|---:|---|
| `os` | Yes | OS annotation (free string, lowercased; for human understanding, not a data-plane identifier) |
| `os_version` | No | Version annotation, defaults to `''` (no version) |

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

#### Success response

```json
{ "agent": "a1b2c3d4e5f6", "name": "_tpl_ubuntu_2204.img", "os": "ubuntu", "os_version": "22.04" }
```

### DELETE (clear)

```bash
curl -s -X DELETE "$BASE_URL/agents/a1b2c3d4e5f6/masters/_tpl_ubuntu_2204.img/tag" \
  -H "Authorization: Bearer $TOKEN"
```

Returns `{ "agent": ..., "name": ..., "removed": true|false }` (`removed=false` = there was no tag to begin with).

### Error Responses

| Status Code | Scenario |
|---:|---|
| `400` | `os` empty / illegal characters; `os_version` contains illegal characters |
| `401` | Missing or invalid Token |
| `404` | Agent not found |

## DELETE /agents/{agent_id}

### Description

Removes the Agent’s registry entry (`config/agents.yml`) and cleans up its master tags (`state/masters.yml` entry under masters). **Control-plane records only — nothing is blocked and no certificates/running LUNs on the node are touched**.

Semantics (a corollary of declaration authority): the deletion is **soft** — the node still runs with its declaration and re-registers automatically at the next renewal (re-joining requires no new bootstrap token); to truly decommission a node, act on the **node side** (stop the containers / remove the declaration). Side effect: after the registry entry is removed, disks still hosted on that node cannot have their boot addresses projected until the node re-registers (the registry is the knowledge); the response reports this via `disks_hosted` / `affected_workers` (informational only, not blocking).

### curl

```bash
curl -s -X DELETE "$BASE_URL/agents/a1b2c3d4e5f6" \
  -H "Authorization: Bearer $TOKEN"
```

### Successful response

```json
{ "deleted": "a1b2c3d4e5f6", "master_tags_removed": false, "disks_hosted": 3, "affected_workers": ["worker-01", "worker-02"] }
```

### Common errors

| HTTP status | Common cause |
|---:|---|
| `401` | Missing or wrong token |
| `404` | Agent does not exist |

## Agent LUN/Target Management (NVMe-oF primary path)

### Description

The Control Plane can directly manage targets/LUNs on any Agent. Requests are forwarded by the Control Plane to the Agent over mTLS (the Control Plane’s component client certificate, bound to the internal CA), so callers only need Control Plane credentials and never need to contact the Agent directly.

The data-plane backend is selected by the node declaration `spec.agent.backend`: `nvmet` (default, primary path) identifies targets by **disk NQN**; `stgt` / `lio` are iSCSI compatibility backends and identify targets by IQN. To serve both, these endpoints keep `iqn` as the request key: on the nvmet backend the Agent converts it back to an NQN (same-suffix prefix transform), and list responses also return `nqn` (nvmet) or `iqn` (stgt/lio).

Differences from Worker lifecycle endpoints (`POST /workers`, `DELETE /workers/{worker_id}`):

- Worker endpoints are **inventory-oriented**: they generate the disk NQN and derived IQN, write `state/workers.yml`, and maintain the dnsmasq derived projection.
- LUN management endpoints are **data-plane direct management**: they do not write any inventory; they directly operate targets on the Agent. Suitable for master image management, manual troubleshooting, temporary ISO mounting, etc.

All endpoints require management-plane authentication (see [Authentication](./auth.md)). If the Agent is not found, `404 agent not found` is returned. If the Agent is unreachable, `503` is returned. Business validation errors from the Agent side (e.g., NQN prefix mismatch, file already exists, target already exists) are passed through with the Agent’s status code and `detail`:

```json
{"agent": "a1b2c3d4e5f6", "error": "iqn base mismatch: ..."}
```

### GET /agents/{agent_id}/luns

Lists all targets/LUNs on the specified Agent. On the nvmet backend the target identifier is the NQN (`nqn`; the `iqn` key is kept for the historical interface contract and equals `nqn`); stgt includes a `tid` field, and LIO is the parsed output of `targetcli`. The Control Plane passes it through unchanged.

#### Path Parameters

| Parameter | Required | Description |
|---|---:|---|
| `agent_id` | Yes | Agent identifier, corresponding to a key in `config/agents.yml`. |

#### curl

```bash
curl -s "$BASE_URL/agents/a1b2c3d4e5f6/luns" \
  -H "Authorization: Bearer $TOKEN"
```

#### Example Success Response (nvmet)

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

Entries from stgt/lio backends have no `nqn`:

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

Creates a disk LUN on the specified Agent. Pass `master` to clone from a master image (prefers btrfs / ZFS(≥2.2) reflink for instant cloning), or pass `size` to create an empty sparse disk. If the Agent is not configured with `role.disk`, it returns `400 agent ... not configured for disk role`; if the Agent is disabled (drain), it returns `409 agent ... disabled: not accepting new LUNs`; on the nvmet data plane, a master in 512-byte-sector GPT layout is rejected with `400` (including 4K rebuild guidance; see the `gpt` field in the `GET /masters` response).

#### Path Parameters

| Parameter | Required | Description |
|---|---:|---|
| `agent_id` | Yes | Agent identifier. |

#### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `iqn` | Yes | iSCSI form of the target identifier (disk IQN, derived from the disk NQN). Must be prefixed with the Agent’s derived IQN base (`nqn_to_iqn(spec.agent.nqnBase)`); the nvmet backend converts it back to an NQN. |
| `filename` | No | Backing filename. If omitted, the Agent auto-generates one from the IQN. |
| `master` | Conditionally required | Master image filename (located in `DISK_DIR`). Mutually exclusive with `size`. |
| `size` | Conditionally required | Empty disk size, e.g., `40G`. Mutually exclusive with `master`. |

#### curl

```bash
# Clone from master
curl -s -X POST "$BASE_URL/agents/a1b2c3d4e5f6/luns/disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "iqn": "iqn.2026-07.com.kurrent:worker-02.ubuntu.0d26b6f33a89",
    "master": "_tpl_ubuntu_2204.img"
  }'

# Create empty disk
curl -s -X POST "$BASE_URL/agents/a1b2c3d4e5f6/luns/disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "iqn": "iqn.2026-07.com.kurrent:worker-02.ubuntu.5f1c2a3b4d5e",
    "filename": "worker-02.ubuntu.5f1c2a3b4d5e.img",
    "size": "40G"
  }'
```

#### Example Success Response

```json
{
  "iqn": "iqn.2026-07.com.kurrent:worker-02.ubuntu.5f1c2a3b4d5e",
  "backing": "/home/iscsi_img/worker-02.ubuntu.5f1c2a3b4d5e.img"
}
```

### POST /agents/{agent_id}/luns/cd

Creates a CD (ISO virtual drive) LUN on the specified Agent. Only Agents with `role.cd` set to true support this; the nvmet backend does not support CD (`400 nvmet backend does not support cd`); an Agent not configured for the CD role returns `400 agent ... not configured for cd role`. A disabled (drained) Agent returns `409 agent ... disabled: not accepting new LUNs`.

#### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `iso` | Yes | ISO filename (present in `DISK_DIR`). |
| `iqn` | No | Target IQN. If omitted, the Agent auto-generates one from the derived IQN base (`nqn_to_iqn(spec.agent.nqnBase)`):`iso_filename`. |

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

Deletes a LUN/target on the specified Agent.

#### Query Parameters

| Parameter | Required | Default | Description |
|---|---:|---|---|
| `iqn` | Yes | None | Target IQN to delete. |
| `delete_file` | No | `false` | Whether to also delete the backing file (`.img`/`.iso`). |
| `ignore_missing` | No | `false` | If `true`, treats a `404 iqn not found` from the Agent as success. |

#### curl

```bash
# Delete only the target, keep the backing file
curl -s -X DELETE "$BASE_URL/agents/a1b2c3d4e5f6/luns?iqn=iqn.2026-07.com.kurrent:worker-02.ubuntu.0d26b6f33a89" \
  -H "Authorization: Bearer $TOKEN"

# Delete target and backing file, continue even if target was already missing
curl -s -X DELETE "$BASE_URL/agents/a1b2c3d4e5f6/luns?iqn=iqn.2026-07.com.kurrent:worker-02.ubuntu.0d26b6f33a89&delete_file=true&ignore_missing=true" \
  -H "Authorization: Bearer $TOKEN"
```

#### Example Success Response

```json
{
  "deleted": "iqn.2026-07.com.kurrent:worker-02.ubuntu.0d26b6f33a89",
  "delete_file": false
}
```

When ignoring a missing target:

```json
{
  "deleted": "iqn.2026-07.com.kurrent:worker-02.ubuntu.0d26b6f33a89",
  "delete_file": true,
  "ignored_missing": true
}
```

### POST /agents/{agent_id}/luns/scan

Triggers the Agent to scan its image directory and rebuild targets for missing `.img` files (file-is-truth); master images (`*_tpl_*`) are managed by the master scanner and skipped. nvmet subsystems persist in configfs and usually skip everything; stgt returns what was recreated. `stgt` also creates CD targets for `.iso` files, while `nvmet` / `lio` skip ISOs.

#### curl

```bash
curl -s -X POST "$BASE_URL/agents/a1b2c3d4e5f6/luns/scan" \
  -H "Authorization: Bearer $TOKEN"
```

#### Example Success Response

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

## GET /masters (Master Image List)

### Description

Aggregates and lists the master images from all **enabled disk-role** Agents (`enabled=true` and `role.disk=true`). Master images are identified by the storage Agent’s background scanning thread (every 30 seconds by default), which looks for image files in `DISK_DIR` whose names contain the `_tpl_` marker (case-insensitive, e.g., `_tpl_ubuntu_2204.img`).

Used by the WebUI for the master clone drop-down list. **It has no linkage to Worker creation APIs** — purely a read-only query, does not change any state or write inventory.

Each master entry carries a `gpt` field (GPT sector layout probe: `4k` / `512` / `none`). On the nvmet data plane, **a `512` master cannot be cloned** — the file backend exports logical blocks in the backing FS block size (typically 4K), a 512-byte-sector GPT is then interpreted as 4K by the initiator, and EDK2 cannot find the ESP (observed as a boot failure). The Agent rejects such clones at creation time (see this section); the frontend labels them accordingly.

### Failure Tolerance

- A single Agent unreachable or authentication failure: that node returns an `error` field and logs an audit `master.list` (failed), **but does not block the overall result**.
- All nodes fail: the overall response is `502`.
- Partial success / no available nodes: returns `200` (with an empty `agents` array if no nodes).

### curl

```bash
curl -s "$BASE_URL/masters" \
  -H "Authorization: Bearer $TOKEN"
```

### Example Success Response

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

| Field | Description |
|---|---|
| `agents` | Array, one entry per Agent with the disk role enabled. |
| `agents[].agent` | Agent identifier (key in `config/agents.yml`). |
| `agents[].storager_ip` | Data-plane address (same fallback rule as `/boot-vars`). |
| `agents[].masters` | Array of masters, each `{name, size, mtime, gpt}`: filename / size in bytes / modification timestamp / GPT sector layout (`4k` / `512` / `none`). |
| `agents[].error` | Error details if querying this node failed (absent for successful nodes). |

---
