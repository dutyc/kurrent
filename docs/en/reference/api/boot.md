---
title: "Boot and Device Admission"
---

# Boot and Device Admission

Device boot uses the following endpoints: boot-chain status, boot variable projection, the registration window, and challenge-attestation.

## GET /boot-assets

### Description

Boot-chain status (requires management authentication: `human` or `automation`). The control plane parses `dhcp-boot` in `dnsmasq.conf` and the `boot.ipxe → boot.ipxe.cfg → menu.ipxe` chain to answer three questions: whether the UEFI firmware is present with the capabilities the scripts require, whether the script chain is complete, and whether the menu is parsable. It reports status only: no menu entries are expanded and no directory browsing is performed. The control plane must have `./tftp` mounted read-only (`404` when `dnsmasq.conf` is missing).

### Response Fields

| Field | Description |
|---|---|
| `firmware[]` | UEFI firmware declared by `dhcp-boot=tag:efi64`: `mode` (always `uefi`), `label`, `file`, `path`, `host_path`, `status` (ok/incomplete/missing/empty/rejected), `size`, `mtime`; when readable, also `version` and `capabilities[]` (`id`/`label`/`present`) |
| `stage2[]` | Script-chain files (`boot.ipxe`, `boot.ipxe.cfg`, `menu.ipxe`): `file`, `path`, `host_path`, `status`, `size`, `mtime` |
| `menu` | `status` (ok/unavailable) and `items` (selectable item count, skipping `--gap` lines) |
| `secureboot` | Optional Secure Boot artifacts: `present`, `status` (ok/incomplete/missing), `artifact`, `path`, `artifact_sha256`, `artifact_size`, `manifest_present`, `manifest_matches`, `signer`, `built_at`, `boot_https_port`, `port_matches`, `boot_ca_present`, `boot_ca_sha256`, `boot_ca_matches` |

`status=incomplete` means the firmware file exists but lacks any of `HTTPS` / `efi_nvs` / `akgen` / `akpub` / `quote` / `TOFU` — stock iPXE cannot complete TPM attestation either. `status=empty` is a zero-byte file, `missing` means the file does not exist, and `rejected` means the path escapes the tftp root (the control plane refuses it and never reads it). `host_path` is the host path relative to the deployment root (`tftp/...`), used when adding a missing file. `menu.status=ok` requires at least one `menu` section and one non-`--gap` `item`. `secureboot.status=ok` requires the signed image, a matching manifest hash, a boot HTTPS port baked into the image that equals the deployed `KURRENT_BOOT_HTTPS_PORT`, a present boot CA certificate, and a manifest CA fingerprint that matches the current control-plane boot CA (a wrong port cannot be reached, a wrong anchor is refused by the firmware); without `tftp/secureboot/` artifacts `present=false` and the other checks are unaffected.

### curl

```bash
curl --cert client.crt --key client.key https://<host>/api/cp/boot-assets
```

---

## GET /boot-vars

### Description

Provides per-worker boot variables for iPXE boot scripts. This endpoint does not require authentication and only exposes variables needed for booting within a controlled network. NVMe-oF credentials (`nbft_secret` / `nbft_tls_psk`) are **injected only after TPM attestation passes** (`mac` + one-time `nonce` + a PCR quote bound to that nonce, see this section).

> **Note**: This endpoint is **read-only** (a projection of boot variables, no write side effects). Registration-window enrollment (new MACs entering the device pool with a TPM AK) has been consolidated into `GET /devices/report` (see [Devices](./devices.md)), which `boot.ipxe.cfg` chains **before** `/boot-vars`.

**Rate limit**: sliding window per source IP (240 requests/minute by default; override with `KURRENT_CP_RATE_LIMIT_BOOT_VARS`, `<=0` disables). Over the limit returns `429` with `Retry-After` and emits a `boot_rate_limited` alarm. Attestation verification is the most expensive work here; the limit only suppresses scanning and is not an identity boundary.

The Control Plane identifies the device and projects boot variables in the following order:

1. `mac` → `state/devices.yml` (device inventory) → `bound_worker_id` → `state/workers.yml`
2. `hostname` → `state/workers.yml` (by hostname or worker_id; fallback when no MAC is supplied or the device is not in the inventory)
3. `config/agents.yml` (data-plane address of the default boot disk’s Agent)

