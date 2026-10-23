# Control Plane API Reference

This document describes the HTTP endpoints currently implemented by the Control Plane, including request parameters, response structures, and directly executable `curl` commands for testing.

> **API-first design**: All Control Plane capabilities are exposed through the REST API as the primary interface. The Web administration interface (WebUI) itself is just one client of this API, equal to any third-party system or automation script. The guiding principle: **Everything faces the Control Plane**. Third-party integrations always call the Control Plane API and never bypass it to operate directly on an Agent or the data plane.

The Control Plane is a persistent HTTP service running on the Controller node, responsible for:

- Adding Workers
- Removing Workers
- Querying Worker inventory
- Querying Agent status
- Maintaining `dnsmasq/dhcp-hosts.conf`
- Calling Agents to create or delete iSCSI LUNs

It does **not** generate iPXE menus, serve static files, or directly operate `tgtadm`/`targetcli`.

---

## 1. General Information

### 1.1 Base URL

External entry point (the only one): `https://<host>/api/cp` (nginx 443; port 80 only redirects with 301).

The management plane listens on no TCP port at all: nginx forwards over a unix socket
(`/run/kurrent/cp.sock`) on a shared volume, and the container health check uses the same
socket. For local debugging, go through the 443 entry point or enter the control-plane
container to reach that socket.

> The entry point uses a TOFU self-signed certificate (`control_plane/state/certs/server.crt`,
> generated automatically at startup), so curl needs `-k`, or
> `--cacert control_plane/state/certs/server.crt` to pin that exact certificate.

Replace with the actual address if you expose it on a different port or domain.

### 1.2 Environment Variables

The control plane does **not** parse a `.env` file; it reads container environment
variables via `os.getenv(...)`. The root `docker-compose.yml` currently injects
only `TZ`; add any extra switch you need (for example the development-mode
`KURRENT_CP_INSECURE=1`, local only, never in production) to that service's
`environment:` block, or pass it with `docker compose --env-file`.

### 1.3 Authentication (authenticator chain)

External access goes through the nginx 443 entry point (the only external entry); the
control-plane management plane listens on a unix socket only and exposes no TCP port.
Except `GET /healthz` and the iPXE boot-chain endpoints (`/boot-vars`, `/devices/report`,
`/devices/challenge`), management endpoints require authentication. Three credential types
are tried in order (serial short-circuit: first success wins; an invalid credential returns
`401` without falling through):

| Credential | Carrier | Notes |
|---|---|---|
| mTLS client certificate | 443 mutual TLS | admin certificate issued during setup (CN=admin, no O); the CLI auto-discovers `control_plane/state/admin.conf` (a binary outside the repo can install a self-contained snapshot into `~/.kurrent` with `kurrent credentials install`; it is the last link in the discovery chain). nginx validates the chain (users CA + nodes CA bundle) and, only on `SUCCESS`, injects DN / issuer / serial / fingerprint / class; the control plane resolves groups from `state/users.yml` by CN. Identity headers are empty by default on every other proxied location |
| Management token | `Authorization: Bearer <token>` | issued by `kurrent token create --usage admin` (only a sha256 digest is stored); scopes are `admin` / `readonly` / `enroll`; TTL defaults to 24 hours, max 30 days; for scripts and WebUI sign-in |
| Session cookie | `__Host-kurrent_session` | obtained via `POST /session` with a certificate or a management token; the cookie carries only `{jti, exp, epoch}`, and identity/groups come from the server-side `state/sessions.yml` registry (12-hour TTL, sliding renewal only in the second half); deleting the registry entry invalidates the next request |

**Principal classes**: `human` (client certs issued by the users CA), `component` (agent /
nvmet-host certs issued by the nodes CA), `automation` (Bearer management token), and
`anonymous` (no credential). nginx derives human / component from the certificate issuer
and forwards `X-Client-Class`; each control-plane route declares its accepted classes and
**denies by default** (cross-class requests get `403`: component certs cannot reach the
management plane, human certs cannot call `/enroll/renew`).

Authorization (minimal RBAC): read requests (GET) only need to be **authenticated**; write
requests (POST/PUT/PATCH/DELETE) require the admin group, otherwise `403`. The only
exception is an automation token with the `enroll` scope: it may call `POST /pki/tokens`
to issue bootstrap tokens, and may not issue admin tokens. Reads are denied too (the scope
means “issue only”, so it cannot enumerate devices, workers, credentials, or sessions),
and the token cannot be exchanged for a browser session. Cookie-authenticated writes
also require `Origin` to match `Host`. Boot-chain endpoints (`/boot-vars`,
`/devices/report`, `/devices/challenge`) are `anonymous`; `POST /enroll` accepts
`anonymous` (Bearer bootstrap token) and `automation`; `POST /enroll/renew` accepts
`component` only.

**Audit**: every request appends one `state/audit.jsonl` event (`event` /
`principal_class` / `principal` / `issuer` / `serial` / `fingerprint_sha256` / `route` /
`decision` / `reason` / `request_id` / `client_ip`), and the response carries
`X-Request-Id`; secrets, token plaintext, private keys and PSKs are never recorded. If the
audit write fails, the request fails with `503` (fail-closed). Eight alarms are written as
`event=alarm` entries: boot credential `injected && !verified`, component/control on a
management route, unknown issuer / CRL hit (nginx rejects a CRL hit at the handshake
layer; the app-side alarm covers unknown-issuer evidence that reaches it), an
authentication-failure burst from one source, bootstrap identity/CSR CN mismatch, admin certificate issuance / CRL update / CA
generation, `users.yml` change, and audit write failure.

**Alarm webhook (optional)**: with `KURRENT_CP_ALARM_WEBHOOK=<url>` set, every
`event=alarm` audit entry is POSTed as JSON to that URL (same fields as the audit entry plus
`alarm` / `severity` / `detail`; no keys or plaintext). Delivery uses a background thread and
a bounded queue (200 entries), retries twice (1s / 4s apart, 5s timeout per attempt), and
drops with an ERROR log when the queue is full — it never sits in the request path and does
not affect the `503` fail-closed semantics. Nothing is delivered when the variable is unset.

Development mode: `KURRENT_CP_INSECURE=1` allows all requests (identity
insecure/admin); local development only, never in production — there is no
"no credential configured means open" path.

Example:

```bash
# On the control-plane host: the CLI auto-discovers admin.conf and uses 443 mTLS
kurrent agents list

# Scripts: management token (kurrent token create --usage admin)
export BASE_URL=https://<host>/api/cp
export TOKEN=<admin-token>
```

curl with authentication:

