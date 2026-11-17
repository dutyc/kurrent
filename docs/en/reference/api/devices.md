---
title: "Device Pool"
---

# Device Pool

The device ledger is the underlying entity for binding and boot authorization. The endpoints below query, import, bind, revoke and attest devices.

## Device Pool (Device Inventory)

The device inventory (`state/devices.yml`) is the bottom-layer entity of the three-layer entity model (device / Worker / system disk): auto-registration and manual import only enter the device pool and grant no boot authority; a device gains its identity only after binding to a Worker. The binding relationship is authoritative on the device side (`bound_worker_id`); the Worker side only projects it and does not store it.

**Binding semantics**: device ↔ Worker is strictly **one-to-one**. A device binds via `POST /devices/{mac}/bind`; `force=true` performs an atomic rebind (pre-check → new binding persisted → old binding cleared → rollback on failure). Unbinding (`DELETE /devices/{mac}/bind`) returns the device to the pool; the Worker's system disks are kept. Deleting a Worker cascades an unbind (11). `POST /workers` with a `mac` also binds directly (7).

**Readiness projection**: Worker responses (list / detail / status) derive two fields from the ledger:

- `bound_device`: the MAC of the device bound to this Worker (`null` if none)
- `readiness`: `ready` (bound device + at least one system disk) / `partial` (bound device **or** system disk) / `idle` (neither)

### Device States

| State | Description |
|---|---|
| `pooled` | In the device pool, unbound (window-enrolled / manually registered / bulk imported) |
| `bound` | Bound to a Worker (one-to-one); see `bound_worker_id` |
| `revoked` | Revoked; no longer accepts reports and cannot be re-registered |

### Record Structure (Example)

```yaml
devices:
  "00:0c:29:b9:8b:2d":
    mac: 00:0c:29:b9:8b:2d
    uuid: "4c4c4544-..."          # SMBIOS UUID (dual factor, optional)
    state: pooled                 # pooled | bound | revoked
    bound_worker_id: null         # authoritative here; worker side only projects
    identity:                     # TPM identity (filled by an AK report/claim; required by attestation enforcement)
      kind: tpm_ak
      ak_public: 3059301306072a8648ce3d0201...   # AK public key, SPKI DER hex
      ak_name: 000b6f2a...                        # TPM2B_NAME hex (alg + public-key digest)
      ak_type: ecc_p256                           # ecc_p256 | rsa2048
    pcr_baseline:                 # recorded on the first successful attestation
      selection: [0, 2, 4, 7]
      digest_sha256: 9f2c...7b1d
      recorded_at: 2026-08-15T10:05:00+08:00
    attestation:                  # result of the latest attestation
      state: proven               # unproven | proven | mismatch
      last_at: 2026-08-15T12:00:00+08:00
      last_reason: null           # ak_unregistered | quote_invalid | nonce_invalid | pcr_mismatch | baseline_approved
    source: ipxe                  # ipxe (auto) | manual (manual entry/import)
    fingerprint:                  # reported values; updated by device reports
      manufacturer: ASUSTeK COMPUTER INC.
      product: ROG Zephyrus G15
      serial: "..."
      cpumodel: "Intel(R) Core(TM) Ultra 7 155H"
      mem_total: 32768            # normalized decimal (accepts 0x hex reports)
      mem_type: DDR5
      mem_speed: 5600
      chip: RTL8125
      busid: "0110ec8125"
    first_seen: 2026-08-15T10:00:00+08:00
    last_seen: 2026-08-15T12:00:00+08:00
```

### GET /devices

Device pool list with `state` filtering (`all` / `pooled` / `bound` / `revoked`, default `all`).

**curl**:

```bash
curl "$BASE_URL/devices?state=pooled" \
  -H "Authorization: Bearer $TOKEN"
```

**Successful response**: an array; each item is a device record (see “Record Structure” above).

### GET /devices/{mac}

Single device detail (bound Worker, fingerprint, first/last report). `mac` uses the colon format (`00:0c:29:b9:8b:2d`).

