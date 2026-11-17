---
title: "Basics and State"
---

# Basics and State

The base information, runtime environment, state files and health endpoint needed to call the API are listed below.

## Base URL

Management entry point (the only one): `https://<host>/api/cp` (nginx 443; port 80 only redirects with 301). Boot-chain endpoints are additionally served on 4443, as described below.

The management plane listens on no TCP port at all: nginx forwards over a unix socket
(`/run/kurrent/cp.sock`) on a shared volume, and the container health check uses the same
socket. For local debugging, go through the 443 entry point or enter the control-plane
container to reach that socket.

> The entry point uses a self-signed server certificate (`control-plane/state/certs/server.crt`
> under the state root, generated automatically at startup; the state root defaults to
> `~/.local/state/kurrent` and `kurrent deployment show` prints it), so curl needs `-k`, or
> `--cacert <state>/control-plane/state/certs/server.crt` to pin that exact certificate.

Replace with the actual address if you expose it on a different port or domain.

## Environment Variables

The control plane does **not** parse a `.env` file; it reads container environment
variables via `os.getenv(...)`. The root `docker-compose.yml` injects
`TZ`, `KURRENT_HTTPS_PORT` and `KURRENT_BOOT_HTTPS_PORT`; add any extra switch you need (for example the development-mode
`KURRENT_CP_INSECURE=1`, local only, never in production) to that service's
`environment:` block, or pass it with `docker compose --env-file`.

## State and Configuration Files

The current Control Plane state and configuration are divided as follows (container paths; the host counterparts are `control-plane/{state,config,dnsmasq,bootstrap}/` under the state root, which defaults to `~/.local/state/kurrent` and can be overridden with `KURRENT_STATE_DIR`):

| File | Meaning |
|---|---|
| `config/agents.yml` | Agent node inventory and scheduling roles |
| `state/devices.yml` | Device ledger (pool / bindings / fingerprint / TPM AK and PCR baseline) — the **single authority for bindings** (`bound_worker_id`) |
| `state/workers.yml` | Worker ledger (compute identity / system disks / default boot) |
| `state/credentials.yml` | NVMe-oF key store (DHHC-1, key follows the Worker) |
| `state/credential-push.yml` | Credential push-state ledger (per-Agent delivery results, for reconciliation and projection, see [System disks](./disks.md)) |
| `state/masters.yml` | Master tag ledger (os / os_version annotations, see [Agents and storage](./agents.md)) |
| `state/settings.json` | Runtime settings (registration window, enforcement switch) |
| `state/users.yml` | Management-plane user→group map (CN → groups; admin entry bootstrapped on first start, then groups resolve by cert CN) |
| `state/sessions.yml` | Server-side session registry (key = `sha256(jti)`; deleting an entry invalidates it, `epoch` invalidates all) |
| `state/audit.jsonl` | Authentication/authorization/credential audit and alarms (see [Authentication](./auth.md); separate from `operations.jsonl`) |
| `state/pki/ca/{users,nodes,control}/` | Three separate root CAs (users=humans, nodes=node components, control=control-plane clients) |
| `state/pki/users/issued.yml` | users-domain certificate registry (by serial; includes revocation time and reason) |
| `state/pki/components/issued.yml` | nodes-domain component certificate registry (agent / nvmet-host, written at issuance; includes revocation time and reason) |
| `state/pki/control/issued.yml` | control-domain certificate registry (control-plane clients, written at issuance; includes revocation time and reason) |
| `state/pki/nginx/ca-bundle.pem` | nginx trust bundle (users + nodes public certs only; no private keys) |
| `state/pki/nginx/clients.crl` | nginx client-certificate CRL (users + nodes tables concatenated) |
| `state/pki/nginx/nodes.crl` / `control.crl` | Component-face CRLs (signed by the nodes / control CA; distributed via `/enroll/crl`, `nextUpdate` 30 days) |
| `state/operations.jsonl` | Control Plane operation audit trail (with actor) |
| `dnsmasq/dhcp-hosts.conf` | `MAC -> hostname` bindings (**derived view**; the device ledger is authoritative) |
| `bootstrap/` | One-shot management credential exchange for first deployment (`request` → `ready` → `consumed`; deleted after consumption) |

Notes:

- The deployment record `~/.kurrent/deployment.yaml` (0600) pins the project names, repository root and state root; it lives outside the container and is read-only through `kurrent deployment show`;
- `workers.yml` does **not** store MAC addresses — MAC addresses and bindings live in the device ledger, and the Worker side only holds a projection.
- The Compose file mounts the whole `dnsmasq/` directory into the control-plane container (which also writes `dnsmasq.conf` there).
- `dhcp-hosts.conf` is written by **truncating in place** (preserving the inode) rather than via an atomic replace: the file is bind-mounted into the dnsmasq container at file level, so replacing the inode would leave the container reading the old file.
- `dnsmasq/dhcp-hosts.conf` contains one binding per line, with a fixed format:

```text
00:0c:29:b9:8b:2d,worker-01
```

This file only serves DHCP hostname/address assignment and does not participate in
device↔Worker ownership decisions: ownership always follows the device ledger. The control
plane writes this projection on bind/rebind (and repairs it when missing), and dnsmasq
never writes back to the ledger. A missing or stale projection only means the derived
artifact is out of sync; it does not change binding authority.

---

## GET /healthz

### Description

Health check endpoint. Does not change any state and requires no authentication. When a startup-derived artifact (`dnsmasq.conf`, server certificate, component PKI) fails it returns `503 {"status":"degraded"}` — `kurrent init` uses this to roll back the declaration; error details are only returned by the authenticated `GET /config`.

### curl

```bash
curl -s "$BASE_URL/healthz"
```

### Successful Response

```json
{"status":"ok"}
```

---

## GET /config (declaration status)

Returns the effective declaration loaded by the running process, its canonical `configHash`, and derived-artifact consistency (requires management-plane auth: `human` or `automation`). It answers "which declaration is running, and do derived artifacts match it"; it never returns private keys or secrets.

| Field | Description |
|---|---|
| `apiVersion` / `kind` / `metadata.name` | Declaration identity |
| `spec` | Effective config (camelCase aliases, defaults injected; identical to `kurrent config show`) |
| `configHash` | Canonical SHA-256 of the effective config (sorted compact JSON; comparable with the hash printed by `kurrent config validate`) |
| `derived.dnsmasqConf` | `path` and `matchesDeclaration` (whether the on-disk dnsmasq.conf equals the rendering of the current declaration) |
| `derived.serverCert` | `present` / `fingerprint` / `san` / `days` / `notAfter` / `drift` (`san` / `days` / `expired` / `missing`) / `rotateOnChange`; drift is reported by default and only rotated when `rotateOnChange=true` |
| `startup.errors` / `startup.warnings` | Startup-derived failures/warnings (non-empty `errors` makes `/healthz` return 503) |

```bash
kurrent config status     # CLI wrapper (443 mTLS / Bearer)
curl -s --cert "$CERT" --key "$KEY" "$BASE_URL/config"
```

---