```bash
curl -s "$BASE_URL/workers" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 2. State and Configuration Files

The current Control Plane state and configuration are divided as follows (container paths; the host counterparts are `control_plane/state/` and `control_plane/config/`):

| File | Meaning |
|---|---|
| `config/agents.yml` | Agent node inventory and scheduling roles |
| `state/devices.yml` | Device ledger (pool / bindings / fingerprint / public key) — the **single authority for bindings** (`bound_worker_id`) |
| `state/workers.yml` | Worker ledger (compute identity / system disks / default boot) |
| `state/credentials.yml` | NVMe-oF key store (DHHC-1, key follows the Worker) |
| `state/credential-push.yml` | Credential push-state ledger (per-Agent delivery results, for reconciliation and projection, see 7.7) |
| `state/masters.yml` | Master tag ledger (os / os_version annotations, see 6.4) |
| `state/settings.json` | Runtime settings (registration window, enforcement switch) |
| `state/users.yml` | Management-plane user→group map (CN → groups; admin entry bootstrapped on first start, then groups resolve by cert CN) |
| `state/sessions.yml` | Server-side session registry (key = `sha256(jti)`; deleting an entry invalidates it, `epoch` invalidates all) |
| `state/audit.jsonl` | Authentication/authorization/credential audit and alarms (see 1.3; separate from `operations.jsonl`) |
| `state/pki/ca/{users,nodes,control}/` | Three separate root CAs (users=humans, nodes=node components, control=control-plane clients) |
| `state/pki/users/issued.yml` | users-domain certificate registry (by serial; includes revocation time and reason) |
| `state/pki/components/issued.yml` | nodes-domain component certificate registry (agent / nvmet-host, written at issuance; includes revocation time and reason) |
| `state/pki/nginx/ca-bundle.pem` | nginx trust bundle (users + nodes public certs only; no private keys) |
| `state/pki/nginx/clients.crl` | nginx client-certificate CRL (users + nodes tables concatenated) |
| `state/operations.jsonl` | Control Plane operation audit trail (with actor) |
| `dnsmasq/dhcp-hosts.conf` | `MAC -> hostname` bindings (**derived view**; the device ledger is authoritative) |

Notes:

- `workers.yml` does **not** store MAC addresses — MAC addresses and bindings live in the device ledger, and the Worker side only holds a projection.
- The Compose file mounts the whole `dnsmasq/` directory into the control-plane container (which also writes `dnsmasq.conf` there).
- `dhcp-hosts.conf` is written by **truncating in place** (preserving the inode) rather than via an atomic replace: the file is bind-mounted into the dnsmasq container at file level, so replacing the inode would leave the container reading the old file.
- `dnsmasq/dhcp-hosts.conf` contains one binding per line, with a fixed format:

```text
00:0c:29:b9:8b:2d,worker-01
```

---

## 3. Endpoint Overview

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Health check |
| `GET` | `/whoami` | Current request identity (authenticator chain result, see 1.3) |
| `POST` | `/session` | Exchange credentials for a session cookie (registry-backed sign-in) |
| `DELETE` | `/session` | Sign out (delete the session registry entry and clear the cookie) |
| `GET` | `/sessions` | List active sessions (admin, see 6.7) |
| `DELETE` | `/sessions/{session_id}` | Revoke one session (admin, see 6.7) |
| `DELETE` | `/sessions` | Sign out all: clear the registry and bump epoch (admin, see 6.7) |
| `GET` | `/boot-vars` | Dynamic iPXE boot variable injection, no auth required |
| `GET` | `/devices/report` | iPXE device info reporting (no auth, 11 fields, see 16.6) |
| `GET` | `/devices/challenge` | One-time nonce issuance (no auth, challenge-response, see 5.2) |
| `POST` | `/devices/rotate` | Device key rotation (no auth, signed by the old key, see 5.3) |
| `GET` | `/devices` | Device pool list (state filter, see 16.1) |
| `GET` | `/devices/{mac}` | Single device detail (see 16.2) |
| `POST` | `/devices` | Manually register a device into the pool (see 16.3) |
| `POST` | `/devices/import` | Bulk import device manifest (see 16.4) |
| `DELETE` | `/devices/{mac}` | Revoke a device (see 16.5) |
| `DELETE` | `/devices/revoked` | Purge all revoked devices (optional `dry_run` preview, see 16.5.1) |
| `GET` | `/settings/registration-window` | Get registration window status (see 5.1) |
| `POST` | `/settings/registration-window` | Open the registration window (TTL hard cap 1-60 min; optional admission quota `max_devices` 1-1000, see 5.1) |
| `DELETE` | `/settings/registration-window` | Close the registration window early (see 5.1) |
| `GET` | `/settings/enforcement` | Get the device signature enforcement switch (see 5.1) |
| `PUT` | `/settings/enforcement` | Toggle the device signature enforcement switch (see 5.1) |
| `GET` | `/agents` | List Agents and their capabilities |
| `POST` | `/agents` | Register a new Agent (writes agents.yml; 409 if id exists) |
| `POST` | `/agents/probe` | Probe an Agent and auto-derive registration parameters (preview, no file writes) |
| `PUT` | `/agents/{agent_id}` | Update an Agent’s configuration (id cannot be changed) |
| `POST` | `/pki/tokens` | Issue a node-bound bootstrap token (default single-use, 60 minutes) or a management token (scope/TTL, see 6.5) |
| `GET` | `/pki/tokens` | List the unexpired token ledger (see 6.5) |
| `DELETE` | `/pki/tokens/{token_id}` | Revoke a token (deletion takes effect immediately, see 6.5) |
| `POST` | `/pki/users/{cn}` | Issue a certificate for a registered user (private key only in this response, see 6.7) |
| `GET` | `/pki/users` | List certificates issued by the users CA (revoked/expired included, see 6.7) |
| `DELETE` | `/pki/users/{cn}` | Revoke certificates (optional `serial` for a single one): re-sign CRL + reload nginx + kick sessions (see 6.7) |
| `GET` | `/pki/components` | List nodes-domain component certificates (revoked/expired included, see 6.7) |
| `DELETE` | `/pki/components/{agent_id}` | Revoke component certificates (optional `serial`): write the nodes CRL + reload nginx + delete bootstrap tokens (see 6.7) |
| `DELETE` | `/agents/{agent_id}` | Remove an Agent’s registry entry (incl. master-tag cleanup, see 6.6) |
| `GET` | `/agents/{agent_id}/luns` | List iSCSI targets/LUNs on a given Agent |
| `GET` | `/masters` | Aggregate master image inventory from all storage nodes (merged with registered tags, for clone selection) |
| `PUT` | `/agents/{agent_id}/masters/{master_name}/tag` | Register a master tag (os / os_version annotations, see 6.4) |
| `DELETE` | `/agents/{agent_id}/masters/{master_name}/tag` | Clear a master tag (see 6.4) |
| `POST` | `/agents/{agent_id}/luns/disk` | Create a disk LUN on a given Agent (master clone / empty disk) |
| `POST` | `/agents/{agent_id}/luns/cd` | Create a CD (ISO virtual drive) LUN on a given Agent |
| `DELETE` | `/agents/{agent_id}/luns` | Delete a LUN/target on a given Agent |
| `POST` | `/agents/{agent_id}/luns/scan` | Trigger an Agent to scan its image directory and rebuild targets |
| `POST` | `/workers` | Register a Worker identity (hostname binding; `mac` optional — providing it binds the device directly) |
| `POST` | `/workers/batch` | Bulk-create Workers (count + naming rule, per-item independent, optional `macs` direct bind, see 7.6) |
| `POST` | `/workers/{worker_id}/luns/disk` | Create a system disk LUN for a given Worker |
| `POST` | `/workers/luns/disk/batch` | Batch create system disks for multiple Workers (per-item node naming, or `workers` placed by the Control Plane; `dry_run` returns a plan) |
| `DELETE` | `/workers/{worker_id}/luns/disk/{os_tag}` | Delete a single system disk of a Worker (with option to keep/delete .img file) |
| `PUT` | `/workers/{worker_id}/default-disk` | Set the Worker’s default boot configuration (default disk os_tag / menu item / timeout) |
| `GET` | `/workers` | List Workers |
| `GET` | `/workers/{worker_id}` | Query a single Worker |
| `GET` | `/workers/{worker_id}/status` | Query Worker inventory and real-time status |
| `DELETE` | `/workers/{worker_id}` | Delete a Worker |
| `POST` | `/workers/delete/batch` | Batch delete Workers (each item processed independently, with success/failure summary) |
| `PUT` | `/workers/{worker_id}/credential` | Set/rotate the NVMe-oF authentication key (DHHC-1 validation; nvmet disk creation already issues one, see 7.7) |
| `DELETE` | `/workers/{worker_id}/credential` | Delete the NVMe-oF authentication key (revoke authentication, see 7.7) |
| `GET` | `/workers/{worker_id}/credential` | Query credential metadata (never returns plaintext, see 7.7) |
| `GET` | `/operations` | Read operation audit log |

---

## 4. GET /healthz

### Description

Health check endpoint. Does not change any state and requires no authentication.

### curl

```bash
curl -s "$BASE_URL/healthz"
```

### Successful Response

```json
{"status":"ok"}
```

---

## 5. GET /boot-vars

### Description

Provides per-worker boot variables for iPXE boot scripts. This endpoint does not require authentication and only exposes variables needed for booting within a controlled network. NVMe-oF credentials (`nbft_secret` / `nbft_tls_psk`) are **injected only after the device identity is verified by signature** (`mac` + one-time `nonce` + a valid signature, see 5.1).

> **Note**: This endpoint is **read-only** (a projection of boot variables, no write side effects). Registration-window enrollment (new MACs entering the device pool with a public key) has been consolidated into `GET /devices/report` (see 16.6), which `boot.ipxe.cfg` chains **before** `/boot-vars`.

**Rate limit**: sliding window per source IP (240 requests/minute by default; override with `KURRENT_CP_RATE_LIMIT_BOOT_VARS`, `<=0` disables). Over the limit returns `429` with `Retry-After` and emits a `boot_rate_limited` alarm. Signature verification is the most expensive work here; the limit only suppresses scanning and is not an identity boundary.

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
| `disk_nqn` | The full NQN of the default boot disk (same disk selection rule; disk record `nqn` — the data-plane unique identifier and the authoritative value from the control plane) — the firmware splices it verbatim into `sanboot nvme://<ip>:<data-port>/${disk-nqn}` and no longer reassembles it from hostname/os/os-tag. Not returned when the disk record lacks `nqn` or when the Worker has no system disk. |
| `base_iqn` | The IQN of the Worker’s default boot disk (the disk for `default_disk`; if not set, the first disk) with the last colon-and-following part stripped — the disk NQN is the authoritative identifier, the IQN is derived from it. **Not returned** if the Worker has no system disk (iPXE falls back to the static default in `boot.ipxe.cfg`). |
| `storager_ip` | The `storager_ip` of the Agent that hosts the default boot disk (same disk selection rule as above) — the data-plane address shared by NVMe-oF boot and the iSCSI installers. Not returned when there is no system disk. |
| `iscsi_sep` | The iSCSI root **separator** (the part between `${storager-ip}` and `${base-iqn}`, consumed by the installer iSCSI assembly). The root-path assembly is done on the iPXE side. **Returned for stgt / LIO backends only**: `:::1:` for stgt backends (lun placeholder 1), `::::` for LIO backends (empty placeholder). The backend type is resolved by **querying the Agent’s `/capabilities` `backend` field in real time** (the node reports its own capability — capability is truth); if the agent is unregistered / the query fails / the value is invalid, stgt format is assumed. It is **not derived from the Agent’s `tags`** in `agents.yml` (tags are human annotations only — otherwise a drifting tag would assemble a wrong separator). **Not sent for nvmet backends** (the menu installer items are guarded by `isset ${iscsi-sep}` and skipped). Not returned when there is no system disk. |
| `nbft_secret` | The Worker’s NVMe-oF authentication key (DHHC-1, `state/credentials.yml` indexed by worker_id, see 7.7). **Injected only when the Worker is bound, has a key entry, and the device identity is verified by signature**; not returned when there is no key / the Worker is unbound / the request is unverified / the request is denied. Consumed by the firmware as `nvme://...?secret=${nbft-secret}` (on nvmet backends a missing key withholds the whole `/boot-vars` payload and the menu refuses to boot) |
| `nbft_tls_psk` | Pre-shared key for NVMe/TCP TLS: a **pure function of the authentication key** (`HMAC-SHA256(raw_key, b"kurrent-nvme-tls-psk-v1")` → 64 hex, see 7.7; no separate storage field — rotating or revoking the key rotates or revokes the PSK atomically). Returned **only when all four hold: the Worker is bound with a key, the device identity is verified by signature, the backend is nvmet, and the Agent reports a ready data-plane port**; otherwise the whole boot-variable payload is withheld (boot refused, audited) — encryption is the default and there is no plaintext fallback. The firmware uses it for its own handshake on that port and hands it to the initramfs via the NBCT table, which reconnects with `--tls` on the same port |
| `nbft_tls_port` | Data-plane port (shared by the firmware and kernel boot segments; read live from the Agent’s `/capabilities.port.trsvcid` — capability is truth, same live-query rule as `iscsi_sep`/`backend`); returned under the same conditions as `nbft_tls_psk`. The menu appends `&tls_psk=${nbft-tls-psk}&tls_trsvcid=${nbft-tls-port}` to the sanboot URI |
| `hostnqn` | The Worker’s initiator Host NQN (disk NQN prefix + `:host.<worker_id>`, in the same namespace as the disk NQN and matching the host entry registered in nvmet-host); **injected when the default disk carries an NQN**. Consumed by the firmware: iPXE nvmetcp defaults hostnqn to `nqn.2014-08.org.ipxe:<uuid>` (falling back to `:ipxe` without a UUID); a mismatch against the registered value fails strict-mode authentication, so it must be overridden with this field (firmware patch `0005` supports the `hostnqn` setting) |
| `os` | The OS name of the Worker’s default boot disk (the disk for `default_disk`; if not set, the first disk; the disk record is authoritative) — 2026-08-30 MAIN MENU dynamicization: the menu keeps a single generic OS item (`boot-os`), and this variable is display-only for the menu label; the data-plane path comes from `disk_nqn`. Not returned when the Worker has no system disk (reboot loop waits for disk creation). |
| `os_version` | Version annotation of the default boot disk (disk record `os_version`; `''` = no version) — omitted when empty; the `os-label` assembly in menu.ipxe adapts (shows `os version` when present). Not returned when the Worker has no system disk. |
| `menu_default` | Derived chain: `workers.yml` `default_disk` (set separately after disk creation; os_tag references a specific disk) > `boot.menu_default` (explicit configuration) > `reboot` (loop reboot waiting when not configured). 2026-08-30 dynamicization: since the menu OS item has converged to the single generic item `boot-os`, any OS-semantics default (`default_disk`, or legacy OS names in `boot.menu_default`) is normalized to `boot-os`; non-OS navigation values (`menu-diag` / `menu-install` / `config` / `shell` / `reboot` / `exit`) are returned as-is |
| `menu_timeout` | When a default boot is configured: `boot.menu_timeout` > `spec.boot.menuTimeoutMs` (default 5000). When in `reboot` loop: always uses `spec.boot.autoBootTimeoutSec` (default 1). Units are milliseconds. |

Worker lookup rules (**binding authority first**):

```text
mac -> devices.yml (device inventory) -> bound_worker_id -> workers.yml
mac missing or not found -> hostname -> workers.yml (by hostname or worker_id)
mac not found and hostname not found -> dnsmasq projection (legacy / no-inventory fallback)
not identified and mac provided ->
  - device in pool (pooled) -> reboot loop (menu-default=reboot + short timeout), waiting for binding
  - device revoked -> empty script (menu stays)
  - unknown MAC + registration window open + quota left + a valid public key -> enter the device pool (see “Registration-Window Enrollment” below), return reboot loop
  - unknown MAC + window closed / quota used up / no public key -> empty script (menu stays; report only records the fingerprint, no pool entry)
```

### Default Boot Item Rules

The default boot item is derived by `/boot-vars` in the following order:

```text
default_disk (set separately after disk creation, see 7.3) -> boot.menu_default (explicit config) -> reboot (not configured)
```

- Recommended approach: after creating a system disk, call `PUT /workers/{worker_id}/default-disk` to set the default boot disk (os_tag references a specific disk):
  ```text
  disk=<os_tag> -> menu_default=boot-os, os=<disk os>, os-version=<disk os_version if any>, disk_nqn=<disk nqn>
  ```
  Since 2026-08-30, the MAIN MENU keeps a single generic OS item: `menu_default` is normalized to `boot-os` (legacy OS names in `boot.menu_default` are also normalized), while `os` / `os-version` are display-only and `disk_nqn` carries the boot identity consumed by the generic item.
- Alternatively, you can leave `default_disk` unset and use `boot.menu_default` to specify a default item on the iPXE menu (e.g., `menu-install` during installation, or `exit`).
- When neither is configured, `menu_default` returns `reboot` (a short-timeout reboot loop waiting for the admin to create a disk / set a default disk; `exit` is only used when explicitly configured).

### Registration-Window Enrollment (Zero-touch Provisioning)

When a new device boots without an identity, iPXE first `chain`s `/devices/report` (11-field report plus optional `pubkey`, see 16.6) and then requests `/boot-vars`. **Enrollment only happens during the registration window** (2026-08-21 ruling): only when the window is open, the admission quota is not used up, and the report carries a valid ECDSA P-256 public key does an unknown MAC enter the device pool; once the window closes, reports only update the fingerprint without pool entry (there is no enrollment channel outside the window, and a permanent window cannot be configured at the code level).

1. `GET /devices/report`: unknown MAC + window open + quota left + `pubkey` present → written to `state/devices.yml` (`state=pooled`, fingerprint stored, `key_hash` filled, `source=ipxe`)
2. `GET /boot-vars`: MAC in the pool and unbound → returns `menu-default=reboot` + short timeout, rebooting in a loop until the admin binds it
3. After the admin binds the device to a Worker (WebUI / API; single bind 16.7, batch bind preview/execution 16.9/16.10), the next boot follows the Worker configuration normally

Key claim for existing devices: an already-pooled device reporting with a public key during the window gets its `key_hash` filled in (claim complete); a key mismatch is rejected (revoke/re-register by deleting the inventory entry). A claim depends only on the window being open and is not constrained by the admission quota. After all devices have claimed their keys, enable signature enforcement (see 5.1).

Controls:

| Item | Default | Description |
|---|---:|---|
| Registration window | Closed | Open it with `POST /settings/registration-window` during deployment (TTL hard cap 1-60 min, auto-closes on expiry), see 5.1 |
| Admission quota | Unlimited | Optional `max_devices` when opening (1-1000 devices); once that many devices have auto-joined within the window, admission stops, see 5.1 |
| Signature enforcement | Off | `GET/PUT /settings/enforcement`: when on, `/boot-vars` is only served to bound devices that pass signature verification, see 5.1 |
| `spec.boot.autoBootTimeoutSec` | `1` | Menu timeout in milliseconds during the reboot loop. |

The entire enrollment process is logged as operations (`device.register` / `device.claim`). On failure, the inventory is rolled back and an empty script is returned, so the next request will retry without affecting the iPXE boot process.

### Anti-Impersonation (Binding as Authentication)

When a request carries a `mac`, the Control Plane verifies the Worker that the device is **bound to** (`bound_worker_id`, the binding authority); if it does not match (device bound to another Worker / unbound / unknown), the request is **denied** — an empty script is returned and the boot vars are not leaked. Requests without a `mac` (hostname only) cannot be verified: with signature enforcement on they are **rejected** (`missing_mac`); during the transition period only menu/navigation variables are served and credentials are never injected. This makes the device↔Worker binding and the device signature the boot-time authentication boundary: only the bound device, after signature verification, can receive the Worker's boot configuration and data-plane credentials (e.g., `disk_nqn` / `nbft_secret` / `nbft_tls_psk`).

### Query Parameters

| Parameter | Required | Default | Description |
|---|---:|---|---|
| `mac` | No | None | MAC address. The backend normalizes it by stripping `:` / `-` / `.`. Both colon-separated (`00:0c:29:b9:8b:2d`) and `mac:hexraw` (`000c29b98b2d`) formats are supported. |
| `hostname` | No | None | Hostname, e.g., `worker-01`. |
| `format` | No | `ipxe` | `ipxe` or `json`. |
| `nonce` | No | None | One-time challenge-response nonce (issued by `/devices/challenge`, see 5.2); when enforcement is on, missing → `missing_sig` rejected |
| `sig` | No | None | ECDSA P-256 signature as base64(DER) over `nonce‖mac` (no separator, see 5.2); when enforcement is on, missing → `missing_sig` rejected. **Compatibility restore**: iPXE builds URLs without percent-encoding, so the `+` of base64 enters the query verbatim and is decoded as a space under form-urlencoded rules; the server restores spaces to `+` before verifying |

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
set nbft-secret DHHC-1:01:<base64>   # only when NVMe-oF authentication is enabled (see 7.7)
set nbft-tls-psk <64 hex>            # derived from the authentication key
set nbft-tls-port 4421
set hostnqn nqn.2026-07.com.kurrent:host.worker-01
set os ubuntu
set menu-default boot-os
set menu-timeout 5000
```

Registered but no default boot configured (no system disk / no `default_disk` / no explicit `boot.menu_default`):

```ipxe
#!ipxe
# boot vars for worker-01
set menu-default reboot
set menu-timeout 1
```

New MAC (window enrollment) or completely unrecognized, and if the window is closed or no public key is carried, returns an empty script:

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
  "menu_default": "reboot",
  "menu_timeout": 1
}
```

