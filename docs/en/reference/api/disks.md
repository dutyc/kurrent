---
title: "System Disks and Credentials"
---

# System Disks and Credentials

System disks belong to Workers. The endpoints below create disks, create disks in batches, set the default boot disk, delete disks and manage NVMe-oF credentials.

## POST /workers/{worker_id}/luns/disk

### Description

Creates a system disk LUN for a given Worker. System disks are categorized by (os, os_version); a Worker can have multiple system disks (at most one per OS+version pair; different versions of the same OS can coexist — distinguished by the disk-level `os_tag` random identifier). The Control Plane will:

1. Verify the Worker exists and does not already have a disk for that (os, os_version) (returns `409` if it does).
2. Generate the disk-level random identifier `os_tag` (12 hex chars, the data-plane unique key) — it determines the disk NQN suffix and filename.
3. Select a storage Agent (`disk_agent` if given; otherwise the placement policy decides: candidates must be enabled with a ready data plane, and for `type=master` must hold that master locally — no usable candidate returns `503` with `detail.excluded` explaining each node).
4. Assemble the disk NQN (`base-nqn:worker-id.os.<os_tag>`) and backing filename (`worker-id.os.<os_tag>.img`).
5. Call the Agent to create the disk target (master clone or empty disk).
6. Update the Worker’s `disks` inventory in `state/workers.yml` (append to array). On the first disk creation, `state` transitions from `registered` to `ready`; **that disk is also written to `default_disk`** (it is the Worker’s only disk, so no extra default-disk call is needed).
7. **Issue on disk creation**: when the disk lands on an nvmet-backed node and the Worker has no key yet, the Control Plane generates a DHHC-1 key locally and pushes it (audited as `credential.issue`, see this section) — an existing key is not rotated (key follows the Worker; multiple disks share one key). Non-nvmet backends get no key (DHHC-1 is NVMe-oF-specific; iSCSI uses CHAP).

This endpoint resides under the `/luns/` namespace to reserve space for future data disks (`/luns/data`). In a multi-disk scenario, the default boot disk is determined by the `disk` (os_tag) parameter of `PUT /workers/{worker_id}/default-disk` (the second and later disks never rewrite the default automatically).

### Path Parameters

| Parameter | Required | Description |
|---|---:|---|
| `worker_id` | Yes | Worker identifier. |

### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `type` | Yes | `master` or `empty`. |
| `name` | Conditionally required | Required when `type=master`. The master image filename. |
| `size` | Conditionally required | Required when `type=empty`. The empty disk size, e.g., `40G`. |
| `os` | Yes | OS annotation (for human understanding; determines the os segment in the disk NQN/filename). Lowercased, then validated: the first character must be a letter or digit; the rest may only be lowercase letters, digits, dots, underscores and hyphens, up to 32 characters; there is no distro enumeration. |
| `os_version` | No | Version annotation (`''` = no version, the default). At most one disk per (os, os_version); different versions can coexist. |
| `disk_agent` | No | Specify a storage Agent. If omitted, the Control Plane places it (candidates are checked for a ready data plane and local master availability). |

### Clone from Master

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

### Create Empty Disk

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

### Example Success Response (master clone)

