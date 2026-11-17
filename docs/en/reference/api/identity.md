---
title: "Identity and PKI"
---

# Identity and PKI

Management identities are issued by three CA roots. The endpoints below cover one-command join, tokens, sessions, and certificate revocation.

## One-Command Join: node-bound bootstrap token issuance + enroll auto-registration

### Description

The control plane issues a **node-bound bootstrap token** (`<6-hex>.<16-hex>`, only the sha256 hash is stored, TTL defaults to **60 minutes**, max 24 hours): issuance may specify `agent_id` (only 12 lowercase hex characters); when omitted the control plane allocates a new identity, and enroll only accepts that identity. Tokens are **single-use by default**: an allowance is reserved atomically before issuance and released if issuance fails; bulk onboarding uses `max_uses` (`--uses N`, 1..1000 and no higher than `spec.pki.bootstrapTokenMaxUses`, default 100) explicitly instead of unlimited reuse. Every issuance is a new token (plaintext unrecoverable); **revocation is supported** (`DELETE /pki/tokens/{token_id}`, deletion takes effect immediately); unrevoked tokens expire naturally.

The node identity is a machine identity and accepts only 12 lowercase hex characters. The control plane allocates it when a bootstrap token is issued (a new identity when `agent_id` is omitted, or the supplied existing identity for recovery) and returns it in the response and the printed join command; `kurrent token create` and the WebUI only call this endpoint and consume the returned `agent_id`. Join writes it to `storager/state/agent.id` under the node state root, which is not part of the node declaration. Nicknames belong to the control-plane ledger.

The `nvmet-host` component credential is **not issued here**: when an agent enrolls with `backend=nvmet`, the control plane derives one (bound to agent_id) and returns it in the enroll/renew response; the agent persists it to `storager/bootstrap/nvmet-host.token` under the node state root for the nvmet-host container (capability-driven derivation — issuance does not foresee the backend).

### POST /pki/tokens

Request body: `{"usage": "bootstrap", "agent_id": "<existing node id, optional>", "component": "agent", "max_uses": 1}`
(bootstrap is the default; omitting `agent_id` makes the control plane allocate a new identity; `component` is `agent` | `nvmet-host`; `max_uses` is 1..1000,
default 1, and no higher than `spec.pki.bootstrapTokenMaxUses`), or
`{"usage": "admin", "scopes": ["readonly"], "ttl_hours": 24}`
(management token for the authenticator chain Bearer path; scope is `admin` | `readonly` |
`enroll`, default `admin`; TTL is 1 hour up to `spec.auth.tokenMaxTtlHours` (default 720),
default 24). Scopes cap authority:
`readonly` is read-only, `enroll` may only issue bootstrap tokens (every other read or
write endpoint and session exchange returns `403`); `readonly` / `enroll` tokens may not
issue admin tokens.

#### curl