Unrecognized and not enrolled (window closed / no public key):

```json
{}
```

### iPXE Integration

In `tftp/boot.ipxe.cfg`, the trust chain fetches this endpoint over HTTPS (nginx 443, TOFU anchor) after the challenge-respond handshake (see 5.2):

```ipxe
# Stage 3: challenge-respond (R7) — signed request carries nonce+sig
:challenge
chain --autofree ${https-url}devices/challenge?mac=${mac} && goto signed || goto bootvars

:signed
sign ${nonce}${mac} || goto bootvars
chain --autofree ${https-url}boot-vars?mac=${mac}&hostname=${hostname}&nonce=${nonce}&sig=${sig} || goto vars-done
goto vars-done

# Degraded path (challenge failed: not registered/claimed, or legacy firmware):
# served while signature enforcement is off — missing_sig is denied once enforcement is on
:bootvars
chain --autofree ${https-url}boot-vars?mac=${mac}&hostname=${hostname} || goto vars-done

:vars-done
isset ${iscsi-sep} || set iscsi-sep :::1:
isset ${hostname} && set initiator-iqn ${base-iqn}:${hostname} || set initiator-iqn ${base-iqn}:${mac}
```

On success, the returned `disk-nqn` / `storager-ip` may override the static defaults; derived variables are re-built at `:vars-done`. The `isset` guard keeps the backend-specific separator (`stgt` `:::1:` / LIO `::::`) projected by `/boot-vars`.

In `menu.ipxe`, the main boot entry assembles the NVMe-oF root-path (`set root-path nvme://${storager-ip}:${nbft-tls-port}/${disk-nqn}`, with `?secret=` appended when `nbft-secret` is injected and `&tls_psk=…&tls_trsvcid=…` appended from the injected TLS variables; `nbft-tls-port` / `nbft-tls-psk` are required — their absence means the target is not encryption-ready, and the entry drops to `failed` instead of assembling a plaintext URI); the installer entries keep the iSCSI assembly (`set root-path iscsi:${storager-ip}${iscsi-sep}${base-iqn}:${hostname}.<os>`). The protocol prefixes and assembly structure remain static; only the separator is projected from the backend.

### Agent Data-Plane Address

`/boot-vars` returns the **data-plane address** that Workers use to connect to storage (shared by NVMe-oF boot and the iSCSI installers), not the Agent’s HTTP API address. It is recommended to explicitly configure it in `config/agents.yml`:

```yaml
agents:
  storage-lio-01:
    base_url: http://host.docker.internal:4840
    storager_ip: 192.168.80.3
```

If `storager_ip` is not configured, the Control Plane falls back to the host portion of `base_url`. However, when `base_url` is `host.docker.internal`, this value is not suitable for physical Workers.

### 5.1 Registration Window & Enforcement (/settings/registration-window, /settings/enforcement)

#### Description

The registration window is the controlled enrollment channel for the deployment phase (2026-08-21 ruling; it replaces the former `auto-register` permanent switch, whose endpoint has been removed): only while the window is open can new devices report with their public key (`GET /devices/report`) to auto-join the pool and complete key claim; once it closes, reports only record fingerprints without pool entry (no enrollment channel outside the window). The TTL has a hard cap of 60 minutes (a permanent window cannot be configured at the code level) and auto-closes on expiry (lazy computation; query for the actual state). An **admission quota** (`max_devices`, in devices) may be set as well: once that many devices have auto-joined within the window, admission stops (the window record is not cleared). The quota constrains only the automatic admission of unknown devices — key claims and manual registration/import by the admin are unaffected; the admitted count is derived from the device ledger, with no stored counter. All endpoints require `Authorization: Bearer`.

The signature enforcement switch (transition-period compatibility): when off, keyless devices may still receive menu variables but **credentials are never injected** (credentials require a verified signature); when on, `/boot-vars` applies the 4th injection condition to bound devices — missing `mac` → rejected as `missing_mac`; no `key_hash` → rejected as `no_key`; missing `nonce`/`sig` → rejected as `missing_sig`; replay or invalid signature → rejected. Claimed devices carrying an invalid signature are rejected during the transition period as well (forged signatures are never served). It is recommended to turn enforcement on only after all existing devices have completed key claim.

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

`admitted` is a **derived** value with no stored counter: it counts device-ledger entries with `source == "ipxe"` and `first_seen >= opened_at`. Manual registration (`POST /devices`, `source=manual`) and manifest import therefore do not consume quota, and devices that existed before the window opened (including key-only claims) are not counted.

```json
{"open": true, "opened_at": "2026-08-21T10:00:00+08:00", "ttl_minutes": 30, "closes_at": "2026-08-21T10:30:00+08:00", "remaining_seconds": 1799, "max_devices": 20, "admitted": 3, "remaining_devices": 17, "quota_exhausted": false}
```

#### POST /settings/registration-window

Opens the registration window (status 201). If already open → 409 (close it first); an expired leftover record can be re-opened directly (overwrite), and a reopened window derives its quota from the new opening time. Recorded as an operation (`settings.registration_window`).

**Request body**:

| Field | Required | Type | Description |
|---|---|---|---|
| `ttl_minutes` | Yes | int | 1-60 (returns 400 outside the range) |
| `max_devices` | No | int \| null | Admission quota (devices), 1-1000 (returns 400 outside the range); omitted/`null` = unlimited, still bounded by the TTL |

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

**Response**: same as GET (window status after opening).

#### DELETE /settings/registration-window

Closes the registration window early (409 when never opened / no record). Recorded as an operation (`settings.registration_window`).

```bash
curl -X DELETE "$BASE_URL/settings/registration-window" \
  -H "Authorization: Bearer $TOKEN"
```

**Response**: `{"open": false}`

#### GET/PUT /settings/enforcement

Gets/toggles the device signature enforcement switch (persisted to `state/settings.json`, survives restarts).

**Request body (PUT)**:

| Field | Required | Type | Description |
|---|---|---|---|
| `enabled` | Yes | bool | `true` = enforce (keyless / signature-less devices are denied boot) |

```bash
curl -X PUT "$BASE_URL/settings/enforcement" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

**Response**: `{"enabled": bool}` (same for GET and PUT). PUT is recorded as an operation (`settings.enforcement`).

### 5.2 GET /devices/challenge

Challenge endpoint (**no auth**): issues a one-time nonce (short TTL, bound to MAC, replay protection; the nonce itself holds no secret) for the `/boot-vars` signature verification chain. Device missing, unclaimed (no `key_hash`), or revoked → **404** (cannot take the signature path, and revoked devices get no nonce).

**Query parameters**:

| Parameter | Required | Description |
|---|---|---|
| `mac` | Yes | MAC address (normalization rules same as `/boot-vars`) |

**Success (200, `text/plain`)**: a `#!ipxe` script body — iPXE consumes it directly with `chain`, and `${nonce}` becomes the one-time 64-hex-char nonce afterwards:

```ipxe
#!ipxe
set nonce 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
```

**Errors**: `400` (invalid MAC); `404` (device missing/unclaimed); `429` (source IP above the per-minute threshold, carries `Retry-After`).

**Rate limit**: like `/devices/report`, limited per source IP in a sliding window (120 requests/minute by default; override with `KURRENT_CP_RATE_LIMIT_CHALLENGE`, `<=0` disables). Exceeding it emits a `boot_rate_limited` alarm.

**Challenge-response chain (with `/boot-vars`, device identity verification)**:

1. `GET /devices/challenge?mac=<mac>` → obtain `${nonce}` by executing the response script
2. iPXE signs `nonce‖mac` (UTF-8, no separator; MAC in lowercase colon form) with the device key (ECDSA P-256) → `sig` (base64(DER)); hostname does not participate in the signature (identity is guaranteed by MAC → device-ledger binding)
3. `GET /boot-vars?mac=&hostname=&nonce=&sig=` → credentials are served only after verification passes (injection condition #4, see 5.1); verification failure → empty script (deny, audited as `boot_vars.credential`, reasons see 5.1)

### 5.3 POST /devices/rotate

Device key rotation (**no auth**; proves itself with a signature from the old key; does not reopen the registration window and issues no certificate). The device must be claimed (has `key_hash`) and not revoked.

1. `GET /devices/challenge?mac=` to obtain a one-time nonce
2. Sign `rotate‖mac‖new_pubkey‖nonce` with the **old private key** (UTF-8, no separator; `new_pubkey` is a 130-hex uncompressed point, `mac` is lowercase colon form)
3. `POST /devices/rotate?mac=&pubkey=&nonce=&sig=` (`sig` = base64(DER))

On success the ledger's `key_hash` becomes the new public key and `key_rotated_at` is recorded; the old key **fails immediately** on the `/boot-vars` verification path. The nonce is one-time (replay `401`); revoked device `403`; unclaimed (no `key_hash`) `409`; unknown device `404`; invalid `pubkey` `400`. Rate limit: 10/minute per source IP (override with `KURRENT_CP_RATE_LIMIT_ROTATE`).

```bash
# sig must be produced by the device's old private key: ECDSA-P256(rotate + mac + new_pubkey + nonce)
curl -s -X POST "https://<host>/devices/rotate?mac=000c29b98b2d&pubkey=<130-hex>&nonce=<64-hex>&sig=<base64-DER>"
```

```json
{ "mac": "00:0c:29:b9:8b:2d", "rotated": true }
```

---

## 6. GET /agents

### Description

Lists Agents configured in `config/agents.yml`. By default, it also probes each Agent’s `/healthz` and `/capabilities` in real time.

`health` has three states: `ok` (healthy); `degraded` (the agent is up but the data plane is not ready — for the nvmet backend this means the host service is unreachable or configfs is not ready; the reason is in `error`); `error` (agent unreachable or capability probing failed). With `live=false` no probing is done and `health` is absent.

Identity layering: `id` is the machine identity (normally auto-generated by `kurrent join` as a random short id — 12 hex chars, same form as the disk `os_tag`, auto-registered at enroll; manual registration is 6.1, and the readable ids in examples are illustrative only); `remark` is a human label (from the node declaration `metadata.remark`, shown on the WebUI card and editable in the edit form or via 6.3).

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
    "id": "storage-lio-01",
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
      "backend": "lio",
      "fs_type": "btrfs",
      "cd": false,
      "persistent": "saveconfig (auto-load on start)",
      "base_nqn": "nqn.2026-07.com.controller",
      "clone": "reflink (FICLONE) -> shutil.copy fallback",
      "empty_disk": "truncate (sparse)"
    }
  }
]
```

---

## 6.1 POST /agents

### Description

Registers a new Agent: writes to `config/agents.yml` and takes effect immediately (the Agent will be included in disk creation / mount scheduling). Duplicate `id` registration returns `409`.

**Recommended flow**: first fill in the API address in the WebUI (or use `POST /agents/probe`, see 6.2) and probe to automatically obtain role, tags, data-plane address, etc. After confirming, call this endpoint to complete registration. Direct full-parameter submission is also supported.

### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `id` | Yes | Agent identifier. Automatically lowercased. Same character rules as worker id (letters, digits, dots, underscores, hyphens). |
| `base_url` | Yes | Agent Control Plane API address. Must start with `http://` or `https://`. Trailing `/` is removed automatically. |
| `storager_ip` | No | Data-plane address (business network IP, protocol-neutral). Falls back to the hostname of `base_url` if omitted. |
| `role` | No | Roles: `disk` = can create system disks (storage node), `cd` = can mount ISOs (optical drive node). Default `{disk: false, cd: false}`. |
| `tags` | No | Free-form tag array (e.g., `storage` / `rack-a3`), for **human annotation / display only**. It does not participate in `/boot-vars` separator derivation (the backend type is resolved by `/boot-vars` querying the Agent’s `/capabilities` in real time, see 5.1). |
| `enabled` | No | Whether the Agent is enabled. `false` = **drain**: no new disks accepted (always 409), existing disks keep serving and booted devices are unaffected. Default `true`. |
| `remark` | No | Human label (display-only), empty string by default. Normally no need to set it by hand — it is auto-registered from the node declaration `metadata.remark` at enroll (see 6.5.1). |

### curl

```bash
curl -s -X POST "$BASE_URL/agents" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "storage-stgt-02",
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
  "id": "storage-stgt-02",
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

## 6.2 POST /agents/probe

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
| `agent_id` | No | Agent identifier in edit scenarios (reserved; the probe logic does not currently depend on it). |

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
  "backend": "stgt",
  "fs_type": "btrfs",
  "base_iqn": "iqn.2026-07.com.controller",
  "clone": "reflink (FICLONE) -> shutil.copy fallback",
  "empty_disk": "truncate (sparse)",
  "persistent": "auto-scan on startup"
}
```

### Error Responses

| Status Code | Scenario |
|---|---|
| `400` | `base_url` does not start with http(s) |
| `502` | Agent unreachable (`/healthz` failed) or `/capabilities` call failed (e.g., mTLS client certificate not issued by the Control Plane CA) |

---

## 6.3 PUT /agents/{agent_id}

### Description

