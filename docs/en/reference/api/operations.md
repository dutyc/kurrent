---
title: "Operations and Verification"
---

# Operations and Verification

The operation log endpoint is listed below, followed by a call sequence from health checks through disk creation to boot.

## GET /operations

### Description

Reads the Control Plane operation audit log. This file is an incremental query interface over `state/operations.jsonl`.

### Query Parameters

| Parameter | Required | Default | Description |
|---|---:|---|---|
| `since` | No | `0` | Only return records with `id > since`. |
| `limit` | No | `1000` | Maximum number of records to return. |
| `mac` | No | — | Only return operations of this device (MAC, normalized then filtered by the `mac` field before `limit` is applied); used for viewing device binding history. |
| `op` | No | — | Only return the given operation type (e.g. `platform.enroll`, `device.bind`), filtered before `limit` is applied. |
| `order` | No | `asc` | `asc` returns records in write order; `desc` returns only the latest page, newest first (for overview queries), with `next_cursor` set to the newest record's `id`. |

### curl

Read from the beginning:

```bash
curl -s "$BASE_URL/operations" \
  -H "Authorization: Bearer $TOKEN"
```

Incremental read:

```bash
curl -s "$BASE_URL/operations?since=10&limit=100" \
  -H "Authorization: Bearer $TOKEN"
```

Latest page (newest first):

```bash
curl -s "$BASE_URL/operations?limit=10&order=desc" \
  -H "Authorization: Bearer $TOKEN"
```

Filter by operation type (platform enrollment records):

```bash
curl -s "$BASE_URL/operations?op=platform.enroll&order=desc" \
  -H "Authorization: Bearer $TOKEN"
```

### Example Success Response

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

## Typical Test Sequence

It is recommended to verify in this order (all examples use `worker-01` and device `00:0c:29:b9:8b:2d`):

### Check Service Liveness

```bash
curl -s "$BASE_URL/healthz"
```

### Check Boot-Chain Status and Boot Variable Projection

```bash
curl -s "$BASE_URL/boot-assets" -H "Authorization: Bearer $TOKEN"
curl -s "$BASE_URL/boot-vars?mac=000c29b98b2d&hostname=worker-01"
curl -s "$BASE_URL/boot-vars?mac=000c29b98b2d&hostname=worker-01&format=json"
```

### Check Agent Configuration and Capabilities

Note: `config/agents.yml` is read from inside the Control Plane container. If an Agent is on the same host as the Control Plane, you cannot use `http://localhost:4840` because `localhost` inside the container points to the Control Plane container itself.

The default Compose file already configures:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

Therefore, for an Agent on the same host, configure it as:

```yaml
base_url: http://host.docker.internal:4840
```

```bash
curl -s "$BASE_URL/agents?live=true" \
  -H "Authorization: Bearer $TOKEN"
```

### Prepare the Device and Register the Worker

The device must enter the device pool first (automatic enrollment via the registration window is described in [Boot and Device Admission](./boot.md); manual registration below):

```bash
curl -s -X POST "$BASE_URL/devices" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "mac": "00:0c:29:b9:8b:2d"
  }'
```

Then register the Worker and bind the device directly:

```bash
curl -s -X POST "$BASE_URL/workers" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "worker_id": "worker-01",
    "mac": "00:0c:29:b9:8b:2d"
  }'
```

At this point, the Worker is bound to the device but has no system disk (`state=registered`, `disks` is an empty array, `readiness=partial`).

### Create a System Disk for worker-01 (empty disk)

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

After the disk is created, `state` transitions from `registered` to `ready`; the first disk automatically becomes `default_disk`, and the returned `os_tag` identifies that default boot disk.

### Set Default Boot Configuration (optional)

The first disk is already the default, so this call is normally unnecessary. For multiple disks or to override the menu item/timeout:

```bash
# disk = the os_tag returned by the disk-creation response (12 hex chars, precisely references a specific disk)
curl -s -X PUT "$BASE_URL/workers/worker-01/default-disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "disk": "5f1c2a3b4d5e"
  }'
```

`/boot-vars` will now return `menu-default=boot-os` (the MAIN MENU OS item has converged to a single generic item). See [setting the default disk](./disks.md) for examples of setting menu items and timeout.

### Query Worker Inventory

```bash
curl -s "$BASE_URL/workers/worker-01" \
  -H "Authorization: Bearer $TOKEN"
```

### Query Real-Time Status

```bash
curl -s "$BASE_URL/workers/worker-01/status" \
  -H "Authorization: Bearer $TOKEN"
```

### Delete Worker, Keeping the Empty Disk File

```bash
curl -s -X DELETE "$BASE_URL/workers/worker-01?delete_disk=false" \
  -H "Authorization: Bearer $TOKEN"
```

Deletion also unbinds the device (returns it to the pool without revoking it) and clears the Worker’s credential entries; keeping the `.img` supports the workflow where you create an empty disk, then manually rename it to a master image.

---