Then returns the Worker's data-plane address, boot disk NQN, default menu item, and menu timeout.

By default, it returns an iPXE script snippet for maximum compatibility, which can be executed directly by iPXE’s `chain`. Add `format=json` to receive JSON (useful for manual debugging).

### Field Origins

The response of `/boot-vars` is a projection of the inventory:

| Response Field | Origin |
|---|---|
| `worker_id` | The identified Worker (binding authority first; not returned for a pooled, unbound device or an empty script) |
| `disk_nqn` | The full NQN of the default boot disk (same disk selection rule; disk record `nqn` — the data-plane unique identifier and the authoritative value from the control plane) — the firmware splices it verbatim into `sanboot nvme://<ip>:<data-port>/${disk-nqn}` and no longer reassembles it from hostname/os/os-tag. Not returned when the disk record lacks `nqn` or when the Worker has no system disk. |
| `base_iqn` | The IQN of the Worker’s default boot disk (the disk for `default_disk`; if not set, the first disk) with the last colon-and-following part stripped — the disk NQN is the authoritative identifier, the IQN is derived from it. **Not returned** if the Worker has no system disk (iPXE falls back to the static default in `boot.ipxe.cfg`). |
| `storager_ip` | The `storager_ip` of the Agent that hosts the default boot disk (same disk selection rule as above) — the data-plane address used by NVMe-oF boot. Not returned when there is no system disk. |
| `iscsi_sep` | The iSCSI root **separator** (the part between `${storager-ip}` and `${base-iqn}`). **Returned for stgt / LIO backends only**: `:::1:` for stgt backends (lun placeholder 1), `::::` for LIO backends (empty placeholder). The backend type is resolved by **querying the Agent’s `/capabilities` `backend` field in real time** (the node reports its own capability — capability is truth); if the agent is unregistered / the query fails / the value is invalid, the backend is treated as unknown, a warning is logged, and this field is omitted (it no longer falls back to stgt format). It is **not derived from the Agent’s `tags`** in `agents.yml` (tags are human annotations only — otherwise a drifting tag would assemble a wrong separator). **Not sent for nvmet backends**. Not returned when there is no system disk. The current boot scripts do not consume this field; it is retained for API clients of the iSCSI compatibility backend. |
| `nbft_secret` | The Worker’s NVMe-oF authentication key (DHHC-1, `state/credentials.yml` indexed by worker_id, see [System disks](./disks.md)). **Injected only when the Worker is bound, has a key entry, and TPM attestation passes**; not returned when there is no key / the Worker is unbound / the request is unverified / the request is denied. Consumed by the firmware as `nvme://...?secret=${nbft-secret}` (on nvmet backends a missing key withholds the whole `/boot-vars` payload and the menu refuses to boot) |
| `nbft_tls_psk` | Pre-shared key for NVMe/TCP TLS: a **pure function of the authentication key** (`HMAC-SHA256(raw_key, b"kurrent-nvme-tls-psk-v1")` → 64 hex, see [System disks](./disks.md); no separate storage field — rotating or revoking the key rotates or revokes the PSK atomically). Returned **only when all four hold: the Worker is bound with a key, TPM attestation passes, the backend is nvmet, and the Agent reports a ready data-plane port**; otherwise the whole boot-variable payload is withheld (boot refused, audited) — encryption is the default and there is no plaintext fallback. The firmware uses it for its own handshake on that port and hands it to the initramfs via the NBCT table, which reconnects with `--tls` on the same port |
| `nbft_tls_port` | Data-plane port (shared by the firmware and kernel boot segments; read live from the Agent’s `/capabilities.port.trsvcid` — capability is truth, same live-query rule as `iscsi_sep`/`backend`); returned under the same conditions as `nbft_tls_psk`. The menu appends `&tls_psk=${nbft-tls-psk}&tls_trsvcid=${nbft-tls-port}` to the sanboot URI |
| `hostnqn` | The Worker’s initiator Host NQN (disk NQN prefix + `:host.<worker_id>`, in the same namespace as the disk NQN and matching the host entry registered in nvmet-host); **injected when the default disk carries an NQN**. Consumed by the firmware: iPXE nvmetcp defaults hostnqn to `nqn.2014-08.org.ipxe:<uuid>` (falling back to `:ipxe` without a UUID); a mismatch against the registered value fails strict-mode authentication, so it must be overridden with this field (firmware patch `0005` supports the `hostnqn` setting) |
| `os` | The OS name of the Worker’s default boot disk (the disk for `default_disk`; if not set, the first disk; the disk record is authoritative) — the menu keeps a single generic OS item (`boot-os`), and this variable is display-only for the menu label; the data-plane path comes from `disk_nqn`. Not returned when the Worker has no system disk (reboot loop waits for disk creation). |
| `os_version` | Version annotation of the default boot disk (disk record `os_version`; `''` = no version) — omitted when empty; the `os-label` assembly in menu.ipxe adapts (shows `os version` when present). Not returned when the Worker has no system disk. |
| `menu_default` | Derived chain: `boot.menu_default` (explicit override, highest priority) > `workers.yml` `default_disk` (the first disk is set as default automatically; use [setting the default disk](./disks.md) for multi-disk, os_tag references a specific disk) > `reboot` (loop reboot waiting when not configured). Setting `boot.menu_default` (`config` / `shell` / `reboot` / `exit`) overrides the default disk and selects that item; when unset it falls back to the `default_disk` `boot-os` (the only OS item); when neither is set it is `reboot` |
| `menu_timeout` | When a default boot is configured: `boot.menu_timeout` > `spec.boot.menuTimeoutMs` (default 5000). When in `reboot` loop: always uses `spec.boot.autoBootTimeoutMs` (default 1). Units are milliseconds. |