Updates an existing Agent: overwrites the corresponding entry in `config/agents.yml` and takes effect immediately (disk creation / mount scheduling uses the new configuration). The `id` cannot be changed (taken from the path parameter).

Use cases: disable/enable a node (drain), data-plane address migration, remark and role adjustments. **For auto-registered nodes (tags containing `auto`), `base_url` is managed by the node declaration** (declaration authority, see 6.5.1): submitting the current value succeeds (the regular shape of a WebUI edit save); submitting a different value returns `409` — after a node move, edit `spec.agent.advertiseUrl` in the node’s `storager/kurrent.yaml` and re-run `kurrent join`; renewal syncs it automatically. Manually registered nodes can still change the address from the control plane.

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
curl -s -X PUT "$BASE_URL/agents/storage-stgt-02" \
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
  "id": "storage-stgt-02",
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

> **Probing during edit**: In edit scenarios, it is recommended to first call `POST /agents/probe` (6.2) to verify the address is reachable before saving. The probe authenticates with the Control Plane’s mTLS client certificate (identity bound to the component CA) — no token is required. For auto-registered nodes the address is read-only and the probe runs against the current value.

---

## 6.4 Master Tags (/agents/{agent_id}/masters/{master_name}/tag)

### Overview

Master tags are control-plane annotations (`state/masters.yml`) — `os` / `os_version` are **annotation-only** (for human understanding, no whitelist validation):

- Since 2026-08-30 there is no valid OS set anymore (OS_ITEMS retired): disk creation `os` is a free string, and multiple versions of the same OS are distinguished by the disk-level `os_tag` (a 12-hex random identifier, the data-plane unique key). Master tags only associate a master name with an OS/version.
- **Tagging does not verify that the master exists** (an Agent may be offline; the ledger is authoritative); `/masters` attaches `os` / `os_version` to tagged entries only.
- When creating a disk from a master, the WebUI auto-fills the tag (annotation, editable); clearing a tag only affects future disk creation.

### PUT (register / update)

| Field | Required | Description |
|---|---:|---|
| `os` | Yes | OS annotation (free string, lowercased; for human understanding, not a data-plane identifier) |
| `os_version` | No | Version annotation, defaults to `''` (no version) |

#### curl

```bash
curl -s -X PUT "$BASE_URL/agents/storage-lio-01/masters/_tpl_ubuntu_2204.img/tag" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "os": "ubuntu",
    "os_version": "22.04"
  }'
```

#### Success response

```json
{ "agent": "storage-lio-01", "name": "_tpl_ubuntu_2204.img", "os": "ubuntu", "os_version": "22.04" }
```

### DELETE (clear)

```bash
curl -s -X DELETE "$BASE_URL/agents/storage-lio-01/masters/_tpl_ubuntu_2204.img/tag" \
  -H "Authorization: Bearer $TOKEN"
```

Returns `{ "agent": ..., "name": ..., "removed": true|false }` (`removed=false` = there was no tag to begin with).

### Error Responses

| Status Code | Scenario |
|---:|---|
| `400` | `os` empty / illegal characters; `os_version` contains illegal characters |
| `401` | Missing or invalid Token |
| `404` | Agent not found |

## 6.5 One-Command Join: node-bound bootstrap token issuance + enroll auto-registration

### Description

The control plane issues a **node-bound bootstrap token** (`<6-hex>.<16-hex>`, only the sha256 hash is stored, TTL defaults to **60 minutes**, max 24 hours): issuance specifies `agent_id` (= the node declaration `metadata.name`, charset `[a-z0-9][a-z0-9.-]*`), and enroll only accepts that identity. Tokens are **single-use by default**: an allowance is reserved atomically before issuance and released if issuance fails; bulk onboarding uses `max_uses` (`--uses N`, max 100) explicitly instead of unlimited reuse. Every issuance is a new token (plaintext unrecoverable); **revocation is supported** (`DELETE /pki/tokens/{token_id}`, deletion takes effect immediately); unrevoked tokens expire naturally.

The node identity is a machine identity: when `kurrent token create` omits `--agent-id`, it
generates a random 12-hex id and bakes it into the printed join command (`--agent-id`);
human labels go into `metadata.remark`.

The `nvmet-host` component credential is **not issued here**: when an agent enrolls with `backend=nvmet`, the control plane derives one (bound to agent_id) and returns it in the enroll/renew response; the agent persists it to `storager/bootstrap/nvmet-host.token` for the nvmet-host container (capability-driven derivation — issuance does not foresee the backend).

### POST /pki/tokens

Request body: `{"usage": "bootstrap", "agent_id": "<node id>", "component": "agent", "max_uses": 1}`
(bootstrap is the default; `component` is `agent` | `nvmet-host`; `max_uses` is 1-100,
default 1), or `{"usage": "admin", "scopes": ["readonly"], "ttl_hours": 24}`
(management token for the authenticator chain Bearer path; scope is `admin` | `readonly` |
`enroll`, default `admin`; TTL is 1-720 hours, default 24). Scopes cap authority:
`readonly` is read-only, `enroll` may only issue bootstrap tokens (every other read or
write endpoint and session exchange returns `403`); `readonly` / `enroll` tokens may not
issue admin tokens.

#### curl

```bash
# Node bootstrap token (bound to a node identity)
curl -s -X POST "$BASE_URL/pki/tokens" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "node-01"}'

# Management token (scripts and WebUI sign-in; same identity as the admin cert)
curl -s -X POST "$BASE_URL/pki/tokens" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"usage": "admin"}'

# Read-only management token (24 hours)
curl -s -X POST "$BASE_URL/pki/tokens" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"usage": "admin", "scopes": ["readonly"], "ttl_hours": 24}'
```

#### Successful response (201)

```json
{
  "token": "a1b2c3.0123456789abcdef",
  "expires_at": "2026-09-22T12:00:00Z",
  "usage": ["enroll"],
  "groups": [],
  "scopes": [],
  "uses": 0,
  "max_uses": 1,
  "agent_id": "node-01",
  "component": "agent"
}
```

The `token` plaintext is visible only in this response (storage keeps only the
sha256 hash). `usage=["enroll"]` is a node bootstrap token (bound to `agent_id`,
single-use by default, 60 minutes); `usage=["admin"]` with `scopes=["admin"]` is a
management token (authenticator-chain Bearer; same as
`kurrent token create --usage admin`).

#### Common errors

| HTTP status | Common cause |
|---:|---|
| `400` | bootstrap token without/with an illegal `agent_id`, an out-of-range quota/TTL, or an invalid scope |
| `401` | Missing or wrong token |

### GET /pki/tokens

List **non-expired** token ledger entries (management visibility): bootstrap tokens, admin tokens, and nvmet-host derived tokens share one table. Never returns plaintext (already unrecoverable) or `token_secret_hash`; `created_at` is an empty string for legacy entries (the field is injected at issuance; historical entries lack it). `scopes` / `uses` / `max_uses` / `last_used_at` support audit and capacity observation (`max_uses=0` means unlimited: legacy entries and management tokens). Read requests only need authentication (minimal RBAC).

```bash
curl -s "$BASE_URL/pki/tokens" -H "Authorization: Bearer $TOKEN"
```

```json
[
  {
    "token_id": "a1b2c3",
    "usage": ["admin"],
    "groups": ["admin"],
    "scopes": ["admin"],
    "component": "",
    "agent_id": "",
    "name": "admin",
    "created_at": "2026-09-27T09:00:00+00:00",
    "expires_at": "2026-10-04T09:00:00+00:00",
    "last_used_at": "2026-09-27T10:12:00+00:00",
    "uses": 0,
    "max_uses": 0
  }
]
```

### DELETE /pki/tokens/{token_id}

Revoke a token: deletes the ledger entry, **deletion takes effect immediately** — Bearer validation re-reads the ledger on every request (no caching), so the token gets 401 right after revocation and cannot be restored. "Who revoked which token when" is recorded by the operation audit (`pki.token.revoke`). Write requests require the admin group.

```bash
curl -s -X DELETE "$BASE_URL/pki/tokens/a1b2c3" \
  -H "Authorization: Bearer $TOKEN"
```

Returns `{"revoked": "a1b2c3"}` on success; `404` when the token does not exist. The WebUI "Credentials" page and `kurrent token list|revoke` are the same calls.

### 6.5.1 enroll auto-registration (POST /enroll, nginx entry /api/cp/enroll)

First-report auto-registration: when an `agent` component enrolls (`POST /enroll`, Bearer = bootstrap token bound to that node identity) and the id is absent from `agents.yml`, the control plane **auto-creates the entry** (role_disk=True, role_cd=False, enabled=True, tags=`("auto", "storage")`; `("auto",)` only for legacy agents that report no capabilities — the **backend type is not written into tags**, it is resolved by `/boot-vars` querying `/capabilities` in real time) and persists the request’s `base_url` field (agent side `spec.agent.advertiseUrl`, control-plane-reachable address) and `remark` field (agent side `metadata.remark`, human label) into the registry. The response carries two certificates (client / serving, issued by the nodes CA, subject = CN only) and **two trust anchors**: `ca_nodes_crt` (validate peer components) and `ca_control_crt` (validate control-plane clients). Rules:

- Token validation: format / registry entry / component match / binding match (`agent_id` equals the ledger entry) / not expired; on success an allowance is **reserved atomically** and released if certificate issuance fails; single-use by default (`max_uses` can raise it)
- `/enroll/renew` uses the component’s existing client cert (nginx forces mTLS on that path; the issuer must be the nodes CA → class=component; human certs get `403`), and re-signs only when CN == `<prefix>-<agent_id>`
- `nvmet-host` components are **not auto-registered** (they share the agent_id; the agent component must be registered first — otherwise 400, container restart retries; the allowance is not consumed)
- A non-empty `base_url` must start with `http://` / `https://`, otherwise 400 (no auto-registration)
- When an already-registered agent enrolls/renews again, `base_url` / `remark` / `role.cd` are **refreshed from the node declaration** (declaration authority: the node’s file wins — after a node move or remark change, the next renewal aligns the registry, no reinstall needed); control-plane attributes `enabled` / `storager_ip` / `tags` are **left untouched**, and fields the node does not report keep their current values
- An agent reporting `backend=nvmet` gets an extra `nvmet_token` field in the response (derived nvmet-host credential); stgt/lio backends and legacy agents have no such field

### 6.5.2 One-command join (node side)

Declarative workflow (declaration-driven config, CLI as tooling): the control plane issues the token and prints a **join command carrying the control-plane URL and the node id**; pasting it on the storage node yields both the address and the identity — the node declaration `storager/kurrent.yaml` is auto-generated from defaults when missing (`metadata.name` = `--agent-id`, the address synced into `spec.controlPlane.url`) or read in and merged when present (a declared identity that differs from `--agent-id` is rejected), then the agent is converged-started (idempotent, re-runnable; use `--dir` to point at the storager directory from outside the repository root):

```bash
# Control plane (issue a node-bound bootstrap token; --cp-url = control-plane HTTPS entry reachable
# from the storage node, derived from --server otherwise; omitting --agent-id generates a random 12-hex id):
kurrent token create --agent-id node-01 --cp-url https://<cp-host>

# Storage node (the command already carries the URL and node id — paste and run; business keys can be pre-declared via kurrent config print node-defaults):
kurrent join https://<cp-host> --token a1b2c3.0123456789abcdef --agent-id node-01
```

## 6.6 DELETE /agents/{agent_id}

### Description

Removes the Agent’s registry entry (`config/agents.yml`) and cleans up its master tags (`state/masters.yml` entry under masters). **Control-plane records only — nothing is blocked and no certificates/running LUNs on the node are touched**.

Semantics (a corollary of declaration authority): the deletion is **soft** — the node still runs with its declaration and re-registers automatically at the next renewal (re-joining requires no new bootstrap token); to truly decommission a node, act on the **node side** (stop the containers / remove the declaration). Side effect: after the registry entry is removed, disks still hosted on that node cannot have their boot addresses projected until the node re-registers (the registry is the knowledge); the response reports this via `disks_hosted` / `affected_workers` (informational only, not blocking).

### curl

```bash
curl -s -X DELETE "$BASE_URL/agents/storage-lio-01" \
  -H "Authorization: Bearer $TOKEN"
```

### Successful response

```json
{ "deleted": "storage-lio-01", "master_tags_removed": false, "disks_hosted": 3, "affected_workers": ["worker-01", "worker-02"] }
```

### Common errors

| HTTP status | Common cause |
|---:|---|
| `401` | Missing or wrong token |
| `404` | Agent does not exist |

## 6.7 Management sessions and certificate revocation

### Sessions (server-side registry)