**404**: device not found.

### POST /devices

Manually register a device: MAC (+ optional UUID / manufacturer / product / serial) into the pool.

**Request body**:

| Field | Required | Type | Description |
|---|---|---|---|
| `mac` | Yes | str | MAC address |
| `uuid` | No | str | SMBIOS UUID (dual factor, optional) |
| `manufacturer` / `product` / `serial` | No | str | Reported info; initial inventory values only, superseded by device reports |

**Successful response (201)**: device record (`state=pooled`, `source=manual`).

**409**: device already exists (including revoked — revoked devices cannot be re-registered).

### POST /devices/import

Bulk import a device manifest (pre-import of the MAC manifest): each entry is independent; duplicates are skipped; invalid/revoked entries count as `failed`.

**Request body**:

| Field | Required | Type | Description |
|---|---|---|---|
| `entries` | Yes | array | Manifest array; each entry follows the `POST /devices` request body (`mac` required) |

**Successful response**:

| Field | Description |
|---|---|
| `created` | Entries newly added to the pool (each `{mac}`) |
| `skipped` | Already existing (pooled/bound) entries and reasons |
| `failed` | Invalid MAC / revoked entries and reasons |

### DELETE /devices/{mac}

Revoke a device: `pooled` → `revoked`; the record stays in the inventory (audit retention).

Revocation also writes `MAC,ignore` into that device's dnsmasq binding file and removes its static reservation, so it no longer receives a DHCP lease (the first denial happens at the network relay, not at the unauthenticated endpoints). A file write failure does not roll back the revocation but records a `device.dhcp_block` error event.

**409**: device is bound to a Worker (must unbind first) or already revoked.

### DELETE /devices/revoked

Purges revoked devices (`revoked` → entry deleted); `dry_run=true` only returns the MACs that
would be purged and leaves the ledger untouched. Only revoked entries are removed — `pooled` /
`bound` devices are never touched; an empty list is returned when there is nothing to purge
(idempotent). Intended for cleaning up probe leftovers or mistakenly revoked devices.
Repeat `mac` to scope the purge (e.g. `?mac=02:00:00:00:00:01&mac=02:00:00:00:00:aa`; MACs that
are not revoked are ignored); without it every revoked device is purged. Purging also removes the
MAC's `MAC,ignore` deny line from the dnsmasq binding file (`dry_run` touches no files).

```bash
curl -s -X DELETE "$BASE_URL/devices/revoked?dry_run=true" -H "Authorization: Bearer $TOKEN"
curl -s -X DELETE "$BASE_URL/devices/revoked?mac=02:00:00:00:00:01&mac=02:00:00:00:00:aa" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{ "purged": ["02:00:00:00:00:01", "02:00:00:00:00:aa"], "count": 2, "dry_run": false }
```

### GET /devices/report

iPXE device info reporting endpoint (**no auth**; `boot.ipxe.cfg` chains it before `/boot-vars`): updates the fingerprint + `last_seen`; an unknown MAC enters the pool only while the registration window is open, the admission quota is not used up, a valid TPM AK is carried, and Secure Boot is on. For an existing device, `uuid` can only fill an empty value and is never overwritten by a reported value (the binding comparison basis is not writable through an unauthenticated endpoint). **Returns an empty script body** (`#!ipxe\n`; consumable directly by `chain`, no script side effects; an empty body is reported as EOF by some iPXE builds, so a legal empty script is returned uniformly).

| Parameter | Required | Description |
|---|---|---|
| `mac` | Yes | MAC; accepts colon format (`${mac}`) and compact hex (`${netX/mac}`) |
| `uuid` / `manufacturer` / `product` / `serial` / `cpumodel` / `mem-type` / `chip` / `busid` | No | String fields; empty values tolerated |
| `mem-total` / `mem-speed` | No | Integer; accepts `0x` hex and decimal; stored normalized as decimal |
| `ak_public` | No | AK public key, SPKI DER hex (`ecc_p256` or `rsa2048`), used for enrollment/claim; absent or invalid → no identity |
| `ak_name` | No | TPM2B_NAME hex (68 hex: algorithm prefix + public-key digest), paired with `ak_public`; absent or invalid → no identity |
| `ak_type` | No | `ecc_p256` / `rsa2048`; required alongside `ak_public` and must match the actual key algorithm — missing or mismatched means no identity |
| `sb` | No | Self-reported Secure Boot state, `1` = on; an unknown MAC is admitted only with `sb=1` (the boot-time decision relies on attestation, never on this self-report) |