```bash
# Node bootstrap token (bound to a node identity)
curl -s -X POST "$BASE_URL/pki/tokens" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "3f2a9c1d8b7e"}'

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
  "agent_id": "3f2a9c1d8b7e",
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
| `400` | an illegal `agent_id`, an out-of-range quota/TTL, or an invalid scope |
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

### enroll auto-registration (POST /enroll, nginx entry /api/cp/enroll)

First-report auto-registration: when an `agent` component enrolls (`POST /enroll`, Bearer = bootstrap token bound to that node identity) and the id is absent from `agents.yml`, the control plane **auto-creates the entry** (role_disk=True, role_cd=False, enabled=True, tags=`("auto", "storage")`; `("auto",)` only for legacy agents that report no capabilities — the **backend type is not written into tags**, it is resolved by `/boot-vars` querying `/capabilities` in real time) and persists the request’s `base_url` field (agent side `spec.agent.advertiseUrl`, control-plane-reachable address) into the registry; the nickname is not written during bootstrap and is set by an administrator in the WebUI or via `PUT /agents/{agent_id}` after registration. The response carries two certificates (client / serving, issued by the nodes CA, subject = CN only) and **two trust anchors**: `ca_nodes_crt` (validate peer components) and `ca_control_crt` (validate control-plane clients). Rules:

- Token validation: format / registry entry / component match / binding match (`agent_id` equals the ledger entry) / not expired; on success an allowance is **reserved atomically** and released if certificate issuance fails; single-use by default (`max_uses` can raise it)
- `/enroll/renew` uses the component’s existing client cert (nginx forces mTLS on that path; the issuer must be the nodes CA → class=component; human certs get `403`), and re-signs only when CN == `<prefix>-<agent_id>`
- The response includes `renew_threshold` (`spec.pki.renewThreshold`, default 0.2): components persist it as `policy.json` and renew when the certificate's remaining lifetime drops below it; legacy components ignore the field (keep the built-in default)
- `nvmet-host` components are **not auto-registered** (they share the agent_id; the agent component must be registered first — otherwise 400, container restart retries; the allowance is not consumed)
- A non-empty `base_url` must start with `http://` / `https://`, otherwise 400 (no auto-registration)
- When an already-registered agent enrolls/renews again, `base_url` / `role.cd` are **refreshed from the node declaration** (declaration authority: the node’s file wins — after a node move, the next renewal aligns the registry, no reinstall needed); control-plane attributes `enabled` / `storager_ip` / `tags` / `remark` are **left untouched**, and fields the node does not report keep their current values
- An agent reporting `backend=nvmet` gets an extra `nvmet_token` field in the response (derived nvmet-host credential); stgt/lio backends and legacy agents have no such field
- The response also carries `nodes_crl` / `control_crl` (PEM) and `next_update`, which components persist for inbound/outbound revocation checks; if CRL generation fails these fields are omitted (best effort) and the component fetches them via `/enroll/crl` after startup

### One-command join (node side)

Declarative workflow (declaration-driven config, CLI as tooling): the control plane issues the token and prints a **join command carrying the control-plane URL and the node id**; pasting it on the storage node yields both the address and the identity — the node declaration `storager/kurrent.yaml` is auto-generated from defaults when missing (the address synced into `spec.controlPlane.url`) or read in and merged when present, and the identity is written to `storager/state/agent.id` under the node state root (the first run must provide `--agent-id`; reruns must match the state file), then the agent is converged-started (idempotent, re-runnable; use `--dir` to point at the repository root or the `storager` directory from outside, and `--cp-ca` to supply the controller trust anchor on split hosts):

```bash
# Control plane (issue a node-bound bootstrap token; --cp-url = control-plane HTTPS entry reachable
# from the storage node, derived from --server otherwise; omitting --agent-id makes the control plane
# allocate a new 12-lowercase-hex identity; an explicit id binds an existing identity for recovery):
kurrent token create --cp-url https://<cp-host>

# Storage node (paste the join command printed above; the node id was allocated by the control plane and is already included; business keys can be pre-declared via kurrent config print node-defaults):
kurrent join https://<cp-host> --token a1b2c3.0123456789abcdef --agent-id <agent_id-from-response>
```

### GET /enroll/crl (component CRL distribution)

Components (agent / nvmet-host) fetch the revocation tables with their existing client
cert. The entry `/api/cp/enroll/crl` forces mTLS and admits only `class=component`
(human certificates get `403`, no certificate gets `401`):

```json
{
  "nodes_crl": "-----BEGIN X509 CRL-----\n...",
  "control_crl": "-----BEGIN X509 CRL-----\n...",
  "next_update": "2026-11-23T09:00:00+00:00"
}
```

- `nodes_crl` is signed by the nodes CA: components verify **peers** (agent ↔ nvmet-host)
- `control_crl` is signed by the control CA: components verify the **control-plane client** (4840 inbound)
- Control-plane outbound (agent_client) and agent → nvmet-host outbound also load the nodes CRL and check freshness on every connection; a missing or stale (`nextUpdate` passed) CRL **refuses the connection** (fail-closed, not a degraded allow)
- Components refresh every 60 seconds by default (`KURRENT_COMPONENT_CRL_REFRESH_SECONDS`; `<=0` disables); a failed fetch keeps the previous snapshot, which is refused once stale
- Revocation takes effect at the component's next refresh after the control plane writes the CRL (≤60 s by default), with no component restart