`POST /session` exchanges the current credential (admin certificate / management token)
for a session cookie; the cookie carries only `{jti, exp, epoch}`, and identity/groups
come from `state/sessions.yml`. The default TTL is 12 hours with sliding renewal only in
the second half; cookie-authenticated writes require `Origin` to match `Host`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/sessions` | List unexpired sessions (admin); `session_id` = `sha256(jti)`, not a usable credential |
| `DELETE` | `/sessions/{session_id}` | Revoke one: delete the entry; next request gets `401`; missing → `404` |
| `DELETE` | `/sessions` | Sign out all: clear the registry and bump `epoch`, invalidating every existing cookie |

`session_id` is 64 hex characters; pass it to DELETE verbatim (`kurrent sessions list`
prints the full value). Removing a user from `state/users.yml` makes that user's session
fail with `401` on its next request and cleans it up automatically.

### Certificate issuance, listing and revocation (users CA + CRL)

`POST /pki/users/{cn}` signs a **CSR** for a user **already registered** in
`state/users.yml` (admin only). The private key is generated locally by the requester and
**never uploaded**; the control plane only signs.

- The CN must exist in `state/users.yml` first (the certificate carries only the CN;
  groups come from that file); unknown users get `400`
- `csr` is required (PEM): its CN must equal the `cn` in the path, and it may not carry
  any subject attribute other than CN (self-reported O/OU are rejected); missing/invalid
  → `400`
- `days` is optional: 365 by default, 3650 max
- The response contains only `certificate` (PEM), never the private key; the certificate
  file stays at `state/pki/users/<cn>/issued/<serial>.crt` for audit; issuance emits a
  `user_cert_issued` alarm event
- Exception: the bootstrap admin certificate created by `kurrent init` is still generated
  locally by the control plane (local convenience path)

```bash
# Requester (local): generate key + CSR, keep the key at 0600 and never upload it
kurrent cert keygen alice --out ./alice
# Admin: read the CSR file and submit it
kurrent cert issue alice --csr ./alice/alice.csr --out ./alice --days 365
```

The request body is `{"csr": "<PEM>", "days": 365}`; the response carries certificate
metadata plus `certificate` (PEM).

`GET /pki/users` lists certificates issued by the users CA (revoked / expired included):

```json
[
  {
    "cn": "admin",
    "serial": "6f1c0d2e",
    "fingerprint": "<sha256 hex>",
    "created_at": "2026-10-23T09:00:00+00:00",
    "expires_at": "2027-10-23T09:00:00+00:00",
    "revoked_at": "",
    "reason": "",
    "expired": false
  }
]
```

`DELETE /pki/users/{cn}?reason=key_compromise` revokes a CN's certificates; add
`&serial=<hex>` to revoke exactly one (a user may hold an old and a new certificate):

- `reason` is `unspecified` (default) | `key_compromise` | `superseded` | `cessation_of_operation`
- Marks `state/pki/users/issued.yml`, re-signs `state/pki/nginx/users.crl` and
  `clients.crl` (users + nodes concatenated), then reloads nginx — the revoked certificate
  fails on its **next TLS handshake**
- Omitting `serial` revokes every unrevoked certificate for the user and deletes all of
  their sessions; with `serial`, only sessions bound to that certificate fingerprint are
  kicked (count returned as `sessions`)
- Cannot revoke the current principal's own certificate (`400`); no unrevoked certificate
  → `404`; CRL written but reload failed → `500` (revocation is not in effect yet)
- Emits a `crl_updated` alarm

The control plane re-signs the CRL and asks nginx to reload every 12 hours
(`CRL_REFRESH_HOURS`; `KURRENT_CP_CRL_REFRESH_INTERVAL` overrides the interval in
seconds, `<=0` disables it) so the 7-day `nextUpdate` cannot expire during long runs —
an expired CRL makes nginx reject **every** client certificate. The controller never
touches the container runtime: the reload request goes to the shared volume
(`/run/kurrent/nginx.reload`), an in-container watcher runs `nginx -s reload` and writes
`nginx.ack`, and the controller waits for that acknowledgement (timeout ⇒ `500`).

```bash
curl -s -X DELETE "$BASE_URL/pki/users/alice?reason=key_compromise" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{ "revoked": "alice", "serials": ["6f1c0d2e"], "sessions": 1, "reason": "key_compromise" }
```

CLI equivalents are `kurrent cert keygen <cn> [--out DIR]`,
`kurrent cert issue <cn> --csr FILE`, `kurrent cert list [-o json]` and
`kurrent cert revoke <cn> [--serial HEX] [--reason ...]`; the WebUI "Credentials" page
shows token scopes / last-used and the session registry.

### Component certificates (nodes domain, agent / nvmet-host)

`GET /pki/components` lists the component certificates signed by the nodes CA (one entry
each for `client` and `serving`):

```json
[
  {
    "cn": "agent-node-a",
    "agent_id": "node-a",
    "kind": "client",
    "serial": "3f9c1b7e",
    "fingerprint": "9c1f...",
    "created_at": "2026-10-23T09:00:00+00:00",
    "expires_at": "2027-01-21T09:00:00+00:00",
    "revoked_at": "",
    "reason": "",
    "expired": false
  }
]
```

`DELETE /pki/components/{agent_id}?reason=key_compromise` (optionally `&serial=<hex>`)
revokes that node's component certificates:

- records the revocation in `state/pki/components/issued.yml`, re-signs the nodes part of
  `clients.crl`, and reloads nginx — the revoked certificate fails the **next mTLS
  handshake** (both control-plane and peer-component directions)
- also deletes the node's bootstrap tokens (agent and nvmet-host paths), closing the
  "revoke, then enroll a fresh certificate" path; `/enroll/renew` additionally returns
  `403` for a revoked serial (application-side fallback while the CRL reload is pending)
- registration happens only at issuance (enroll / renew): certificates issued before P4
  have no registry entry and cannot be revoked until they are re-issued
- `404` when nothing is left to revoke; `400` for a bad `reason` / `serial`; `500` when
  the CRL was written but the nginx reload failed

```bash
curl -s -X DELETE "$BASE_URL/pki/components/node-a?reason=key_compromise" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{ "revoked": "node-a", "serials": ["3f9c1b7e", "7a2d4c11"], "kinds": ["client", "serving"], "reason": "key_compromise" }
```

CLI equivalents are `kurrent cert components [-o json]` and
`kurrent cert revoke-component <agent_id> [--serial HEX] [--reason ...]`.

---
---

## 7. POST /workers

### Description

Registers a Worker’s **identity**: hostname binding. **Storage and identity are separated** — this endpoint does not create any system disks. System disks must be created separately via `POST /workers/{worker_id}/luns/disk` (see 7.1). `mac` is now **optional**:

- Without `mac` → a pure idle Worker (hostname binding only; no device authorized). Bind a device later via `POST /devices/{mac}/bind` (16.7).
- With `mac` → the device must exist in the device pool (`state=pooled`); it is validated and bound directly (one-to-one authorization). If the device is out-of-pool or already bound, a `409` is returned — **register first, then bind**.

The Control Plane will:

1. Validate `worker_id`, `hostname`; validate `mac` if provided
2. Write to `state/workers.yml` (`disks` as empty array, `state=registered`)
3. If `mac` is provided: bind the device (write `state/devices.yml` `bound_worker_id` + write to `dnsmasq/dhcp-hosts.conf`)
4. Send a HUP signal to the `ipxe-dnsmasq` container via Docker:

```bash
docker exec ipxe-dnsmasq killall -HUP dnsmasq
```

5. If `windows_iso` is specified, additionally call the Agent to create a CD target (installation optical drive, unrelated to system disks).

### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `worker_id` | Yes | Worker identifier. Automatically lowercased. Allowed characters: letters, digits, dots, underscores, hyphens. |
| `mac` | No | Worker NIC MAC address, e.g., `00:0c:29:b9:8b:2d`. If provided, the device must already be in the pool (see 16); the device is bound to this Worker in the same call. |
| `hostname` | No | Hostname. Defaults to `worker_id` if not provided. |
| `arch` | No | Architecture. Defaults to `x86_64`. |
| `windows_iso` | No | Windows installation ISO filename. When provided, an installation CD target is created during registration. |
| `boot` | No | iPXE menu default item and timeout configuration. If omitted, `/boot-vars` derives them from the default boot disk and global defaults. Writes to the same ledger fields as the 7.3 `default-disk` endpoint; later calls override earlier ones. |

### `boot` Fields

| Field | Required | Description |
|---|---:|---|
| `menu_default` | No | Default item on the iPXE main menu (auto-selected after the menu times out); valid values in the 7.3 table; case-insensitive, e.g., `ubuntu`, `debian`, `windows`, `exit`. |
| `menu_timeout` | No | iPXE menu timeout in milliseconds, e.g., `5000`; `0` means the menu waits indefinitely and never auto-selects. |

When `boot` is omitted:

- `menu_default` defaults to `default_disk` (set separately after disk creation, see 7.3); if not set, defaults to `reboot` (loop reboot waiting for configuration, see section 5).
- `menu_timeout` defaults to `spec.boot.menuTimeoutMs` (currently `5000`) when a default boot is configured; in the `reboot` loop it is fixed to `spec.boot.autoBootTimeoutSec` (currently `1`, see section 5).

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

After creating a system disk, call `PUT /workers/{worker_id}/default-disk` (see 7.3) to set the default boot disk (os_tag references a specific disk), and `menu-default` will switch to the generic OS item `boot-os`.

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

During Windows installation, if you want to default to the installation menu:

```json
{
  "worker_id": "worker-win-build",
  "mac": "00:0c:29:b9:8b:11",
  "windows_iso": "Win11_24H2.iso",
  "boot": {
    "menu_default": "menu-install",
    "menu_timeout": 3000
  }
}
```

### PUT /workers/{worker_id}/mac (Rebind Mapping)

Changes the Worker’s MAC binding (hostname unchanged). It is mapped internally to a **device rebind**: the new MAC must be in the pool (`state=pooled`) and is bound to this Worker; the old device (if bound to this Worker) is released back to the pool. The audit trail records `device.bind` (new) + `device.unbind` (old) plus the compatibility `worker.mac.update`.

**Request body**: `{"mac": "00:0c:29:b9:8b:2d"}`

**409**: new MAC out-of-pool / revoked / already bound to another Worker; old device bound to an unexpected Worker.

**Idempotent**: setting the same MAC again returns `changed=false`.


## 7.1 POST /workers/{worker_id}/luns/disk

### Description

Creates a system disk LUN for a given Worker. System disks are categorized by (os, os_version); a Worker can have multiple system disks (at most one per OS+version pair; different versions of the same OS can coexist — distinguished by the disk-level `os_tag` random identifier). The Control Plane will:

1. Verify the Worker exists and does not already have a disk for that (os, os_version) (returns `409` if it does).
2. Generate the disk-level random identifier `os_tag` (12 hex chars, the data-plane unique key) — it determines the disk NQN suffix and filename.
3. Select a storage Agent (`disk_agent` if given; otherwise the placement policy decides: candidates must be enabled with a ready data plane, and for `type=master` must hold that master locally — no usable candidate returns `503` with `detail.excluded` explaining each node).
4. Assemble the disk NQN (`base-nqn:worker-id.os.<os_tag>`) and backing filename (`worker-id.os.<os_tag>.img`).
5. Call the Agent to create the disk target (master clone or empty disk).
6. Update the Worker’s `disks` inventory in `state/workers.yml` (append to array). On the first disk creation, `state` transitions from `registered` to `ready`.
7. **Issue on disk creation**: when the disk lands on an nvmet-backed node and the Worker has no key yet, the Control Plane generates a DHHC-1 key locally and pushes it (audited as `credential.issue`, see 7.7) — an existing key is not rotated (key follows the Worker; multiple disks share one key). Non-nvmet backends get no key (DHHC-1 is NVMe-oF-specific; iSCSI uses CHAP).

This endpoint resides under the `/luns/` namespace to reserve space for future data disks (`/luns/data`). In a multi-disk scenario, the default boot disk is determined by the `disk` (os_tag) parameter of `PUT /workers/{worker_id}/default-disk`.

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
| `os` | Yes | OS annotation (for human understanding; determines the os segment in the disk NQN/filename). Free string, lowercased, **no whitelist validation** (OS_ITEMS retired 2026-08-30). |
| `os_version` | No | Version annotation (`''` = no version, the default). At most one disk per (os, os_version); different versions can coexist. |
| `disk_agent` | No | Specify a storage Agent. If omitted, the Control Plane places it (candidates are checked for a ready data plane and local master availability). |

### 7.1.1 Clone from Master

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

### 7.1.2 Create Empty Disk

#### curl

```bash
curl -s -X POST "$BASE_URL/workers/worker-00/luns/disk" \
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
      "agent": "storage-lio-01",
      "nqn": "nqn.2026-07.com.controller:worker-01.ubuntu.0d26b6f33a89",  # NVMe-oF data-plane identifier (authoritative; NQN is not defined via IQN; suffix carries os_tag)
      "iqn": "iqn.2026-07.com.controller:worker-01.ubuntu.0d26b6f33a89",  # iSCSI data-plane identifier (derived from the NQN)
      "filename": "worker-01.ubuntu.0d26b6f33a89.img",
      "backing": "/home/iscsi_img/worker-01.ubuntu.0d26b6f33a89.img",
      "os": "ubuntu",
      "os_version": "22.04",
      "os_tag": "0d26b6f33a89",
      "source": {
        "type": "master",
        "name": "_tpl_ubuntu_2204.img"
      }
    }
  ],
  "cd": null,
  "worker_id": "worker-01",
  "mac": "00:0c:29:b9:8b:2d"
}
```

### Example Success Response (empty disk)

```json
{
  "hostname": "worker-00",
  "arch": "x86_64",
  "state": "ready",
  "disks": [
    {
      "agent": "storage-lio-01",
      "iqn": "iqn.2026-07.com.controller:worker-00.ubuntu.5f1c2a3b4d5e",
      "filename": "worker-00.ubuntu.5f1c2a3b4d5e.img",
      "backing": "/home/iscsi_img/worker-00.ubuntu.5f1c2a3b4d5e.img",
      "os": "ubuntu",
      "os_version": "",
      "os_tag": "5f1c2a3b4d5e",
      "source": {
        "type": "empty",
        "size": "40G"
      }
    }
  ],
  "cd": null,
  "worker_id": "worker-00",
  "mac": "00:0c:29:b9:8b:00"
}
```

### 7.1.3 Batch Create System Disks (POST /workers/luns/disk/batch)

Batch deployment scenario: apply the same disk parameters to multiple Workers. **Two mutually exclusive ways to place them** (supplying both returns `400`):

- `targets`: name the storage node **per item** (explicit override, the original form);
- `workers` + `placement`: declare only *which* Workers get a disk and let the **Control Plane place them**.

Placement semantics for the `workers` form — placement is a system decision; the caller supplies intent and constraints:

- **Candidate nodes** = disk role ∧ enabled (not draining) ∧ has `base_url` ∧ **data plane ready** (`healthz.backend_ready` is not `false`) ∧ listed in `placement.candidates` (empty = unrestricted) ∧ (for `type=master`) **the master image exists locally** — cloning happens locally on the node, so a node without that master cannot possibly serve the request;
- **Assignment** = fewest existing disks first (spread), ties broken by registration order;
- `placement.pins` pins individual Workers (explicit override): a pinned node does **not** have to be inside `candidates`, but it must itself be usable, otherwise `503`;
- No usable candidate → `503`, with `detail.excluded` giving the reason per node (wrong role / draining / unreachable / data plane not ready / missing master).

`dry_run=true` returns the plan (`plan` / `warnings`) without creating anything. **Preview and execution share the same placement implementation**, so the node in `plan` is exactly what execution will do (each row's `action` is `create` / `skip` / `error`). Note that a plan for the `targets` form does **not** probe the nodes (it judges create/skip/error from inventory alone), whereas every candidate of the `workers` form was probed during placement.

Same as single-disk creation: `master` clones from a master image, `empty` creates a blank disk. At most one disk per (os, os_version) is allowed; if a Worker already has one it is **automatically skipped** (not considered a failure). **Successfully created Workers automatically set `default_disk` to the batch disk’s `os_tag`** — batch deployment goes directly to the default boot without needing an extra `PUT /workers/{worker_id}/default-disk` call (single-disk creation does NOT set this automatically). Workers placed on nvmet-backed nodes also get their authentication key **issued on disk creation** (each Worker gets its own distinct key, in one pass — see 7.1 step 7 and 7.7). Each item is processed independently; a failure of one does not affect the others. Returns a summary of `succeeded` / `skipped` / `failed`. Items whose target Agent does not exist or is disabled (drain) land in `failed` (`agent not found` / `agent ... disabled`); the remaining items proceed as usual.

#### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `type` | Yes | `master` or `empty`. |
| `os` | Yes | OS annotation (same for all Workers in the batch; determines the os segment in the disk NQN/filename; free string, no whitelist validation). |
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
    "placement": { "candidates": ["storage-lio-01", "storage-lio-02"] },
    "dry_run": true
  }'
```