```json
{
  "hostname": "worker-01",
  "arch": "x86_64",
  "state": "ready",
  "disks": [
    {
      "agent": "a1b2c3d4e5f6",
      "nqn": "nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",  # NVMe-oF data-plane identifier (authoritative; NQN is not defined via IQN; suffix carries os_tag)
      "iqn": "iqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",  # iSCSI data-plane identifier (derived from the NQN)
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

### Example Success Response (empty disk)

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

### Batch Create System Disks (POST /workers/luns/disk/batch)

Batch deployment scenario: apply the same disk parameters to multiple Workers. **Two mutually exclusive ways to place them** (supplying both returns `400`):

- `targets`: name the storage node **per item** (explicit override, the original form);
- `workers` + `placement`: declare only *which* Workers get a disk and let the **Control Plane place them**.

Placement semantics for the `workers` form — placement is a system decision; the caller supplies intent and constraints:

- **Candidate nodes** = disk role ∧ enabled (not draining) ∧ has `base_url` ∧ **data plane ready** (`healthz.backend_ready` is not `false`) ∧ listed in `placement.candidates` (empty = unrestricted) ∧ (for `type=master`) **the master image exists locally** — cloning happens locally on the node, so a node without that master cannot possibly serve the request;
- **Assignment** = fewest existing disks first (spread), ties broken by registration order;
- `placement.pins` pins individual Workers (explicit override): a pinned node does **not** have to be inside `candidates`, but it must itself be usable, otherwise `503`;
- No usable candidate → `503`, with `detail.excluded` giving the reason per node (wrong role / draining / unreachable / data plane not ready / missing master).

`dry_run=true` returns the plan (`plan` / `warnings`) without creating anything. **Preview and execution share the same placement implementation**, so the node in `plan` is exactly what execution will do (each row's `action` is `create` / `skip` / `error`). Note that a plan for the `targets` form does **not** probe the nodes (it judges create/skip/error from inventory alone), whereas every candidate of the `workers` form was probed during placement.

Same as single-disk creation: `master` clones from a master image, `empty` creates a blank disk. At most one disk per (os, os_version) is allowed; if a Worker already has one it is **automatically skipped** (not considered a failure). **Successfully created Workers automatically set `default_disk` to the batch disk’s `os_tag`** — batch deployment goes directly to the default boot without needing an extra `PUT /workers/{worker_id}/default-disk` call (single-disk creation sets it automatically too when it is the first disk). Workers placed on nvmet-backed nodes also get their authentication key **issued on disk creation** (each Worker gets its own distinct key, in one pass — see step 7 of the disk-creation flow and the NVMe-oF credential section). Each item is processed independently; a failure of one does not affect the others. Returns a summary of `succeeded` / `skipped` / `failed`. Items whose target Agent does not exist or is disabled (drain) land in `failed` (`agent not found` / `agent ... disabled`); the remaining items proceed as usual.

#### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `type` | Yes | `master` or `empty`. |
| `os` | Yes | OS annotation (same for all Workers in the batch; determines the os segment in the disk NQN/filename; lowercased, then validated: the first character must be a letter or digit; the rest may only be lowercase letters, digits, dots, underscores and hyphens, up to 32 characters). |
| `os_version` | No | Version annotation (`''` = no version, the default). |
| `name` | Conditionally required | Required when `type=master`. The master image filename. |
| `size` | Conditionally required | Required when `type=empty`. The empty disk size, e.g., `40G`. |
| `targets` | Either/or | Array of `{worker_id, agent}`: storage node named per item. |
| `workers` | Either/or | Array of Worker identifiers: storage node resolved by the placement policy. |
| `placement` | No | Only applies to `workers`. `candidates` (array restricting the auto-placement pool; empty = all), `pins` (object `{worker_id: agent}`, per-Worker override). |
| `dry_run` | No | `true` returns the plan only, creating nothing (default `false`). |

#### curl (Control-Plane placement)

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

#### Plan Response (dry_run)

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

To execute, resend the identical request with `dry_run` removed — placement matches the plan.

#### curl (per-item naming, the original form)

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

#### Example Response

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

## Common Errors for Registration and Disk Creation

This section lists common errors from Worker registration (`POST /workers`) and system disk creation (including the batch endpoint).

| HTTP Status | Common Causes |
|---:|---|
| `400` | Parameter format error; `os` empty or illegal characters; `type=master` but `name` missing; `type=empty` but `size` missing |
| `401` | Missing or incorrect Token |
| `404` | Worker not found when creating a system disk; the `disk_agent` Agent does not exist |
| `409` | `worker_id` already exists; `hostname` already exists; MAC already bound; Worker already has a disk for that (os, os_version) (duplicate version); IQN already exists on Agent; backing file already exists; target Agent is disabled (drain: `agent ... disabled: not accepting new disks`) |
| `500` | dnsmasq binding write failed; file write failure; other unexpected errors |
| `503` | Agent unreachable |

---

## PUT /workers/{worker_id}/default-disk

### Description

**What the “default boot disk” is for**: a Worker can have multiple system disks (at most one per OS+version pair, e.g., `ubuntu 22.04` + `windows`; different versions of the same OS can coexist). On every boot, the iPXE menu auto-selects one item after its timeout — by default it selects the default boot disk configured here (`boot-os`), which also decides which disk’s connection info `/boot-vars` projects (`disk_nqn` / `storager_ip` come from the default boot disk, see [Boot and Device Admission](./boot.md)). When `menu_default` is set, the menu auto-selects that item instead, while the projected connection info still comes from the default disk. Without either, the menu auto-selects `reboot` with a 1 ms timeout and loops rebooting until the admin finishes configuration — never silently booting into the wrong OS.

**Note**: `disk` is the disk-level random identifier `os_tag` (generated at disk creation, 12 hex chars, the data-plane unique key), not an OS name; when multiple versions of the same OS coexist, only the os_tag can precisely locate the **specific disk** to boot by default.

The `/boot-vars` `menu_default` derivation chain:

```text
boot.menu_default (the `menu_default` field of this endpoint, explicit override, highest priority) -> default_disk (the `disk` field of this endpoint) -> reboot (not configured, loop reboot waiting)
```

The three request body fields can be sent individually or in combination; at least one must be provided. Sending `null` (or an empty string) clears the corresponding item. This endpoint can be called repeatedly; later calls override earlier ones — they write the same ledger fields as the `boot` passed at registration (`POST /workers`).

Requirements:

- When setting `disk`: The Worker must already have a system disk with that `os_tag` (created by `POST /workers/{worker_id}/luns/disk`), otherwise a `400` is returned listing the current system disks. In the multi-disk model, use `os_tag` to precisely match the disk to boot by default.
- When setting `menu_default`: Only item IDs that can be selected manually in the current main menu are accepted: `config` / `shell` / `reboot` / `exit` (strict validation to prevent an empty `choose --default` in iPXE); `boot-os` is derived from the default disk and cannot be set manually.
- When setting `menu_timeout`: Non-negative integer; `0` means the menu waits indefinitely (never auto-selects, waits for a human keypress); clearing restores the default `spec.boot.menuTimeoutMs` (currently `5000`).

### Path Parameters

| Parameter | Required | Description |
|---|---:|---|
| `worker_id` | Yes | Worker identifier. |

### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `disk` | No | The `os_tag` (12 hex chars) of the default boot disk — precisely references a specific disk, and must match a disk already attached to this Worker. Pass `null` to clear. |
| `menu_default` | No | Default item on the iPXE main menu (auto-selected after the menu times out). See the valid values table below; case-insensitive (auto-lowercased). Pass `null` to clear. |
| `menu_timeout` | No | Menu timeout in milliseconds, non-negative integer; `0` means the menu waits indefinitely (never auto-selects, waits for a human keypress). Pass `null` to clear, restoring `spec.boot.menuTimeoutMs` (currently `5000`). |

### Valid `menu_default` Values (menu.ipxe Main Menu Item IDs)

| Category | Valid Values |
|---|---|
| Generic OS item | `boot-os` is derived from the default disk configuration and **cannot be set manually** |
| Advanced | `config` `shell` `reboot` `exit` |

### Example: Set Default Boot Disk

```bash
curl -s -X PUT "$BASE_URL/workers/worker-01/default-disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "disk": "0d26b6f33a89"
  }'