Behavior:

- Registered device: updates the fingerprint (non-empty fields overwrite) + `last_seen`; `state` unchanged; while the window is open, a valid AK fills in `identity` (AK claim, not constrained by the admission quota); an AK mismatch is rejected (only audited as `device.claim rejected`)
- Revoked device: ignored (not updated, not resurrected)
- Unknown MAC + window open + quota left + valid AK + `sb=1`: enters the pool (`state=pooled`, `identity` written, `source=ipxe`, audited as `device.ak.register`)
- Unknown MAC + window closed / quota used up / no valid AK / `sb!=1`: no pool entry and no ledger write (audited as `device.register rejected` with reason `window_closed` / `no_tpm` / `sb_disabled`; enrollment only during the window and bounded by the admission quota, see [Boot chain](./boot.md))

**Rate limit**: sliding window per source IP (`X-Real-IP`, overwritten by nginx), 120 requests/minute by default; over the limit returns `429` (with `Retry-After`) and emits a `boot_rate_limited` alarm. Override with `KURRENT_CP_RATE_LIMIT_REPORT`, `<=0` disables. This is scan suppression, not an identity boundary.

**curl** (simulating an iPXE report; the boot chain uses the **bare path** — no `/api/cp` prefix — and requires no authentication):

```bash
curl "https://<host>/devices/report?mac=000c29b98b2d&uuid=4c4c4544-...&manufacturer=ASUSTeK%20COMPUTER%20INC.&product=ROG%20Zephyrus%20G15&cpumodel=Intel(R)%20Core(TM)%20Ultra%207%20155H&mem-total=0x8000&mem-type=DDR5&mem-speed=5600&chip=RTL8125&busid=0110ec8125&ak_public=<SPKI-DER-hex>&ak_name=<TPM2B-NAME-hex>&ak_type=ecc_p256&sb=1"
```

### POST /devices/{mac}/bind

Binds a device to a Worker (one-to-one authorization). **409** by default when the device or the Worker is already bound; `force=true` performs an **atomic rebind**: pre-check → new binding persisted → old binding cleared (old device back to the pool) → on failure the ledger snapshot is restored and dnsmasq is best-effort restored (see “Implementation Boundaries”, §17). Idempotent: re-binding the same device to the same Worker returns `200` without changes.

| Query Parameter | Required | Default | Description |
|---|---:|---|---|
| `worker_id` | Yes | — | Target Worker. |
| `force` | No | `false` | Atomic rebind when device or Worker is already bound. |

**404**: device or Worker not found. **409**: device revoked / already bound (without `force`) / Worker already bound (without `force`) / dnsmasq conflict.

Rebind scenarios with `force=true`:

- Device bound to `worker-01`, rebind to `worker-02` → device moves; `worker-01` becomes idle (no device)
- `worker-02` also bound another device → that device is released back to the pool
- Device already bound to `worker-02` and `worker-02` bound to this device → idempotent success

The audit trail records `device.bind` with `old_worker_id` / `old_device_mac` (rebind history).

**curl**:

```bash
curl -X POST "$BASE_URL/devices/00:0c:29:b9:8b:2d/bind?worker_id=worker-01&force=false" \
  -H "Authorization: Bearer $TOKEN"
```

**Successful response (200)**: the device record (`state=bound`, `bound_worker_id=worker-01`).

### DELETE /devices/{mac}/bind

Unbinds a device: back to the pool (`state=pooled`, `bound_worker_id=null`), the dnsmasq binding is removed and reloaded; the Worker's system disks are kept (its `readiness` degrades to `partial`/`idle`).

