---
title: "Workers"
---

# Workers

A Worker is a compute identity. The endpoints below create, query, check the status of, delete, rebind and batch-manage Workers.

## POST /workers

### Description

Registers a Worker’s **identity**: hostname binding. **Storage and identity are separated** — this endpoint does not create any system disks. System disks must be created separately via `POST /workers/{worker_id}/luns/disk` (see [System disks](./disks.md)). `mac` is now **optional**:

- Without `mac` → a pure idle Worker (hostname binding only; no device authorized). Bind a device later via `POST /devices/{mac}/bind`.
- With `mac` → the device must exist in the device pool (`state=pooled`); it is validated and bound directly (one-to-one authorization). If the device is out-of-pool or already bound, a `409` is returned — **register first, then bind**.

The Control Plane will:

1. Validate `worker_id`, `hostname`; validate `mac` if provided
2. Write to `state/workers.yml` (`disks` as empty array, `state=registered`)
3. If `mac` is provided: bind the device (write `state/devices.yml` `bound_worker_id` + write to `dnsmasq/dhcp-hosts.conf`; dnsmasq watches that file, no HUP needed)

### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `worker_id` | Yes | Worker identifier. Automatically lowercased. Allowed characters: letters, digits, dots, underscores, hyphens. |
| `mac` | No | Worker NIC MAC address, e.g., `00:0c:29:b9:8b:2d`. If provided, the device must already be in the pool (see [Devices](./devices.md)); the device is bound to this Worker in the same call. |
| `hostname` | No | Hostname. Defaults to `worker_id` if not provided. |
| `arch` | No | Architecture. Defaults to `x86_64`. |
| `boot` | No | iPXE menu default item and timeout configuration. If omitted, `/boot-vars` derives them from the default boot disk and global defaults. Writes to the same ledger fields as `PUT /workers/{worker_id}/default-disk`; later calls override earlier ones. |

### `boot` Fields

| Field | Required | Description |
|---|---:|---|
| `menu_default` | No | Default item on the iPXE main menu (auto-selected after the menu times out); valid values in the default-disk table; case-insensitive, e.g., `config`, `reboot`, `exit`. |
| `menu_timeout` | No | iPXE menu timeout in milliseconds, e.g., `5000`; `0` means the menu waits indefinitely and never auto-selects. |

When `boot` is omitted:

- `menu_default` defaults to `default_disk` (the first disk is set as default automatically; use [setting the default disk](./disks.md) for multi-disk); if not set, defaults to `reboot` (loop reboot waiting for configuration, see [Boot and Device Admission](./boot.md)).
- `menu_timeout` defaults to `spec.boot.menuTimeoutMs` (currently `5000`) when a default boot is configured; in the `reboot` loop it is fixed to `spec.boot.autoBootTimeoutMs` (currently `1`, see [Boot and Device Admission](./boot.md)).

Therefore, most Workers do not need to pass `boot`. For example:

```json
{
  "worker_id": "worker-01",
  "mac": "00:0c:29:b9:8b:2d"
}
```

After registration, the Worker has no system disk, so `/boot-vars` returns `menu-default reboot` with a 1 ms timeout — the Worker enters a fast reboot loop waiting for the admin to create a disk / set a default boot OS:

```ipxe
set menu-default reboot
set menu-timeout 1
```

After creating a system disk, call `PUT /workers/{worker_id}/default-disk` (see [System disks](./disks.md)) to set the default boot disk (os_tag references a specific disk), and `menu-default` will switch to the generic OS item `boot-os`.

Only pass `boot` when you want to override the menu behavior:

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

To default to the iPXE configuration screen:

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

## PUT /workers/{worker_id}/mac (Rebind Mapping)

### Description

Changes the Worker’s MAC binding (hostname unchanged), mapped internally to a **device rebind**. Bindings are authoritative in the device inventory (`state/devices.yml`); `dnsmasq/dhcp-hosts.conf` is only a derived projection. The Control Plane:

1. Verifies the Worker exists (`404`) and the new MAC format (`400`).
2. Requires the new MAC to be registered and either in the pool (`pooled`) or already bound to this Worker (idempotent, `changed=false`); unregistered, revoked or bound to another Worker yields `409`.
3. Looks up the Worker’s current device in the inventory (**not** in dnsmasq): the old device returns to the pool, the new one is bound to the Worker.
4. Writes `dnsmasq/dhcp-hosts.conf`: replaces the hostname binding when present, appends it when missing (dnsmasq watches the file; no HUP needed); a projection write failure rolls the inventory back.
5. Records `worker.mac.update` (`old_mac` / `new_mac` / `changed` / `client`); when the inventory had no binding change (e.g. the same MAC again) `changed=false` and only the projection is repaired.