Worker lookup rules (**binding authority first**):

```text
mac -> devices.yml (device inventory) -> bound_worker_id -> workers.yml
mac missing or not found -> hostname -> workers.yml (by hostname or worker_id)
mac not found and hostname not found -> dnsmasq projection (legacy / no-inventory fallback)
not identified and mac provided ->
  - device in pool (pooled) -> reboot loop (menu-default=reboot + short timeout), waiting for binding
  - device revoked -> empty script (menu stays)
  - unknown MAC + registration window open + quota left + a valid AK + sb=1 -> enter the device pool (see “Registration-Window Enrollment” below), return reboot loop
  - unknown MAC + window closed / quota used up / no AK / Secure Boot off -> empty script (menu stays; no pool entry, no ledger write)
```

### Default Boot Item Rules

The default boot item is derived by `/boot-vars` in the following order:

```text
boot.menu_default (explicit override, highest priority) -> default_disk (the first disk is set as default automatically; use [setting the default disk](./disks.md) for multi-disk) -> reboot (not configured)
```

- With a single system disk no extra call is needed: disk creation writes `default_disk`. When multiple disks coexist, call `PUT /workers/{worker_id}/default-disk` to choose the default boot disk (os_tag references a specific disk):
  ```text
  disk=<os_tag> -> menu_default=boot-os, os=<disk os>, os-version=<disk os_version if any>, disk_nqn=<disk nqn>
  ```
  The MAIN MENU keeps a single generic OS item `boot-os`, derived from the default disk; `os` / `os-version` are display-only and `disk_nqn` carries the boot identity consumed by the generic item.
- Alternatively, use `boot.menu_default` to specify a default item on the iPXE menu (e.g., the configuration screen `config`, or `exit`) — it overrides `default_disk`; clearing that override (`{"menu_default": null}`) returns to the default disk.
- When neither is configured, `menu_default` returns `reboot` (a short-timeout reboot loop waiting for the admin to create a disk / set a default disk; `exit` is only used when explicitly configured).

### Registration-Window Enrollment (Zero-touch Provisioning)

When a new device boots without an identity, iPXE first `chain`s `/devices/report` (11 fingerprint fields + TPM AK and Secure Boot state, see [Devices](./devices.md)) and then requests `/boot-vars`. **Enrollment only happens during the registration window**: only when the window is open, the admission quota is not used up, the report carries a valid TPM AK (`ak_public` / `ak_name` / `ak_type`), and it self-reports `sb=1` does an unknown MAC enter the device pool; after the window closes, reports from unknown MACs are neither pooled nor written to the ledger, leaving only a rejection audit record (there is no enrollment channel outside the window, and a permanent window cannot be configured at the code level). Devices without a TPM, or with Secure Boot off, are not admitted.