```

### Example: Set Menu Default Item and Timeout

```bash
curl -s -X PUT "$BASE_URL/workers/worker-01/default-disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "menu_default": "config",
    "menu_timeout": 3000
  }'
```

### Example: Clear Default Boot Disk

```bash
curl -s -X PUT "$BASE_URL/workers/worker-01/default-disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "disk": null
  }'
```

### Success Response

Returns the full Worker inventory, including the set fields like `default_disk`, `boot.menu_default`, `boot.menu_timeout`.

### Common Errors

| HTTP Status | Common Causes |
|---:|---|
| `400` | None of the three fields provided; `disk` does not match any attached system disk; `menu_default` not in the valid values table; `menu_timeout` negative |
| `401` | Missing or incorrect Token |
| `404` | Worker not found |

---

## DELETE /workers/{worker_id}/luns/disk/{os_tag}

### Description

Deletes a single system disk of a given Worker (by the disk-level random identifier `os_tag`, 12 hex chars). The Control Plane will:

1. Verify the Worker exists and has a disk with that `os_tag` (returns `404` if not).
2. Call the hosting Agent to delete the target (nvmet subsystem / iSCSI target).
3. Remove the disk entry from the Worker’s `disks` array in `state/workers.yml`.
4. Cleanup linkage: if the deleted disk was the default boot disk (`default_disk` equals its `os_tag`), both `default_disk` and any matching (same-OS) `boot.menu_default` are cleared (to prevent iPXE from booting to a deleted disk); **if exactly one disk remains, it becomes the default boot disk**.
5. When the last disk is deleted, `state` reverts from `ready` to `registered` (waiting for a new disk to be created).

### Query Parameters

| Parameter | Default | Description |
|---|---:|---|
| `delete_file` | `false` | Whether to also delete the backing `.img` file. `false` only deletes the target (the .img is retained and can be re-attached). |
| `ignore_missing_target` | `false` | If `true`, ignores a `404 iqn not found` from the Agent and continues with the inventory cleanup. Same applies when the registry entry of the disk’s Agent has been deleted (ignore=false → 404 `agent not registered`; ignore=true → treated as missing). |

### Example: Delete System Disk, Keep .img

```bash
curl -s -X DELETE "$BASE_URL/workers/worker-01/luns/disk/0d26b6f33a89" \
  -H "Authorization: Bearer $TOKEN"