> **Note**: `dnsmasq/dhcp-hosts.conf` is a derived view of the device inventory (`MAC -> hostname`), not the MAC authority; the endpoint works even when it is missing or inconsistent. The Worker must renew its DHCP lease (restart NIC / PXE boot again) to use the new MAC identity.

### Body Parameters

| Parameter | Required | Description |
|---|---:|---|
| `mac` | Yes | New MAC address in `XX:XX:XX:XX:XX:XX` form (case-insensitive, normalized) |

### Example

```bash
curl -s -X PUT "$BASE_URL/workers/worker-01/mac" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mac": "00:0c:29:b9:8b:01"}'
```

### Success Response

Returns the full Worker record (`mac` comes from a reverse lookup in the device inventory).

### Common Errors

| HTTP Status | Common Causes |
|---:|---|
| `400` | Invalid `mac` format |
| `401` | Missing or incorrect Token |
| `404` | Worker not found |
| `409` | New device unregistered / revoked / bound to another Worker / dnsmasq projection conflict (MAC bound to another hostname) |
| `500` | Device inventory or dnsmasq binding write failed (inventory rolled back) |

---

## POST /workers/batch

### Description

Bulk-creates Workers (**per-item independent** — a failure of one item does not affect the others; **idempotent** — re-running does not duplicate Workers). `worker_id`s are generated as `name_prefix` + an index (`worker-01`, `worker-02`, …; the index starts at `01` and its width adapts to `count` — `count=100` yields `worker-001` … `worker-100`).

- Without `macs` → all Workers are **idle** (hostname binding only, no device authorized); bind them later via `POST /devices/{mac}/bind`
- With `macs` (must be the same length as `count`) → each MAC is validated against the device pool and bound directly (same semantics as passing `mac` to `POST /workers`: the device must be `state=pooled`; pool-miss / already bound → that item is `failed` and **not created**, retry after fixing)

### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `count` | Yes | Number of Workers to create, 1–100 |
| `name_prefix` | No | Worker ID prefix, default `worker-`. The generated `worker_id`s must be valid (letters, digits, dot, underscore, hyphen allowed); an invalid prefix rejects the whole batch with `400` |
| `macs` | No | Array of MAC addresses (format `00:0c:29:b9:8b:2d`); when provided, its length must equal `count`, and each MAC is validated and bound directly |
| `arch` | No | Architecture. Defaults to `x86_64` |
| `boot` | No | iPXE menu default and timeout config; fields as in `POST /workers` |

### Idempotency and Failure Classification

- `succeeded`: created by this call (with `macs`: includes the bind — an item is only created after its bind succeeds)
- `skipped`: `worker_id` already exists (result of re-running the same request)
- `failed`: device not in pool / revoked / already bound / invalid MAC / hostname conflict etc. — the item is not created, the others are unaffected; retry after fixing

### Example

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

### Success Response (200)

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

### Common Errors

| HTTP Status | Common Causes |
|---:|---|
| `400` | `name_prefix` empty / generated `worker_id` invalid / `macs` length not equal to `count` |
| `401` | Missing or incorrect Token |
| `422` | `count` missing or out of 1–100 range |

---

## GET /workers

### Description

Lists the inventory of all current Workers. The `mac` field in the response comes from the device inventory (`state/devices.yml`, the device bound to that Worker); `dnsmasq/dhcp-hosts.conf` is a derived projection.

### curl

```bash
curl -s "$BASE_URL/workers" \
  -H "Authorization: Bearer $TOKEN"
```

### Example Success Response

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

### Description

Queries the inventory record of a single Worker.

### Path Parameters

| Parameter | Required | Description |
|---|---:|---|
| `worker_id` | Yes | Worker identifier. |

### curl

```bash
curl -s "$BASE_URL/workers/worker-01" \
  -H "Authorization: Bearer $TOKEN"
```

### Success Response

The response structure is identical to the successful `POST /workers` result.

---

## GET /workers/{worker_id}/status

### Description

Queries the Worker’s inventory information and performs real-time checks:

- Whether the Worker’s `MAC,hostname` projection exists in `dnsmasq/dhcp-hosts.conf` (a missing projection only means the derived artifact is out of sync; the device ledger remains the binding authority).
- Whether the corresponding disk target(s) exist on the Agent(s) (matched by disk NQN / derived IQN).
- Whether the corresponding CD target exists on the Agent.

### Path Parameters

| Parameter | Required | Description |
|---|---:|---|
| `worker_id` | Yes | Worker identifier. |

### curl

```bash
curl -s "$BASE_URL/workers/worker-01/status" \
  -H "Authorization: Bearer $TOKEN"
```

### Example Success Response

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

### Description