1. `GET /devices/report`: unknown MAC + window open + quota left + valid AK + `sb=1` → written to `state/devices.yml` (`state=pooled`, fingerprint stored, `identity` records the AK, `source=ipxe`); otherwise audited as `device.register rejected` (`no_tpm` / `sb_disabled` / `window_closed`)
2. `GET /boot-vars`: MAC in the pool and unbound → returns `menu-default=reboot` + short timeout, rebooting in a loop until the admin binds it
3. After the admin binds the device to a Worker (WebUI / API; single or batch binding (see [Device Pool](./devices.md))), the next boot follows the Worker configuration normally

AK claim for existing devices: an already-pooled device reporting with an AK during the window gets its `identity` written (claim complete); an AK mismatch is rejected (only audited as `device.claim rejected`; recovery is the delete-and-re-register flow). A claim depends only on the window being open and is not constrained by the admission quota. Once claimed, the device must pass TPM attestation once; the first success records the PCR baseline (see this section).

Controls:

| Item | Default | Description |
|---|---:|---|
| Registration window | Closed | Open it with `POST /settings/registration-window` during deployment (TTL hard cap 1-60 min, auto-closes on expiry), see this section |
| Admission quota | Unlimited | Optional `max_devices` when opening (1-1000 devices); once that many devices have auto-joined within the window, admission stops, see this section |
| Attestation enforcement | On | `GET/PUT /settings/enforcement`: on by default, so `/boot-vars` is only served to bound devices that pass TPM attestation; turning it off is for transition/troubleshooting only, see this section |
| `spec.boot.autoBootTimeoutMs` | `1` | Menu timeout in milliseconds during the reboot loop. |

Enrollment and claiming are logged as operations (`device.register` / `device.claim` / `device.ak.register`). On failure nothing is written and an empty script is returned, so the next request will retry without affecting the iPXE boot process.

### Anti-Impersonation (Binding as Authentication)

When a request carries a `mac`, the Control Plane verifies the Worker that the device is **bound to** (`bound_worker_id`, the binding authority); if it does not match (device bound to another Worker / unbound / unknown), the request is **denied** — an empty script is returned and the boot vars are not leaked. Requests without a `mac` (hostname only) cannot be verified: with attestation enforcement on (the default) they are **rejected** (`missing_mac`); when it is explicitly off, only menu/navigation variables are served and credentials are never injected. This makes the device↔Worker binding and TPM attestation the boot-time authentication boundary: only the bound device, after attestation passes, can receive the Worker's boot configuration and data-plane credentials (e.g., `disk_nqn` / `nbft_secret` / `nbft_tls_psk`).

### Query Parameters