```

### Example: Delete System Disk and .img File

```bash
curl -s -X DELETE "$BASE_URL/workers/worker-01/luns/disk/0d26b6f33a89?delete_file=true" \
  -H "Authorization: Bearer $TOKEN"
```

### Success Response

Returns the full Worker inventory (with `disks` no longer containing the deleted system disk; if it was the default, `default_disk`/`boot.menu_default` are cleared, and when exactly one disk remains it becomes the default boot disk; if no disks remain, `state=registered`).

### Common Errors

| HTTP Status | Common Causes |
|---:|---|
| `400` | Invalid `os_tag` (not 12 hex chars) |
| `401` | Missing or incorrect Token |
| `404` | Worker not found, or the Worker does not have a disk with that `os_tag` |

---

## POST /workers/luns/disk/batch-delete

### Description

Batch-delete system disks: either `targets` naming each `{worker_id, os_tag}`, or `workers` to delete all disks of those workers — pick one. With `dry_run=true` only a plan is returned and nothing is deleted; `delete_file` / `ignore_missing_target` behave as in the single delete. Items run independently and return `succeeded` / `skipped` / `failed` summaries (missing disk or worker is reported as skipped).

### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `targets` | One of two | Per-item `{worker_id, os_tag}` |
| `workers` | One of two | Worker list; deletes all their system disks |
| `delete_file` | No | Also delete the disk `.img` file, default false |
| `ignore_missing_target` | No | Tolerate missing targets on the Agent, default false |
| `dry_run` | No | Return only the plan, default false |

### curl

```bash
curl -s -X POST "$BASE_URL/workers/luns/disk/batch-delete" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"workers": ["worker-01", "worker-02"], "dry_run": true}'
```

### Example Success Response

```json
{
  "succeeded": [{ "worker_id": "worker-01", "os_tag": "0d26b6f33a89", "os": "ubuntu" }],
  "skipped": [{ "worker_id": "worker-02", "os_tag": null, "reason": "worker not found" }],
  "failed": []
}
```

With `dry_run=true` it returns `{"dry_run": true, "plan": [{worker_id, os_tag, action, reason}]}`, where `action` is `delete` / `skip` / `error`.

## PUT /workers/default-disk/batch

### Description

Batch-set the default boot disk: either `targets` naming each `{worker_id, os_tag}`, or `workers` plus `os`[/`os_version`] matching one disk per worker — pick one. Only `default_disk` is changed; menu overrides (`menu_default` / `menu_timeout`) are out of scope for batch — a Worker with a `menu_default` override still boots that item, so clear the override first (`PUT /workers/{worker_id}/default-disk` with `{"menu_default": null}`). `dry_run=true` returns only the plan. Already-default is skipped; no match or missing disk is failed.

### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `targets` | One of two | Per-item `{worker_id, os_tag}` |
| `workers` | One of two | Worker list; `os` is required for matching |
| `os` | Required with workers | OS label (ledger remark) to match a worker’s disk |
| `os_version` | No | Version; empty means any version (must be unique under that OS) |
| `dry_run` | No | Return only the plan, default false |

### curl

```bash
curl -s -X PUT "$BASE_URL/workers/default-disk/batch" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"workers": ["worker-01", "worker-02"], "os": "ubuntu", "os_version": "24.04"}'
```

### Example Success Response

```json
{
  "succeeded": [{ "worker_id": "worker-01", "os_tag": "0d26b6f33a89" }],
  "skipped": [{ "worker_id": "worker-02", "os_tag": "1a2b3c4d5e6f", "reason": "already default" }],
  "failed": [{ "worker_id": "worker-03", "os_tag": null, "error": "no disk for ubuntu 26.04" }]
}
```

## NVMe-oF Authentication Credentials (/workers/{worker_id}/credential)

### Description

The NVMe-oF connection authentication key store (the key follows the Worker):
keys are indexed by `worker_id`, and all devices bound to that Worker share the same key
(a rebind rotates the key). Keys use the **DHHC-1** format:
`DHHC-1:<two-digit type>:<base64(key + CRC32 little-endian 4 bytes)>`, with a 32-byte
(SHA-256) or 64-byte (SHA-512) key body.

**Issuance timing: issued on disk creation** — when a system disk is created on an
nvmet-backed node (step 7 of disk creation, single and batch alike), the Control Plane generates a DHHC-1 key
for any Worker that has none yet and pushes it (audited as `credential.issue`): the subsystem and
its matching authentication key land together, so a batch of N disks yields N keys in one pass with
no per-worker manual step. Creating a disk for a Worker that already has a key does **not** rotate
it (key follows the Worker; multiple disks share one key). Non-nvmet backends (stgt / lio) get no
key — DHHC-1 is NVMe-oF-specific. The manual endpoints in this section are therefore narrowed to
**explicit rotation / revocation** (revocation deletes the entry, so a later disk creation won't
resurrect it).

After a credential write, the Control Plane **pushes the desired state to the Agent(s) hosting
the Worker’s system disks** (audited as `credential.push`, never logging the key itself). The push
body carries the plaintext key plus the **`tls_psk` derived from it** (besides registering hosts,
the Agent relays `PUT /subsystems/{nqn}/tls` to the host service to inject the PSK into the kernel's
global `.nvme` keyring; revocation removes it). The
Agent relays to the storage node’s nvmet host service to register hosts (the subsystem NQN is
the disk NQN — the NQN is the authoritative disk identifier and the IQN is derived from it
(`iqn.` + nqn[4:]); IQNs are never used as subsystem NQNs (NVMe Base Spec §7.9 requires the
`nqn.` prefix). The **Host NQN is worker-derived**: `nqn.<…>:host.<worker_id>` (base = the disk
NQN prefix, i.e. the node's `spec.agent.nqnBase`) and is injected via `/boot-vars` to override
the firmware's built-in default (the firmware derives it from the SMBIOS UUID by default; without
the override authentication always fails).

**Push reconciliation**: the per-Agent outcome of every push is
recorded in `state/credential-push.yml` (key = `worker_id/agent_id`, storing the audit hash of the
pushed key and a monotonic `generation`, never the plaintext). A control-plane reconciliation
thread (default interval 60s, `KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL` seconds, 0 disables)
compares desired state against the records: pairs whose key hash matches and whose **per-pair
application is confirmed** are skipped, everything else is replayed idempotently — closing three
silent gaps: failed pushes without retry, crash windows (ledger changed, nothing pushed) and
undelivered revocations. `ok` does not mean HTTP 200: the Agent must report `tls_expected`
(number of subsystems × hosts) and `tls_applied`, and they must match; an old Agent that does not
report application results, a partial application, or a target-count mismatch is recorded as
`failed` (fail-closed, never mistaking "sent" for "applied"). When the Agent reports
`tls_unavailable` (host TLS port not ready, 409) for every target the outcome is `degraded` —
the authentication key is written but the transport-encryption PSK is not, so boot still fails;
reconciliation keeps retrying and flips to `ok` once the port is ready.

Even when the ledger says `ok` and the hash matches, reconciliation re-checks the host's kernel
`.nvme` keyring through the Agent's credential-status query: for a set desired state every
(subsystem, host) PSK must be present, and for a revocation every one must be absent; otherwise it
re-pushes — a host reboot clears the keyring, so "the last push succeeded" is not trusted on its
own. Each push carries a monotonic `generation`: the Agent rejects stale values and the ledger
rejects stale write-backs (out-of-order or concurrent replays cannot overwrite a newer key with an
older one); after revocation the entry is deleted but the generation tombstone is kept. Deleting a
subsystem also removes all PSKs under it in the host service, and service startup garbage-collects
keys whose subsystem no longer exists.

A pair with no key and no push history is never pushed — no ledger entry, no audit, no false
"sync failed" while its Agent is offline; with nvmet disk creation issuing keys automatically, this
only covers disks on non-nvmet backends (DHHC-1 not applicable, a push would only return `n/a`).
Once a push record exists, later revocations keep being replayed even when undelivered. Records for
unregistered Agents are pruned (re-registration triggers a fresh push). Reconcile-driven pushes
audit only on state/key changes (noise suppression); user-triggered pushes audit every time.
Push outcomes: `ok` delivered (Agent cache and host service both in sync and applied per pair);
`degraded` partially applied (authentication key written, target TLS unavailable so the PSK is not
in effect); `failed` not delivered (Agent unreachable / host sync error), reconciliation keeps
retrying; `n/a` target Agent is not an nvmet backend (not applicable, not a failure); `pending` no
record yet or a stale one (awaiting replay); `none` no sync target (no NVMe-oF disks, no credential
ever set, or the hosting Agent is unregistered).

Operations that trigger a push: **issuance on disk creation** (nvmet backends, see above),
setting/deleting credentials, device bind/unbind/rebind
(`PUT /workers/{worker_id}/mac`), disk delete (including batch), and Worker deletion.

**Periodic rotation**: each reconciliation cycle first checks the DHHC-1 key age
(`rotated_at`, falling back to `updated_at` / `created_at`). Past
`KURRENT_CP_CREDENTIAL_ROTATION_HOURS` (default 24 hours, `<=0` disables it) the key is rotated:
a new key is generated and pushed to every target Agent of that Worker, and the ledger
`state/credentials.yml` is switched **only after all pushes succeed** (`rotated_at` /
`updated_at` refreshed, `credential.rotate` audited). If any target fails or is degraded (TLS not
in effect), already-delivered targets get the old key pushed back and the ledger stays unchanged,
retrying next cycle; entries without an nvmet target are skipped. Until the switch, `/boot-vars` still projects the old key,
so the window in which hosts hold the new key covers only the push itself.

Static storage boundary: the plaintext key lands in exactly four places along the
chain — control-plane `state/credentials.yml`, the Agent container credential cache (which also
holds the derived TLS PSK; both 0600), the storage node configfs (kernel hosts matrix `dhchap_key`),
and the storage node's kernel global `.nvme` keyring (TLS PSK, at runtime). **No application-layer
encryption is introduced**; the boundary is the deployer's responsibility (disk/host persistence
encryption, root access boundary). The key travels over the mTLS component channel and never enters
browser storage or server logs. The data-plane encryption projection (NVMe/TCP TLS 1.3, the firmware
and kernel boot segments sharing port `4421`) is described in [Boot and Device Admission](./boot.md).

Credential boundary (who distributes, who self-holds): the stateless boot chain distributes exactly
two credentials — the DHHC-1 authentication key and the TLS PSK derived from it — plus a non-secret
one-time nonce (`GET /devices/challenge`). The device identity private key is **never distributed**:
it lives in the TPM as a non-exportable AK, and the control plane only records the AK public key
(`identity.ak_public`) and the PCR baseline. Unsigned debugging firmware pins the TOFU fingerprint for the control-plane HTTPS
endpoint on first connect rather than receiving it; signed firmware trusts only the boot CA embedded at build time. Component bootstrap credentials — the bootstrap
token and mTLS certificates — travel over `/enroll` (management plane) and share no channel with the
boot-chain credentials described here. iSCSI/stgt backends authenticate via CHAP, which is not on
this path.

### PUT /workers/{worker_id}/credential

Set or update the key (idempotent: setting the same value again leaves `updated_at` unchanged,
but still triggers one push as a re-delivery channel).

```bash
curl -X PUT "$BASE_URL/workers/worker-01/credential" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"secret": "DHHC-1:01:<base64>"}'
```

Success response (`push` = per-Agent delivery projection, see above; failures are retried by
reconciliation):

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

| HTTP Status | Common Causes |
|---:|---|
| `422` | DHHC-1 validation failed (prefix / digit-count of type / base64 / length 36 or 68 / CRC final value, any mismatch) |
| `404` | Worker not found |

### DELETE /workers/{worker_id}/credential

Delete the key (revokes the authentication and the transport-encryption PSK for all devices bound
to this Worker; the next boot is refused):

```bash
curl -X DELETE "$BASE_URL/workers/worker-01/credential" -H "Authorization: Bearer $TOKEN"
```

Success returns `{"worker_id": "worker-01", "deleted": true, "push": {...}}` (`push.result=ok`
means the revocation was delivered; undelivered revocations are replayed by reconciliation, never
silently stuck); missing Worker or entry → `404`.

### GET /workers/{worker_id}/credential

Query credential metadata and push state (**never returns plaintext**; `secret_hash` only exposes
a prefix for rotation comparison):

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

When there is no entry, `exists=false` and `secret_hash` is absent; the `push` projection is
still returned (revocation delivery stays visible).
CLI: `kurrent workers credential show|set|revoke <worker_id>`; WebUI: the dedicated "NVMe-oF Credential"
tab on the Worker detail page (shown only when the Worker has an NVMe-oF disk).

---