Per-disk runtime status: export online, credential in effect, active sessions, and usage (logical size + physical allocation). Disks may be spread across different Agents; status is fetched per owning Agent and the Worker-level credential is shown alongside. Called when the disk tab is opened in the Web UI.

### Path Parameters

| Parameter | Required | Description |
|---|---:|---|
| `worker_id` | Yes | Worker ID |

### curl

```bash
curl -s "$BASE_URL/workers/worker-01/disks/status" \
  -H "Authorization: Bearer $TOKEN"
```

### Example Success Response

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

### Field Notes

- `exported`: the subsystem is attached to the data-plane port (export active).
- `hosts`: Host NQNs registered with DHHC-1 authentication (admission allow-list).
- `controllers`: currently active NVMe/TCP sessions (devices connected now).
- `size` / `physical_bytes`: logical size and physical allocation of the disk file (`st_blocks×512`; reflink sharing is counted repeatedly and does not reflect exclusive space).
- `error`: error when the owning Agent is unreachable; usage fields are `null` when the disk file is missing.

## DELETE /workers/{worker_id}

### Description

Deletes a Worker. The Control Plane will:

1. Look up the Worker’s disk and CD inventory from `workers.yml`.
2. **Push credential revocation**: revoke to the registered Agents holding its disks (the Agent drops the hosts entry and clears its cache) — the only credential push during deletion.
3. **Cascade-unbind devices**: every device whose `bound_worker_id` is this Worker is released back to the pool (`state=pooled`, `bound_worker_id=null`; the devices are **not** revoked) — the unbind is persisted first, and if it fails the deletion is aborted.
4. If a CD target exists, delete it first.
5. Then delete the disk target(s).
6. Remove the Worker from `workers.yml`.
7. **Forget the Worker’s credential traces**: drop its key entry from `state/credentials.yml` and its push-state records (audit `credential.forget`) — the Worker no longer exists, and an orphaned entry is neither visible (`GET /credential` returns 404 for a nonexistent Worker) nor removable, while a later Worker re-created with the same id would **silently inherit the old key** and have it pushed to the new machine’s disks.
8. Remove the `mac,hostname` line from `dnsmasq/dhcp-hosts.conf` (dnsmasq watches the file, no HUP needed).

The same cascade-unbind and credential cleanup apply to `POST /workers/delete/batch`.

### Path Parameters

| Parameter | Required | Description |
|---|---:|---|
| `worker_id` | Yes | Worker identifier to delete. |

### Query Parameters

| Parameter | Required | Default | Description |
|---|---:|---|---|
| `delete_disk` | No | `false` | Whether to also delete backing `.img` files. |
| `ignore_missing_target` | No | `false` | If `true`, ignores `404 iqn not found` from the Agent and continues. Same applies when the registry entry of the disk’s Agent has been deleted (ignore=false → 404 `agent not registered`; ignore=true → treated as missing, credential push skipped with an audit entry). |

### curl

Delete only targets, keep `.img` files:

```bash
curl -s -X DELETE "$BASE_URL/workers/worker-01?delete_disk=false" \
  -H "Authorization: Bearer $TOKEN"
```

Delete targets and `.img` files:

```bash
curl -s -X DELETE "$BASE_URL/workers/worker-01?delete_disk=true" \
  -H "Authorization: Bearer $TOKEN"
```

Ignore missing targets on the Agent:

```bash
curl -s -X DELETE "$BASE_URL/workers/worker-01?delete_disk=true&ignore_missing_target=true" \
  -H "Authorization: Bearer $TOKEN"
```

### Example Success Response

```json
{
  "deleted": "worker-01",
  "delete_disk": false,
  "dnsmasq_removed": true
}
```

---

## POST /workers/delete/batch

### Description

Batch delete Workers. Each item is processed independently; **a failure of one does not affect the others**. Returns a summary of `succeeded` / `failed`. The processing per Worker is identical to `DELETE /workers/{worker_id}` (delete CD/system disk targets → remove from inventory → remove dnsmasq binding). After all successful items are processed, the inventory is saved and dnsmasq is reloaded **only once**. Non-existent Workers are counted as `failed` (`worker not found`).

### Request Body Fields

| Field | Required | Default | Description |
|---|---:|---|---|
| `worker_ids` | Yes | — | Array of Worker identifiers to delete. |
| `delete_disk` | No | `false` | Whether to also delete backing `.img` files. |
| `ignore_missing_target` | No | `false` | If `true`, ignores `404 iqn not found` from the Agent and continues. Same applies when the registry entry of the disk’s Agent has been deleted (ignore=false → 404 `agent not registered`; ignore=true → treated as missing, credential push skipped with an audit entry). |

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

### Example Success Response

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