#### Plan Response (dry_run)

```json
{
  "dry_run": true,
  "plan": [
    { "worker_id": "worker-01", "agent": "storage-lio-01", "action": "create" },
    { "worker_id": "worker-02", "agent": "storage-lio-02", "action": "create" },
    { "worker_id": "worker-03", "agent": "storage-lio-01", "action": "skip",
      "reason": "already has a ubuntu system disk" }
  ],
  "warnings": [
    { "agent": "storage-stgt-01", "reason": "does not have master _tpl_ubuntu_2204.img" }
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
      { "worker_id": "worker-01", "agent": "storage-lio-01" },
      { "worker_id": "worker-02", "agent": "storage-lio-01" },
      { "worker_id": "worker-03", "agent": "storage-stgt-01" }
    ]
  }'
```

#### Example Response

```json
{
  "succeeded": [
    { "worker_id": "worker-01", "agent": "storage-lio-01", "iqn": "iqn.2026-07.com.controller:worker-01.ubuntu.0d26b6f33a89" },
    { "worker_id": "worker-03", "agent": "storage-stgt-01", "iqn": "iqn.2026-07.com.controller:worker-03.ubuntu.a1b2c3d4e5f6" }
  ],
  "skipped": [
    { "worker_id": "worker-02", "reason": "already has a ubuntu 22.04 system disk" }
  ],
  "failed": [
    { "worker_id": "worker-04", "agent": "storage-lio-01", "error": "worker not found: worker-04" }
  ]
}
```

---

## 7.2 Windows Installation: Identity Registration + ISO + System Disk

Windows installation is a two-step process: first register the identity (optionally specifying the installation ISO), then create the system disk.

### 7.2.1 Identity Registration (with ISO)

```bash
curl -s -X POST "$BASE_URL/workers" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "worker_id": "worker-win-build",
    "mac": "00:0c:29:b9:8b:11",
    "windows_iso": "Win11_24H2.iso"
  }'
```

After registration, the response has `state=installing` (a CD target exists) and `disks` as an empty array.

### 7.2.2 Create System Disk

```bash
curl -s -X POST "$BASE_URL/workers/worker-win-build/luns/disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "empty",
    "os": "windows",
    "size": "80G"
  }'
```

Response after creation:

```json
{
  "hostname": "worker-win-build",
  "arch": "x86_64",
  "state": "installing",
  "disks": [
    {
      "agent": "storage-lio-01",
      "iqn": "iqn.2026-07.com.controller:worker-win-build.windows.9b8a7c6d5e4f",
      "filename": "worker-win-build.windows.9b8a7c6d5e4f.img",
      "backing": "/home/iscsi_img/worker-win-build.windows.9b8a7c6d5e4f.img",
      "os": "windows",
      "os_version": "",
      "os_tag": "9b8a7c6d5e4f",
      "source": {
        "type": "empty",
        "size": "80G"
      }
    }
  ],
  "cd": {
    "agent": "controller-stgt",
    "iqn": "iqn.2026-07.com.controller:worker-win-build.windows.iso",
    "iso": "Win11_24H2.iso",
    "backing": "/home/iscsi_img/Win11_24H2.iso"
  },
  "worker_id": "worker-win-build",
  "mac": "00:0c:29:b9:8b:11"
}
```

After installation finishes, the CD target is cleaned up as part of the Worker deletion flow.

### Common Errors

| HTTP Status | Common Causes |
|---:|---|
| `400` | Parameter format error; `os` empty or illegal characters; `type=master` but `name` missing; `type=empty` but `size` missing |
| `401` | Missing or incorrect Token |
| `404` | Worker not found when creating a system disk; the `disk_agent` Agent does not exist |
| `409` | `worker_id` already exists; `hostname` already exists; MAC already bound; Worker already has a disk for that (os, os_version) (duplicate version); IQN already exists on Agent; backing file already exists; target Agent is disabled (drain: `agent ... disabled: not accepting new disks`) |
| `500` | dnsmasq binding write failed; file write failure; other unexpected errors |
| `503` | Agent unreachable |

---

## 7.3 PUT /workers/{worker_id}/default-disk

### Description

**What the “default boot disk” is for**: a Worker can have multiple system disks (at most one per OS+version pair, e.g., `ubuntu 22.04` + `windows`; different versions of the same OS can coexist). On every boot, the iPXE menu auto-selects one item after its timeout — the default boot disk configured here decides which item is auto-selected, and also decides which disk’s connection info `/boot-vars` projects (`disk_nqn` / `storager_ip` come from the default boot disk, see section 5). Without it, the menu auto-selects `reboot` with a 1 ms timeout and loops rebooting until the admin finishes configuration — never silently booting into the wrong OS.

**Note**: `disk` is not an OS name; it is the disk-level random identifier `os_tag` (generated at disk creation in 7.1, 12 hex chars, the data-plane unique key) — when multiple versions of the same OS coexist, only the os_tag can precisely locate the **specific disk** to boot by default.

The `/boot-vars` `menu_default` derivation chain:

```text
default_disk (the `disk` field of this endpoint, highest priority) -> boot.menu_default (the `menu_default` field of this endpoint) -> reboot (not configured, loop reboot waiting)
```

The three request body fields can be sent individually or in combination; at least one must be provided. Sending `null` (or an empty string) clears the corresponding item. This endpoint can be called repeatedly; later calls override earlier ones — they write the same ledger fields as the `boot` passed at registration (see 7.0).

Requirements:

- When setting `disk`: The Worker must already have a system disk with that `os_tag` (created by `POST /workers/{worker_id}/luns/disk`), otherwise a `400` is returned listing the current system disks. In the multi-disk model, use `os_tag` to precisely match the disk to boot by default.
- When setting `menu_default`: The value must be a valid item ID from the `menu.ipxe` main menu (strict validation to prevent an empty `choose --default` in iPXE); OS-semantics values (legacy OS names) are normalized to the generic OS item `boot-os`.
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
| Generic OS item | `boot-os` (derived from the default disk configuration, not set manually here; legacy OS-semantics values normalize to it) |
| Tools / Installation | `menu-diag` `menu-install` |
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
curl -s -X PUT "$BASE_URL/workers/worker-win-build/default-disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "menu_default": "menu-install",
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
| `409` | Setting `disk` when the Worker has no system disks |

---

## 7.4 DELETE /workers/{worker_id}/luns/disk/{os_tag}

### Description

Deletes a single system disk of a given Worker (by the disk-level random identifier `os_tag`, 12 hex chars). The Control Plane will:

1. Verify the Worker exists and has a disk with that `os_tag` (returns `404` if not).
2. Call the hosting Agent to delete the iSCSI target.
3. Remove the disk entry from the Worker’s `disks` array in `state/workers.yml`.
4. Cleanup linkage: if the deleted disk was the default boot disk (`default_disk` equals its `os_tag`), both `default_disk` and any matching (same-OS) `boot.menu_default` are cleared (to prevent iPXE from booting to a deleted disk).
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

Returns the full Worker inventory (with `disks` no longer containing the deleted system disk; if it was the default, `default_disk`/`boot.menu_default` are cleared; if no disks remain, `state=registered`).

### Common Errors

| HTTP Status | Common Causes |
|---:|---|
| `400` | Invalid `os_tag` (not 12 hex chars) |
| `401` | Missing or incorrect Token |
| `404` | Worker not found, or the Worker does not have a disk with that `os_tag` |

---

## 7.6 POST /workers/batch

### Description

Bulk-creates Workers (**per-item independent** — a failure of one item does not affect the others; **idempotent** — re-running does not duplicate Workers). `worker_id`s are generated as `name_prefix` + an index (`worker-01`, `worker-02`, …; the index starts at `01` and its width adapts to `count` — `count=100` yields `worker-001` … `worker-100`).

- Without `macs` → all Workers are **idle** (hostname binding only, no device authorized); bind them later via `POST /devices/{mac}/bind` (16.7)
- With `macs` (must be the same length as `count`) → each MAC is validated against the device pool and bound directly (same semantics as passing `mac` in section 7: the device must be `state=pooled`; pool-miss / already bound → that item is `failed` and **not created**, retry after fixing)

`windows_iso` is not supported (register installation ISOs one by one via section 7).

### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `count` | Yes | Number of Workers to create, 1–100 |
| `name_prefix` | No | Worker ID prefix, default `worker-`. The generated `worker_id`s must be valid (letters, digits, dot, underscore, hyphen allowed); an invalid prefix rejects the whole batch with `400` |
| `macs` | No | Array of MAC addresses (format `00:0c:29:b9:8b:2d`); when provided, its length must equal `count`, and each MAC is validated and bound directly |
| `arch` | No | Architecture. Defaults to `x86_64` |
| `boot` | No | iPXE menu default and timeout config; fields as in section 7 |

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

## 7.7 NVMe-oF Authentication Credentials (/workers/{worker_id}/credential)

### Description

The NVMe-oF connection authentication key store (key follows the Worker ruling, 2026-08-22):
keys are indexed by `worker_id`, and all devices bound to that Worker share the same key
(a rebind rotates the key). Keys use the **DHHC-1** format (blueprint 2.1 contract):
`DHHC-1:<two-digit type>:<base64(key + CRC32 little-endian 4 bytes)>`, with a 32-byte
(SHA-256) or 64-byte (SHA-512) key body.

**Issuance timing: issued on disk creation (2026-10-05)** — when a system disk is created on an
nvmet-backed node (7.1 step 7, single and batch alike), the Control Plane generates a DHHC-1 key
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

**Push reconciliation (2026-10-05)**: the per-Agent outcome of every push is recorded in
`state/credential-push.yml` (key = `worker_id/agent_id`, storing the audit hash of the pushed
key, never the plaintext). A control-plane reconciliation thread (default interval 60s,
`KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL` seconds, 0 disables) compares desired state against
the records: pairs whose key hash matches and were delivered (ok / n-a) are skipped, everything
else is replayed idempotently — closing three silent gaps: failed pushes without retry, crash
windows (ledger changed, nothing pushed) and undelivered revocations. A pair with no key and no
push history is never pushed — no ledger entry, no audit, no false "sync failed" while its Agent is
offline; with nvmet disk creation issuing keys automatically, this only covers disks on non-nvmet
backends (DHHC-1 not applicable, a push would only return `n/a`). Once a push record exists,
later revocations keep being replayed even when undelivered. Records for unregistered
Agents are pruned (re-registration triggers a fresh push). Reconcile-driven pushes audit only on
state/key changes (noise suppression); user-triggered pushes audit every time.
Push outcomes: `ok` delivered (Agent cache and host service both in sync); `failed` not delivered
(Agent unreachable / host sync error), reconciliation keeps retrying; `n/a` target Agent is not an
nvmet backend (not applicable, not a failure); `pending` no record yet or a stale one (awaiting
replay); `none` no sync target (no NVMe-oF disks, no credential ever set, or the hosting Agent is unregistered).

Operations that trigger a push: **issuance on disk creation** (nvmet backends, see above),
setting/deleting credentials, device bind/unbind/rebind
(`PUT /workers/{worker_id}/mac`), disk delete (including batch), and Worker deletion.

**Periodic rotation (2026-10-23)**: each reconciliation cycle first checks the DHHC-1 key age
(`rotated_at`, falling back to `updated_at` / `created_at`). Past
`KURRENT_CP_CREDENTIAL_ROTATION_DAYS` (default 90 days, `<=0` disables it) the key is rotated:
a new key is generated and pushed to every target Agent of that Worker, and the ledger
`state/credentials.yml` is switched **only after all pushes succeed** (`rotated_at` /
`updated_at` refreshed, `credential.rotate` audited). If any target fails, already-delivered
targets get the old key pushed back and the ledger stays unchanged, retrying next cycle; entries
without an nvmet target are skipped. Until the switch, `/boot-vars` still projects the old key,
so the window in which hosts hold the new key covers only the push itself.