**409**: device not bound. **404**: device not found.

**curl**:

```bash
curl -X DELETE "$BASE_URL/devices/00:0c:29:b9:8b:2d/bind" \
  -H "Authorization: Bearer $TOKEN"
```

### POST /devices/bind/batch/preview

Bulk bind **preview** (read-only, no writes): pairs the manifest into a pairing table.

**Request body**:

| Field | Required | Description |
|---|---:|---|
| `mode` | No | `manifest` (default): use `pairs`; `sequential`: pair `macs[i]` ↔ `worker_ids[i]` by index (lengths must match, otherwise `400`). |
| `pairs` | No | Array of `{mac, worker_id, manufacturer?, product?, serial?, uuid?}`; the optional fields are **declaration columns** compared against the reported fingerprint (see below). |
| `macs` / `worker_ids` | No | Used by `mode=sequential`. |

Classification per entry (independent, no whole-batch rejection):

- `matched`: device pooled + Worker exists + Worker unbound (includes `device_state`, `worker_state`, `fingerprint_mismatch`)
- `conflicts`: device already bound / Worker already bound / duplicate MAC in manifest / Worker not found
- `not_found`: device out of pool (`device not in pool`), revoked, or invalid MAC

`fingerprint_mismatch` is `null` when consistent; otherwise `{"fields": ["serial", ...]}` — declaration values that differ from reported values (both non-empty). It is advisory and does not block binding.

**Successful response**: `{matched: [...], conflicts: [...], not_found: [...], summary: {total, ok, conflict, not_found}}`.

### POST /devices/bind/batch

Bulk bind **execution** (idempotent, per-entry independent; failures of one entry do not affect the rest). Same request body as the batch-bind preview. Classification:

- `succeeded`: bound now (with `fingerprint_mismatch` marker if declared values differ)
- `skipped`: already bound (same Worker) / device already bound to another Worker / Worker already bound / duplicate MAC in manifest
- `failed`: device not found (out-of-pool — import first via `POST /devices` or bulk import) / invalid MAC

**Audit**: besides the `device.bind.batch` summary, every `succeeded` entry also records a per-device `device.bind` (`mac` / `worker_id`), keeping device binding history (`GET /operations?mac=`) complete; `skipped` / `failed` are only counted in the summary.

**Successful response**: `{succeeded: [...], skipped: [...], failed: [...]}`. Re-running an already-completed manifest returns everything as `skipped`.

**curl**:

```bash
curl -X POST "$BASE_URL/devices/bind/batch" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"mode":"manifest","pairs":[{"mac":"00:0c:29:b9:8b:2d","worker_id":"worker-01"}]}'
```

## POST /devices/{mac}/approve-baseline

Approves the device's most recently reported PCR baseline (**admin**, requires `Authorization: Bearer`). After a firmware or boot-configuration change, the quote's PCR digest no longer matches the stored baseline, `/boot-vars` denies with `pcr_mismatch`, and that digest is kept in `attestation.pending_digest` awaiting approval. After reviewing the change, the admin calls this endpoint to write the pending digest as the new `pcr_baseline`; the device's next boot is validated against it.

No request body: the approved value is always the latest pending digest reported by the device, so the admin never types a digest by hand.

**Errors**: unknown device `404`; revoked device `403`; no pending digest (no mismatch has occurred yet) `409`.

```bash
curl -s -X POST "$BASE_URL/devices/00:0c:29:b9:8b:2d/approve-baseline" \
  -H "Authorization: Bearer $TOKEN"
```

**Success (200)**: the updated device record (`pcr_baseline` holds the new digest; `attestation.state=unproven`, `last_reason=baseline_approved`, `pending_digest` cleared). Recorded as operation `device.pcr_baseline.approve` (with digest and source address).

The AK itself cannot be rotated online (its private key is non-exportable): recovery or a mainboard swap goes through the admin flow of deleting the inventory entry and re-enrolling during a registration window.

---