Public read-only copy (startup self-check): `GET /crl/nodes.crl` is served directly by
nginx from `state/pki/nginx/nodes.crl`, unauthenticated, `GET`/`HEAD` only,
`Cache-Control: no-store`. A revoked certificate cannot pass the `/enroll/crl` mTLS
handshake, so components need an entry point that does not require a client certificate:
when the serial of the local `client.crt` / `serving.crt` appears in this table, the
component re-bootstraps. Components accept the CRL only after verifying its signature,
issuer and `nextUpdate` against the local `ca-nodes.crt`; a failed fetch or validation
does not change the existing chain checks (the self-check is additive). Set
`KURRENT_PKI_SELF_CHECK=0` to disable the startup self-check.

## Management sessions and certificate revocation

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
    "groups": ["admin"],
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

`groups` comes from `state/users.yml` (the certificate carries only the CN; a user removed
from the directory returns an empty array) and is provided for the management UI; actual
authorization still re-reads `users.yml` on every request.

`DELETE /pki/users/{cn}?reason=key_compromise` revokes a CN's certificates; add
`&serial=<hex>` to revoke exactly one (a user may hold an old and a new certificate):

- `reason` is `unspecified` (default) | `key_compromise` | `superseded` | `cessation_of_operation`
- Marks `state/pki/users/issued.yml`, re-signs `state/pki/nginx/users.crl` and
  `clients.crl` (users + nodes concatenated), then reloads nginx — the revoked certificate
  fails on its **next TLS handshake**
- Omitting `serial` revokes every unrevoked certificate for the user and deletes all of
  their sessions; with `serial`, only sessions bound to that certificate fingerprint are
  kicked (count returned as `sessions`)
- Cannot revoke the certificate the current request is using (`400`, compared by
  `serial`); other serials of the same principal may be revoked explicitly by `serial`
  (e.g. invalidating the old certificate after rotation). Omitting `serial` for the
  caller's own CN is also refused; no unrevoked certificate → `404`; CRL written but
  reload failed → `500` (revocation is not in effect yet)
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
    "cn": "agent-23456789abcd",
    "agent_id": "23456789abcd",
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

Recovery after revocation (no local certificate deletion):

1. Issue a new node-bound bootstrap token (WebUI "Certificates" → "Storage nodes" →
   "Recover", or `kurrent token create --agent-id <id>`); revocation deleted the old
   tokens, so they cannot be reused
2. Run `kurrent join <cp-url> --token <token> --agent-id <id>` on the node
3. At startup the agent checks the local client / serving serials; a CRL hit makes it
   re-enroll with the new token and overwrite the local certificates (no manual deletion).
   join waits for the agent to write the derived nvmet-host credential, then restarts
   nvmet-host automatically

```bash
curl -s -X DELETE "$BASE_URL/pki/components/node-a?reason=key_compromise" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{ "revoked": "node-a", "serials": ["3f9c1b7e", "7a2d4c11"], "kinds": ["client", "serving"], "reason": "key_compromise" }
```

CLI equivalents are `kurrent cert components [-o json]` and
`kurrent cert revoke-component <agent_id> [--serial HEX] [--reason ...]`.

### control-domain certificates (control-plane clients)

The client certificate the control plane presents when calling an Agent (4840) is signed
by the control CA. `GET /pki/control` lists that domain (the certificate bootstrapped at
control-plane startup is registered automatically):

```json
[
  {
    "cn": "control-plane",
    "serial": "1de29ec7",
    "fingerprint": "3a71...",
    "created_at": "2026-10-24T09:00:00+00:00",
    "expires_at": "2027-01-22T09:00:00+00:00",
    "revoked_at": "",
    "reason": "",
    "expired": false
  }
]
```

`DELETE /pki/control/{cn}?reason=key_compromise` (optionally `&serial=<hex>`) revokes it:

- records the revocation in `state/pki/control/issued.yml` and re-signs
  `state/pki/nginx/control.crl`
- components refuse that serial after their next refresh (≤60 s by default); the CRL's
  `nextUpdate` is 30 days, tolerating a long control-plane outage
- cannot revoke the certificate used by the current request (`400`); `404` when nothing is left to revoke
- control-domain certificates do not participate in the nginx management handshake
  (`ca-bundle.pem` holds only users + nodes); revocation only affects the
  control-plane → Agent component channel

```bash
curl -s -X DELETE "$BASE_URL/pki/control/control-plane?reason=key_compromise" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{ "revoked": "control-plane", "serials": ["1de29ec7"], "reason": "key_compromise" }
```

---
---