Static storage boundary (2026-10-05): the plaintext key lands in exactly four places along the
chain — control-plane `state/credentials.yml`, the Agent container credential cache (which also
holds the derived TLS PSK; both 0600), the storage node configfs (kernel hosts matrix `dhchap_key`),
and the storage node's kernel global `.nvme` keyring (TLS PSK, at runtime). **No application-layer
encryption is introduced**; the boundary is the deployer's responsibility (disk/host persistence
encryption, root access boundary). The key travels over the mTLS component channel and never enters
browser storage or server logs. Data-plane encryption (NVMe/TCP TLS 1.3, the firmware and kernel
boot segments sharing port `4421`) is designed
and verified per the local blueprint layer (`blueprint/nvme-tcp-tls-in-kernel.md`, not versioned).

Credential boundary (who distributes, who self-holds): the stateless boot chain distributes exactly
two credentials — the DHHC-1 authentication key and the TLS PSK derived from it — plus a non-secret
one-time nonce (`GET /devices/challenge`). The device identity private key (ECDSA P-256) is **never
distributed**: it is generated on the device, stays in NVRAM, and the control plane only collects the
public key (`key_hash`/`pubkey_hash`). The firmware's TOFU fingerprint for the control-plane HTTPS
endpoint is pinned on first connect, not pushed. Component bootstrap credentials — the bootstrap
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
      {"agent_id": "storage-nvmet-01", "result": "ok", "updated_at": "2026-10-05T15:30:00+08:00"}
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
      {"agent_id": "storage-nvmet-01", "result": "ok", "updated_at": "2026-10-05T15:30:00+08:00"}
    ]
  }
}
```

When there is no entry, `exists=false` and `secret_hash` is absent; the `push` projection is
still returned (revocation delivery stays visible).
CLI: `kurrent workers credential show|set|revoke <worker_id>`; WebUI: the dedicated "NVMe-oF Credential"
tab on the Worker detail page (shown only when the Worker has an NVMe-oF disk).

---

## 8. GET /workers

### Description

Lists the inventory of all current Workers. The `mac` field in the response is a real-time reverse lookup from `dnsmasq/dhcp-hosts.conf`.

### curl

```bash
curl -s "$BASE_URL/workers" \
  -H "Authorization: Bearer $TOKEN"
```

### Example Success Response

```json
[
  {
    "hostname": "worker-00",
    "arch": "x86_64",
    "state": "ready",
    "disks": [
      {
        "agent": "storage-lio-01",
        "iqn": "iqn.2026-07.com.controller:worker-00.ubuntu",
        "filename": "worker-00.ubuntu.img",
        "backing": "/home/iscsi_img/worker-00.ubuntu.img",
        "os": "ubuntu",
        "source": {
          "type": "empty",
          "size": "40G"
        }
      }
    ],
    "cd": null,
    "worker_id": "worker-00",
    "mac": "00:0c:29:b9:8b:00"
  }
]
```

---

## 9. GET /workers/{worker_id}

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

## 10. GET /workers/{worker_id}/status

### Description

Queries the Worker’s inventory information and performs real-time checks:

- Whether the hostname’s MAC exists in `dnsmasq/dhcp-hosts.conf`.
- Whether the corresponding disk target(s) exist on the Agent(s).
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
        "agent": "storage-lio-01",
        "iqn": "iqn.2026-07.com.controller:worker-01.ubuntu",
        "filename": "worker-01.ubuntu.img",
        "backing": "/home/iscsi_img/worker-01.ubuntu.img",
        "os": "ubuntu",
        "source": {
          "type": "master",
          "name": "_tpl_ubuntu_2204.img"
        }
      }
    ],
    "cd": null,
    "worker_id": "worker-01",
    "mac": "00:0c:29:b9:8b:2d"
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
          "iqn": "iqn.2026-07.com.controller:worker-01.ubuntu",
          "luns": [
            {
              "backing": "/home/iscsi_img/worker-01.ubuntu.img"
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

## 11. DELETE /workers/{worker_id}

### Description

Deletes a Worker. The Control Plane will:

1. Look up the Worker’s disk and CD inventory from `workers.yml`.
2. **Push credential revocation**: revoke to the registered Agents holding its disks (the Agent drops the hosts entry and clears its cache) — the only credential push during deletion.
3. **Cascade-unbind devices**: every device whose `bound_worker_id` is this Worker is released back to the pool (`state=pooled`, `bound_worker_id=null`; the devices are **not** revoked) — the unbind is persisted first, and if it fails the deletion is aborted.
4. If a CD target exists, delete it first.
5. Then delete the disk target(s).
6. Remove the Worker from `workers.yml`.
7. **Forget the Worker’s credential traces**: drop its key entry from `state/credentials.yml` and its push-state records (audit `credential.forget`) — the Worker no longer exists, and an orphaned entry is neither visible (`GET /credential` returns 404 for a nonexistent Worker) nor removable, while a later Worker re-created with the same id would **silently inherit the old key** and have it pushed to the new machine’s disks.
8. Remove the `mac,hostname` line from `dnsmasq/dhcp-hosts.conf`.
9. Send HUP to `ipxe-dnsmasq`.

The same cascade-unbind and credential cleanup apply to `POST /workers/delete/batch` (11.1).

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

## 11.1 POST /workers/delete/batch

### Description

Batch delete Workers. Each item is processed independently; **a failure of one does not affect the others**. Returns a summary of `succeeded` / `failed`. The processing per Worker is identical to the single deletion in section 11 (delete CD/system disk targets → remove from inventory → remove dnsmasq binding). After all successful items are processed, the inventory is saved and dnsmasq is reloaded **only once**. Non-existent Workers are counted as `failed` (`worker not found`).

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

## 12. GET /operations

### Description

Reads the Control Plane operation audit log. This file is an incremental query interface over `state/operations.jsonl`.

### Query Parameters

| Parameter | Required | Default | Description |
|---|---:|---|---|
| `since` | No | `0` | Only return records with `id > since`. |
| `limit` | No | `1000` | Maximum number of records to return. |
| `mac` | No | — | Only return operations of this device (MAC, normalized then filtered by the `mac` field); used for viewing device binding history. |

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
      "agent": "storage-lio-01",
      "iqn": "iqn.2026-07.com.controller:worker-01.ubuntu"
    }
  ]
}
```

---

## 13. Typical Test Sequence

It is recommended to verify in this order:

### 13.1 Check Service Liveness

```bash
curl -s "$BASE_URL/healthz"
```

### 13.2 Check Boot Variable Projection

```bash
curl -s "$BASE_URL/boot-vars?mac=000c29b98b2d&hostname=worker-01"
curl -s "$BASE_URL/boot-vars?mac=000c29b98b2d&hostname=worker-01&format=json"
```

### 13.3 Check Agent Configuration and Capabilities

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

### 13.4 Register Worker Identity (hostname + MAC binding)

```bash
curl -s -X POST "$BASE_URL/workers" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "worker_id": "worker-00",
    "mac": "00:0c:29:b9:8b:00"
  }'
```

At this point, the Worker is MAC-bound but has no system disk (`state=registered`, `disks` is an empty array).

### 13.5 Create a System Disk for worker-00 (empty disk)

```bash
curl -s -X POST "$BASE_URL/workers/worker-00/luns/disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "empty",
    "os": "ubuntu",
    "size": "40G"
  }'
```

After the disk is created, `state` transitions from `registered` to `ready`.

### 13.6 Set Default Boot Configuration

```bash
# disk = the os_tag returned by disk creation in 13.5 (12 hex chars, precisely references a specific disk)
curl -s -X PUT "$BASE_URL/workers/worker-00/default-disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "disk": "0d26b6f33a89"
  }'
```

`/boot-vars` will now return `menu-default=boot-os` (2026-08-30 MAIN MENU dynamicization: the OS item has converged to a single generic item). See section 7.3 for examples of setting menu items and timeout.

### 13.7 Query Worker Inventory

```bash
curl -s "$BASE_URL/workers/worker-00" \
  -H "Authorization: Bearer $TOKEN"
```

### 13.8 Query Real-Time Status

```bash
curl -s "$BASE_URL/workers/worker-00/status" \
  -H "Authorization: Bearer $TOKEN"
```

### 13.9 Delete Worker, Keeping the Empty Disk File

```bash
curl -s -X DELETE "$BASE_URL/workers/worker-00?delete_disk=false" \
  -H "Authorization: Bearer $TOKEN"
```

This step is ideal for a workflow where you create an empty disk, then manually rename it to a master image.

---

## 14. Agent iSCSI LUN/Target Management

### Description

The Control Plane can directly manage iSCSI targets/LUNs on any Agent. Requests are forwarded by the Control Plane to the Agent over mTLS (the Control Plane’s component client certificate, bound to the internal CA), so callers only need the Control Plane Token and never need to contact the Agent directly.

Differences from Worker lifecycle endpoints (`POST /workers`, `DELETE /workers/{worker_id}`):

- Worker endpoints are **inventory-oriented**: they automatically assemble IQNs, write `state/workers.yml`, and write dnsmasq bindings.
- LUN management endpoints are **data-plane direct management**: they do not write any inventory; they directly operate targets on the Agent. Suitable for master image management, manual troubleshooting, temporary ISO mounting, etc.

All endpoints require authentication (see 1.3, authenticator chain). If the Agent is not found, `404 agent not found` is returned. If the Agent is unreachable, `503` is returned. Business validation errors from the Agent side (e.g., IQN prefix mismatch, file already exists, IQN already exists) are passed through with the Agent’s status code and `detail`:

```json
{"agent": "storage-lio-01", "error": "iqn base mismatch: ..."}
```

### 14.1 GET /agents/{agent_id}/luns

Lists all iSCSI targets/LUNs on the specified Agent. The response structure depends on the Agent backend (stgt includes a `tid` field, LIO is the parsed output of `targetcli`). The Control Plane passes it through unchanged.

#### Path Parameters

| Parameter | Required | Description |
|---|---:|---|
| `agent_id` | Yes | Agent identifier, corresponding to a key in `config/agents.yml`. |

#### curl

```bash
curl -s "$BASE_URL/agents/storage-lio-01/luns" \
  -H "Authorization: Bearer $TOKEN"
```

#### Example Success Response

```json
[
  {
    "iqn": "iqn.2026-07.com.controller:worker-01.ubuntu",
    "luns": [
      {
        "backing": "/home/iscsi_img/worker-01.ubuntu.img"
      }
    ]
  }
]
```

### 14.2 POST /agents/{agent_id}/luns/disk

Creates a disk LUN on the specified Agent. Pass `master` to clone from a master image (prefers btrfs / ZFS(≥2.2) reflink for instant cloning), or pass `size` to create an empty sparse disk. If the Agent is not configured with `role.disk`, it returns `400 agent ... not configured for disk role`; if the Agent is disabled (drain), it returns `409 agent ... disabled: not accepting new LUNs`; on the nvmet data plane, a master in 512-byte-sector GPT layout is rejected with `400` (including 4K rebuild guidance; see the `gpt` field in section 15).

#### Path Parameters

| Parameter | Required | Description |
|---|---:|---|
| `agent_id` | Yes | Agent identifier. |

#### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `iqn` | Yes | Target IQN (the derived form of the disk NQN). Must be prefixed with the Agent’s IQN base (derived from `IPXE_NQN_BASE`). |
| `filename` | No | Backing filename. If omitted, the Agent auto-generates one from the IQN. |
| `master` | Conditionally required | Master image filename (located in `DISK_DIR`). Mutually exclusive with `size`. |
| `size` | Conditionally required | Empty disk size, e.g., `40G`. Mutually exclusive with `master`. |

#### curl

```bash
# Clone from master
curl -s -X POST "$BASE_URL/agents/storage-lio-01/luns/disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "iqn": "iqn.2026-07.com.controller:worker-02.ubuntu",
    "master": "_tpl_ubuntu_2204.img"
  }'

# Create empty disk
curl -s -X POST "$BASE_URL/agents/storage-lio-01/luns/disk" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "iqn": "iqn.2026-07.com.controller:worker-02.ubuntu",
    "filename": "worker-02.ubuntu.img",
    "size": "40G"
  }'
```

#### Example Success Response

```json
{
  "iqn": "iqn.2026-07.com.controller:worker-02.ubuntu",
  "backing": "/home/iscsi_img/worker-02.ubuntu.img"
}
```

### 14.3 POST /agents/{agent_id}/luns/cd

Creates a CD (ISO virtual drive) LUN on the specified Agent. Only Agents with `role.cd` set to true support this. If the Agent is not configured for the CD role (e.g., LIO), it returns `400 agent ... not configured for cd role`. Backend capability limitations are passed through from the Agent. A disabled (drained) Agent returns `409 agent ... disabled: not accepting new LUNs`.

#### Request Body Fields

| Field | Required | Description |
|---|---:|---|
| `iso` | Yes | ISO filename (present in `DISK_DIR`). |
| `iqn` | No | Target IQN. If omitted, the Agent auto-generates one from the derived IQN base (from `IPXE_NQN_BASE`):`iso_filename`. |

#### curl

```bash
curl -s -X POST "$BASE_URL/agents/controller-stgt/luns/cd" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "iso": "Win11_24H2.iso"
  }'
```

### 14.4 DELETE /agents/{agent_id}/luns

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
curl -s -X DELETE "$BASE_URL/agents/storage-lio-01/luns?iqn=iqn.2026-07.com.controller:worker-02.ubuntu" \
  -H "Authorization: Bearer $TOKEN"

# Delete target and backing file, continue even if target was already missing
curl -s -X DELETE "$BASE_URL/agents/storage-lio-01/luns?iqn=iqn.2026-07.com.controller:worker-02.ubuntu&delete_file=true&ignore_missing=true" \
  -H "Authorization: Bearer $TOKEN"