| Parameter | Required | Default | Description |
|---|---:|---|---|
| `mac` | No | None | MAC address. The backend normalizes it by stripping `:` / `-` / `.`. Both colon-separated (`00:0c:29:b9:8b:2d`) and `mac:hexraw` (`000c29b98b2d`) formats are supported. |
| `hostname` | No | None | Hostname, e.g., `worker-01`. |
| `format` | No | `ipxe` | `ipxe` or `json`. |
| `nonce` | No | None | One-time challenge-response nonce (issued by `/devices/challenge`, see this section); with enforcement on (the default), missing → `quote_invalid` rejected |
| `quote` | No | None | Raw TPMS_ATTEST bytes as hex (without the 2-byte TPM2B_ATTEST length prefix): a quote over PCR 0/2/4/7 (SHA-256) whose extraData must equal the ASCII bytes of the `nonce` string (see this section); missing or malformed → `quote_invalid` rejected |
| `quote_sig` | No | None | Signature over the quote by the AK private key (hex; DER for ECC P-256, PKCS#1 v1.5 for RSA-2048); a bad signature → `quote_invalid` rejected |

It is recommended to pass at least one of `mac` or `hostname`. From iPXE, it is best to pass both:

```text
/boot-vars?mac=${mac}&hostname=${hostname}
```

> **Note**: Although `${mac:hexraw}` is technically equivalent to `${mac}` (both are normalized), some real iPXE firmware expands the `hexraw` modifier incorrectly (possibly empty). In practice, you must use colon-separated `${mac}` — do not change it back to `hexraw`.

### curl (iPXE Format)

```bash
curl -s "$BASE_URL/boot-vars?mac=000c29b98b2d&hostname=worker-01"
```

Example success response:

```ipxe
#!ipxe
# boot vars for worker-01
set disk-nqn nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89
set base-iqn iqn.2026-07.com.kurrent
set storager-ip 192.168.80.3
set nbft-secret DHHC-1:01:<base64>   # only when NVMe-oF authentication is enabled (see "System Disks and Credentials")
set nbft-tls-psk <64 hex>            # derived from the authentication key
set nbft-tls-port 4421
set hostnqn nqn.2026-07.com.kurrent:host.worker-01
set os ubuntu
set menu-default boot-os
set menu-timeout 5000
```

Registered but no default boot configured (no system disk / `default_disk` explicitly cleared / no explicit `boot.menu_default`):

```ipxe
#!ipxe
# boot vars for worker-01
set menu-default reboot
set menu-timeout 1
```

New MAC (window enrollment) or completely unrecognized, and if the window is closed or no AK is carried, returns an empty script:

```ipxe
#!ipxe
# no per-worker boot vars found
```

### curl (JSON Format)

```bash
curl -s "$BASE_URL/boot-vars?mac=000c29b98b2d&hostname=worker-01&format=json"
```

Example success response:

```json
{
  "worker_id": "worker-01",
  "disk_nqn": "nqn.2026-07.com.kurrent:worker-01.ubuntu.0d26b6f33a89",
  "base_iqn": "iqn.2026-07.com.kurrent",
  "storager_ip": "192.168.80.3",
  "nbft_secret": "DHHC-1:01:<base64>",
  "nbft_tls_psk": "<64 hex>",
  "nbft_tls_port": "4421",
  "hostnqn": "nqn.2026-07.com.kurrent:host.worker-01",
  "os": "ubuntu",
  "menu_default": "boot-os",
  "menu_timeout": 5000
}
```

Registered but no default boot configured:

```json
{
  "worker_id": "worker-01",
  "menu_default": "reboot",
  "menu_timeout": 1
}
```

A device in the pool but unbound to a Worker receives menu variables only (no `worker_id`):

```json
{
  "menu_default": "reboot",
  "menu_timeout": 1
}
```

Unrecognized and not enrolled (window closed / no AK):

```json
{}
```

### iPXE Integration

In `tftp/boot.ipxe.cfg`, the trust chain of the unsigned debugging scripts fetches this endpoint over HTTPS (nginx 443, with the leaf fingerprint pinned by TOFU) after the challenge-respond handshake (see this section); signed firmware fetches it over 4443 and trusts only the boot CA embedded at build time.

```ipxe
# Stage 3: challenge-attest — a non-secret nonce binds this boot
:challenge
chain --autofree ${https-url}devices/challenge?mac=${mac} && goto quoted || goto bootvars

:quoted
quote ${nonce} || goto bootvars
chain --autofree ${https-url}boot-vars?mac=${mac}&hostname=${hostname}&nonce=${nonce}&quote=${quote}&quote_sig=${quote-sig} || goto vars-done
goto vars-done

# Degraded path (challenge failed: no registered AK, or legacy firmware):
# enforcement is on by default, so a missing quote is rejected as quote_invalid;
# served only when enforcement is explicitly off
:bootvars
chain --autofree ${https-url}boot-vars?mac=${mac}&hostname=${hostname} || goto vars-done

:vars-done
isset ${iscsi-sep} || set iscsi-sep :::1:
isset ${hostname} && set initiator-iqn ${base-iqn}:${hostname} || set initiator-iqn ${base-iqn}:${mac}
```

`${quote}` / `${quote-sig}` are iPXE variables written by the firmware (hyphenated names); the request parameters are `quote` / `quote_sig`. On success, the returned `disk-nqn` / `storager-ip` may override the static defaults; derived variables are re-built at `:vars-done`. `base-iqn` is used to derive `initiator-iqn`; the default `iscsi-sep` does not participate in menu assembly.

In `menu.ipxe`, the main boot entry assembles the NVMe-oF root-path (`set root-path nvme://${storager-ip}:${nbft-tls-port}/${disk-nqn}`, with `?secret=` appended when `nbft-secret` is injected and `&tls_psk=…&tls_trsvcid=…` appended from the injected TLS variables; `nbft-tls-port` / `nbft-tls-psk` are required — their absence means the target is not encryption-ready, and the entry drops to `failed` instead of assembling a plaintext URI).

### Agent Data-Plane Address

`/boot-vars` returns the **data-plane address** that Workers use to connect to storage (used by NVMe-oF boot), not the Agent’s HTTP API address. It is recommended to explicitly configure it in `config/agents.yml`:

```yaml
agents:
  a1b2c3d4e5f6:
    base_url: http://host.docker.internal:4840
    storager_ip: 192.168.80.3
```

If `storager_ip` is not configured, the Control Plane falls back to the host portion of `base_url`. However, when `base_url` is `host.docker.internal`, this value is not suitable for physical Workers.

### Registration Window & Attestation Enforcement (/settings/registration-window, /settings/enforcement)

#### Description

The registration window is the controlled enrollment channel for the deployment phase: only while the window is open can new devices report with their TPM AK (`GET /devices/report`) to auto-join the pool and complete AK claim; once it closes, reports from unknown devices are neither pooled nor written to the ledger, leaving only a rejection audit record (no enrollment channel outside the window). The TTL and quota caps come from the `spec.registration` declaration (`windowMaxTtlMinutes` defaults to 60, `windowMaxDevices` to 1000; a permanent window cannot be configured), and the window auto-closes on expiry (lazy computation; query for the actual state). An **admission quota** (`max_devices`, in devices) may be set as well: once that many devices have auto-joined within the window, admission stops (the window record is not cleared). The quota constrains only the automatic admission of unknown devices — AK claims and manual registration/import by the admin are unaffected; the admitted count is derived from the device ledger, with no stored counter. All endpoints require `Authorization: Bearer`.

The attestation enforcement switch is on by default: `/boot-vars` applies the 4th injection condition to bound devices — missing `mac` → rejected as `missing_mac`; no AK in the ledger → rejected as `ak_unregistered`; missing `nonce`/`quote`/`quote_sig` → rejected as `quote_invalid`; malformed quote or invalid signature → rejected as `quote_invalid`; nonce replay or extraData mismatch → rejected as `nonce_invalid`; PCR digest differing from the baseline → rejected as `pcr_mismatch`. Turning it off is for transition/troubleshooting only: devices without an AK may then receive menu variables, but **credentials are never injected** (credentials require attestation), and invalid proofs are never served in either mode.

#### GET /settings/registration-window

Queries the registration window status (no record / already expired → `open=false`). The time dimension and the quota dimension are projected side by side:

| Field | Type | Description |
|---|---|---|
| `open` | bool | Whether the window is open (time dimension; an exhausted quota does not change it) |
| `opened_at` | str | ISO8601 opening time (`null` when closed) |
| `ttl_minutes` | int | TTL in minutes configured when opened (`null` when closed) |
| `closes_at` | str | ISO8601 expected closing time (`null` when closed) |
| `remaining_seconds` | int | Remaining seconds (`0` when closed) |
| `max_devices` | int \| null | Admission quota (devices); `null` = unlimited |
| `admitted` | int | Devices admitted within this window (derived, see below) |
| `remaining_devices` | int \| null | Quota left; `null` = unlimited |
| `quota_exhausted` | bool | Whether the quota is used up (no further devices join the pool; the window record is not cleared) |
| `limits` | object | Declared caps: `ttl_minutes_min` / `ttl_minutes_max` / `max_devices` (the WebUI constrains inputs by it) |

`admitted` is a **derived** value with no stored counter: it counts device-ledger entries with `source == "ipxe"` and `first_seen >= opened_at`. Manual registration (`POST /devices`, `source=manual`) and manifest import therefore do not consume quota, and devices that existed before the window opened (including AK-only claims) are not counted.

```json
{"open": true, "opened_at": "2026-08-21T10:00:00+08:00", "ttl_minutes": 30, "closes_at": "2026-08-21T10:30:00+08:00", "remaining_seconds": 1799, "max_devices": 20, "admitted": 3, "remaining_devices": 17, "quota_exhausted": false, "limits": {"ttl_minutes_min": 1, "ttl_minutes_max": 60, "max_devices": 1000}}
```

#### POST /settings/registration-window

Opens the registration window (status 201). If already open → 409 (close it first); an expired leftover record can be re-opened directly (overwrite), and a reopened window derives its quota from the new opening time. Recorded as an operation (`settings.registration_window`).

**Request body**:

| Field | Required | Type | Description |
|---|---|---|---|
| `ttl_minutes` | Yes | int | 1 to `spec.registration.windowMaxTtlMinutes` (default 60; outside returns 400) |
| `max_devices` | No | int \| null | Admission quota (devices), 1 to `spec.registration.windowMaxDevices` (default 1000; outside returns 400); omitted/`null` = unlimited, still bounded by the TTL |

```bash
# Time-boxed only
curl -X POST "$BASE_URL/settings/registration-window" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ttl_minutes": 30}'

# Time-boxed + count-boxed: admit at most 20 new devices into the pool
curl -X POST "$BASE_URL/settings/registration-window" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ttl_minutes": 30, "max_devices": 20}'
```

**Response**: same as GET (window status after opening, including `limits`).

#### DELETE /settings/registration-window

Closes the registration window early (409 when never opened / no record). Recorded as an operation (`settings.registration_window`).

```bash
curl -X DELETE "$BASE_URL/settings/registration-window" \
  -H "Authorization: Bearer $TOKEN"
```

**Response**: `{"open": false}`

#### GET/PUT /settings/enforcement

Gets/toggles the TPM attestation enforcement switch (persisted to `state/settings.json`, survives restarts).

**Request body (PUT)**:

| Field | Required | Type | Description |
|---|---|---|---|
| `enabled` | Yes | bool | `true` = enforce (devices without an AK, or failing attestation, are denied boot) |

```bash
curl -X PUT "$BASE_URL/settings/enforcement" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

**Response**: `{"enabled": bool}` (same for GET and PUT). PUT is recorded as an operation (`settings.enforcement`).

### GET /devices/challenge

Challenge endpoint (**no auth**): issues a one-time nonce (short TTL, bound to MAC, replay protection; the nonce itself holds no secret) for the `/boot-vars` attestation chain. Device missing, without a registered AK, or revoked → **404** (cannot take the attestation path, and revoked devices get no nonce).

**Query parameters**:

| Parameter | Required | Description |
|---|---|---|
| `mac` | Yes | MAC address (normalization rules same as `/boot-vars`) |

**Success (200, `text/plain`)**: a `#!ipxe` script body — iPXE consumes it directly with `chain`, and `${nonce}` becomes the one-time 64-hex-char nonce afterwards:

```ipxe
#!ipxe
set nonce 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
```

**Errors**: `400` (invalid MAC); `404` (device missing / no registered AK); `429` (source IP above the per-minute threshold, carries `Retry-After`).

**Rate limit**: like `/devices/report`, limited per source IP in a sliding window (120 requests/minute by default; override with `KURRENT_CP_RATE_LIMIT_CHALLENGE`, `<=0` disables). Exceeding it emits a `boot_rate_limited` alarm.

**Challenge-attest chain (with `/boot-vars`, TPM attestation)**:

1. `GET /devices/challenge?mac=<mac>` → obtain `${nonce}` by executing the response script
2. The firmware quotes PCR 0/2/4/7 (SHA-256) with the TPM AK, placing the ASCII bytes of the `${nonce}` string (64 hex characters) in `extraData`, with no hex decoding or other encoding transform, and outputs `quote` (raw TPMS_ATTEST bytes as hex, without the TPM2B length prefix) and `quote_sig` (AK private-key signature over the quote bytes, hex). Reading the PCRs and producing the quote happen in one TPM call.
3. `GET /boot-vars?mac=&hostname=&nonce=&quote=&quote_sig=` → the quote structure is checked (magic `0xFF544347`, type `TPM_ST_ATTEST_QUOTE`, PCR selection fixed to 0/2/4/7 with SHA-256 only), then the AK signature, then `extraData == nonce` and nonce consumption, and finally the PCR digest against the platform baseline; credentials are served only when all pass (injection condition #4, see this section). Any failure → empty script (deny, audited as `boot_vars.credential` and `device.attest`, reasons see this section).

Quote validation runs before the nonce is consumed: a malformed quote or bad signature does not burn the nonce, so the device can correct and retry; replaying a consumed nonce returns `nonce_invalid`.
