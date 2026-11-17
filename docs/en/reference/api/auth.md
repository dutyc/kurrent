---
title: "Authentication and Authorization"
---

# Authentication and Authorization

The authentication chain, principal classes, authorization rules, and audit alarms are described below. The management plane is served on 443 only; boot-chain endpoints and the public CRL require no authentication.

## Authentication (authenticator chain)

Management access goes through the nginx 443 entry point (the only management entry); the
control-plane management plane listens on a unix socket only and exposes no TCP port.
Except `GET /healthz`, the public CRL (`/crl/nodes.crl`) and the iPXE boot-chain endpoints
(`/boot-vars`, `/devices/report`, `/devices/challenge`), management endpoints require authentication. Three credential types
are tried in order (serial short-circuit: first success wins; an invalid credential returns
`401` without falling through):

| Credential | Carrier | Notes |
|---|---|---|
| mTLS client certificate | 443 mutual TLS | admin certificate issued and installed by `kurrent init` (CN=admin, no O); the CLI resolves credentials in the order `--credentials` > `KURRENT_CREDENTIALS` > the deployment record's `<state>/control-plane/state/admin.conf` > legacy discovery > `~/.kurrent/admin.conf` (`kurrent init` installs that snapshot through the one-shot exchange and verifies it with mTLS). nginx validates the chain (users CA + nodes CA bundle) and, only on `SUCCESS`, injects DN / issuer / serial / fingerprint / class; the control plane resolves groups from `state/users.yml` by CN. Identity headers are empty by default on every other proxied location |
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
audit write fails, the request fails with `503` (fail-closed). Alarms are written as
`event=alarm` entries in the same file and cover: boot credential `injected && !verified`,
boot endpoint rate limiting, component/control on a management route, unknown issuer /
CRL hit (nginx rejects a CRL hit at the handshake layer; the app-side alarm covers
unknown-issuer evidence that reaches it), an authentication-failure burst from one
source, bootstrap identity/CSR CN mismatch, user/admin certificate issuance / CRL update
/ CRL refresh failure / CA generation, `users.yml` change, and audit write failure.

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