```

#### Example Success Response

```json
{
  "deleted": "iqn.2026-07.com.controller:worker-02.ubuntu",
  "delete_file": false
}
```

When ignoring a missing target:

```json
{
  "deleted": "iqn.2026-07.com.controller:worker-02.ubuntu",
  "delete_file": true,
  "ignored_missing": true
}
```

### 14.5 POST /agents/{agent_id}/luns/scan

Triggers the Agent to scan its image directory and rebuild targets for missing `.img`/`.iso` files (file-is-truth). For stgt backends, the result includes what was recreated. For LIO backends, due to `saveconfig` persistence, it usually skips everything.

#### curl

```bash
curl -s -X POST "$BASE_URL/agents/storage-lio-01/luns/scan" \
  -H "Authorization: Bearer $TOKEN"
```

#### Example Success Response

```json
{
  "created": [
    {
      "iqn": "iqn.2026-07.com.controller:worker-02.ubuntu",
      "cd": false
    }
  ],
  "skipped": []
}
```

---

## 15. GET /masters (Master Image List)

### Description

Aggregates and lists the master images from all **enabled disk-role** Agents (`enabled=true` and `role.disk=true`). Master images are identified by the storage Agent’s background scanning thread (every 30 seconds by default), which looks for image files in `DISK_DIR` whose names contain the `_tpl_` marker (case-insensitive, e.g., `_tpl_ubuntu_2204.img`).

Used by the WebUI for the master clone drop-down list. **It has no linkage to Worker creation APIs** — purely a read-only query, does not change any state or write inventory.

Each master entry carries a `gpt` field (GPT sector layout probe: `4k` / `512` / `none`). On the nvmet data plane, **a `512` master cannot be cloned** — the file backend exports logical blocks in the backing FS block size (typically 4K), a 512-byte-sector GPT is then interpreted as 4K by the initiator, and EDK2 cannot find the ESP (observed as a boot failure). The Agent rejects such clones at creation time (see 14.2); the frontend labels them accordingly.

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
      "agent": "storage-lio-01",
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

## 16. Device Pool (Device Inventory)

The device inventory (`state/devices.yml`) is the bottom-layer entity of the three-layer entity model (device / Worker / system disk): auto-registration and manual import only enter the device pool — **registration ≠ authorization**: a device only gains an identity after being bound to a Worker. The binding relationship is authoritative on the device side (`bound_worker_id`); the Worker side only projects it and does not store it.

**Binding semantics (P2)**: device ↔ Worker is strictly **one-to-one**. A device binds via `POST /devices/{mac}/bind` (16.7); `force=true` performs an atomic rebind (pre-check → new binding persisted → old binding cleared → rollback on failure). Unbinding (`DELETE /devices/{mac}/bind`, 16.8) returns the device to the pool; the Worker's system disks are kept. Deleting a Worker cascades an unbind (11). `POST /workers` with a `mac` also binds directly (7).

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
    key_hash: null                # filled in the security blueprint phase; empty for now
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

### 16.1 GET /devices

Device pool list with `state` filtering (`all` / `pooled` / `bound` / `revoked`, default `all`).

**curl**:

```bash
curl "$BASE_URL/devices?state=pooled" \
  -H "Authorization: Bearer $TOKEN"
```

**Successful response**: an array; each item is a device record (see “Record Structure” above).

### 16.2 GET /devices/{mac}

Single device detail (bound Worker, fingerprint, first/last report). `mac` uses the colon format (`00:0c:29:b9:8b:2d`).

**404**: device not found.

### 16.3 POST /devices

Manually register a device: MAC (+ optional UUID / manufacturer / product / serial) into the pool.

**Request body**:

| Field | Required | Type | Description |
|---|---|---|---|
| `mac` | Yes | str | MAC address |
| `uuid` | No | str | SMBIOS UUID (dual factor, optional) |
| `manufacturer` / `product` / `serial` | No | str | Reported info; initial inventory values only, superseded by device reports |

**Successful response (201)**: device record (`state=pooled`, `source=manual`).

**409**: device already exists (including revoked — revoked devices cannot be re-registered).

### 16.4 POST /devices/import

Bulk import a device manifest (pre-import of the MAC manifest): each entry is independent; duplicates are skipped; invalid/revoked entries count as `failed`.

**Request body**:

| Field | Required | Type | Description |
|---|---|---|---|
| `entries` | Yes | array | Manifest array; each entry follows the 16.3 request body (`mac` required) |

**Successful response**:

| Field | Description |
|---|---|
| `created` | MACs newly added to the pool |
| `skipped` | Already existing (pooled/bound) entries and reasons |
| `failed` | Invalid MAC / revoked entries and reasons |

### 16.5 DELETE /devices/{mac}

Revoke a device: `pooled` → `revoked`; the record stays in the inventory (audit retention).

**409**: device is bound to a Worker (must unbind first) or already revoked.

### 16.5.1 DELETE /devices/revoked

Purges revoked devices (`revoked` → entry deleted); `dry_run=true` only returns the MACs that
would be purged and leaves the ledger untouched. Only revoked entries are removed — `pooled` /
`bound` devices are never touched; an empty list is returned when there is nothing to purge
(idempotent). Intended for cleaning up probe leftovers or mistakenly revoked devices.
Repeat `mac` to scope the purge (e.g. `?mac=02:00:00:00:00:01&mac=02:00:00:00:00:aa`; MACs that
are not revoked are ignored); without it every revoked device is purged.

```bash
curl -s -X DELETE "$BASE_URL/devices/revoked?dry_run=true" -H "Authorization: Bearer $TOKEN"
curl -s -X DELETE "$BASE_URL/devices/revoked?mac=02:00:00:00:00:01&mac=02:00:00:00:00:aa" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{ "purged": ["02:00:00:00:00:01", "02:00:00:00:00:aa"], "count": 2, "dry_run": false }
```

### 16.6 GET /devices/report

iPXE device info reporting endpoint (**no auth**; `boot.ipxe.cfg` chains it before `/boot-vars`): updates the fingerprint + `last_seen`; an unknown MAC enters the pool only while the registration window is open, the admission quota is not used up, and a valid public key is carried. For an existing device, `uuid` can only fill an empty value and is never overwritten by a reported value (the binding comparison basis is not writable through an unauthenticated endpoint). **Returns an empty script body** (`#!ipxe\n`; consumable directly by `chain`, no script side effects; an empty body is reported as EOF by some iPXE builds, so a legal empty script is returned uniformly).

| Parameter | Required | Description |
|---|---|---|
| `mac` | Yes | MAC; accepts colon format (`${mac}`) and compact hex (`${netX/mac}`) |
| `uuid` / `manufacturer` / `product` / `serial` / `cpumodel` / `mem-type` / `chip` / `busid` | No | String fields; empty values tolerated |
| `mem-total` / `mem-speed` | No | Integer; accepts `0x` hex and decimal; stored normalized as decimal |
| `pubkey` | No | ECDSA P-256 public key (130-hex uncompressed point), used for enrollment/claim; absent or invalid → ignored |

Behavior:

- Registered device: updates the fingerprint (non-empty fields overwrite) + `last_seen`; `state` unchanged; while the window is open, a valid public key fills in `key_hash` (key claim, not constrained by the admission quota); a key mismatch is rejected (only audited as `device.claim rejected`)
- Revoked device: ignored (not updated, not resurrected)
- Unknown MAC + window open + quota left + valid public key: enters the pool (`state=pooled`, `key_hash` filled, `source=ipxe`)
- Unknown MAC + window closed / quota used up / no public key: fingerprint only, no pool entry (enrollment only during the window and bounded by the admission quota, see 5.1)

**Rate limit**: sliding window per source IP (`X-Real-IP`, overwritten by nginx), 120 requests/minute by default; over the limit returns `429` (with `Retry-After`) and emits a `boot_rate_limited` alarm. Override with `KURRENT_CP_RATE_LIMIT_REPORT`, `<=0` disables. This is scan suppression, not an identity boundary.

**curl** (simulating an iPXE report; the boot chain uses the **bare path** — no `/api/cp` prefix — and requires no authentication):

```bash
curl "https://<host>/devices/report?mac=000c29b98b2d&uuid=4c4c4544-...&manufacturer=ASUSTeK%20COMPUTER%20INC.&product=ROG%20Zephyrus%20G15&cpumodel=Intel(R)%20Core(TM)%20Ultra%207%20155H&mem-total=0x8000&mem-type=DDR5&mem-speed=5600&chip=RTL8125&busid=0110ec8125"
```

### 16.7 POST /devices/{mac}/bind

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

### 16.8 DELETE /devices/{mac}/bind

Unbinds a device: back to the pool (`state=pooled`, `bound_worker_id=null`), the dnsmasq binding is removed and reloaded; the Worker's system disks are kept (its `readiness` degrades to `partial`/`idle`).

**409**: device not bound. **404**: device not found.

**curl**:

```bash
curl -X DELETE "$BASE_URL/devices/00:0c:29:b9:8b:2d/bind" \
  -H "Authorization: Bearer $TOKEN"
```

### 16.9 POST /devices/bind/batch/preview

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

### 16.10 POST /devices/bind/batch

Bulk bind **execution** (idempotent, per-entry independent; failures of one entry do not affect the rest). Same request body as 16.9. Classification:

- `succeeded`: bound now (with `fingerprint_mismatch` marker if declared values differ)
- `skipped`: already bound (same Worker) / device already bound to another Worker / Worker already bound / duplicate MAC in manifest
- `failed`: device not found (out-of-pool — import first via 16.3/16.4) / invalid MAC

**Audit**: besides the `device.bind.batch` summary, every `succeeded` entry also records a per-device `device.bind` (`mac` / `worker_id`), keeping device binding history (`GET /operations?mac=`) complete; `skipped` / `failed` are only counted in the summary.

**Successful response**: `{succeeded: [...], skipped: [...], failed: [...]}`. Re-running an already-completed manifest returns everything as `skipped`.

**curl**:

```bash
curl -X POST "$BASE_URL/devices/bind/batch" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"mode":"manifest","pairs":[{"mac":"00:0c:29:b9:8b:2d","worker_id":"worker-01"}]}'
```

---

## 17. Current Implementation Boundaries

The current version supports:

- Worker identity registration (hostname binding; `mac` optional — providing it binds the device directly)
- Bulk Worker creation (count + naming rule, optional `macs` direct bind, `POST /workers/batch`, 7.6)
- Device inventory (auto pool entry / manual registration / bulk import / revoke, `/devices` endpoints)
- Device↔Worker one-to-one binding (bind / force rebind / unbind / batch bind preview + execution, 16.7–16.10)
- Worker `mac` rebind mapping (`PUT /workers/{worker_id}/mac`, 7)
- Cascade unbind on Worker deletion (11 / 11.1)
- Boot-vars anti-impersonation (binding as authentication, 5)
- iPXE device info reporting (11-field fingerprint, `GET /devices/report`; auto-registration only enters the pool, no Worker creation)
- Worker system disk creation (`POST /workers/{worker_id}/luns/disk`)
- Setting Worker default boot configuration (default disk / menu item / timeout, `PUT /workers/{worker_id}/default-disk`)
- Worker deletion
- Agent selection
- Agent LUN direct management (list / create disk / create CD / delete / scan)
- Master image list query (`GET /masters`, backed by periodic background scanning on storage nodes)
- Master tag registration (os / os_version annotations, `PUT/DELETE /agents/{agent_id}/masters/{master_name}/tag`, 6.4; aggregated and merged in `/masters`)
- Windows ISO special handling
- dnsmasq hostname bindings
- Worker and operation audit trail queries
- Multi-OS system disks (a Worker can have disks for multiple OSes: at most one per (os, os_version), different versions can coexist, distinguished by the disk-level `os_tag`, with `default_disk` determining the default boot)
- NVMe-oF authentication key store (DHHC-1, key follows Worker, 7.7; `/boot-vars` injects `nbft_secret`, 5)
- Credential issuance & push driver (nvmet nodes issue a key automatically on disk creation; pushes to Agents on credential set/revoke, device bind/unbind/rebind, disk delete, Worker deletion; the Agent relays to the nvmet host service to sync the hosts matrix and the TLS PSK in the kernel `.nvme` keyring) + push reconciliation (state ledger + periodic idempotent replay of undelivered items, visible via the push projection, see 7.7)
- Data-plane transport encryption by default: NVMe/TCP TLS 1.3 on the storage node's single data-plane port (`4421`), shared by the firmware and kernel boot segments; the PSK is derived from the authentication key and delivered via `/boot-vars` as `nbft_tls_psk` / `nbft_tls_port`, and a missing port or key refuses the boot instead of falling back to plaintext

The current version does **not** yet include:

- Automatic IP management
- Automatic master image lifecycle management
- Scheduled reconciliation
- Data disk attachment (the `/luns/data` namespace is reserved)
- Real transactions for the file-based storage: rollback = rewrite; if a rebind's old-binding cleanup fails **and** the restore also fails, locate and fix manually via the audit trail (16.7)

---

### Component Ports

#### Control-plane host
- dnsmasq: `53` (default DNS listener), `67` (DHCP), `69` (TFTP)
- nginx: `80` (301 redirect to 443 only), `443` (the only external entry: WebUI / management API / boot chain / file downloads)
- Control Plane: no TCP port (in-container unix socket `/run/kurrent/cp.sock`, reachable only by nginx and the container health check)

#### Storage node
- Agent: `4840` (mTLS, called by the control plane)
- nvmet-host: `4841` (mTLS, called by the Agent via `host.docker.internal:4841`)
- NVMe-oF data plane: `4421` (nvmet port, TLS 1.3 `tsas` required; authentication via DH-HMAC-CHAP)
- LIO / stgt (iSCSI data plane): `3260`