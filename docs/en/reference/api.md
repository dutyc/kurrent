# API Reference

Every control-plane capability is exposed through the REST API. The WebUI, the CLI, and third-party automation are all clients of the same API and never bypass the control plane to operate directly on Agents or the data plane.

Complete endpoints, request fields, response structures, and runnable `curl` examples are authoritative in the repository's bilingual documentation:

- Chinese: `api/control-plane-api.zh-CN.md`
- English: `api/control-plane-api.en.md`

## Entry point

| Item | Value |
| --- | --- |
| Base URL | `https://<host>/api/cp` |
| External entry | nginx 443; port 80 only redirects with 301 |
| Internal transport | nginx forwards over a shared-volume unix socket; the control plane listens on no management TCP port |
| Certificate | TOFU self-signed; pin with `--cacert control_plane/state/certs/server.crt`, or use `-k` |
| OpenAPI | Off by default; set `KURRENT_CP_OPENAPI=1` locally when needed |

## Authentication

Except `/healthz` and boot-chain endpoints, management endpoints require authentication. Three credential types are tried in order, and the first success short-circuits; an invalid credential returns 401 without falling through.

| Credential | Carrier | Notes |
| --- | --- | --- |
| mTLS client certificate | 443 mutual TLS | Admin certificate issued during setup; nginx validates the chain and injects identity headers |
| Management token | `Authorization: Bearer <token>` | Issued by `kurrent token create --usage admin`; only a digest is stored |
| Session cookie | `__Host-kurrent_session` | Exchanged via `POST /session` with a certificate or token; identity comes from the server-side session ledger |

Principal classes:

| Class | Source | Capability |
| --- | --- | --- |
| `human` | User certificates from the users CA | Management and queries |
| `component` | agent / nvmet-host certificates from the nodes CA | enroll / renew and component endpoints |
| `automation` | Management token | Narrowed by scope |
| `anonymous` | No credential | Boot chain and health check only |

Authorization rules:

- Reads (GET) only require authentication;
- writes (POST / PUT / PATCH / DELETE) require the admin group;
- cross-class access is denied by default with 403;
- an automation token with the `enroll` scope can only issue bootstrap tokens, cannot read ledgers, and cannot exchange for a session;
- cookie-authenticated writes require `Origin` to match `Host`;
- boot-chain endpoints are anonymous but constrained by binding, device signature verification, and source-IP rate limits.

## Endpoint groups

| Group | Representative endpoints | Purpose |
| --- | --- | --- |
| Health and declaration | `GET /healthz`, `GET /config` | Liveness, startup-derived self-checks, effective declaration and configHash |
| Boot chain | `GET /boot-assets`, `GET /boot-vars`, `GET /devices/report`, `GET /devices/challenge`, `POST /devices/rotate` | Firmware boot variables, device reporting, challenge-response, key rotation |
| Management identity | `POST/DELETE /session`, `GET /sessions`, `GET /whoami` | Session sign-in, sign-out, ledger, current identity |
| Devices | `GET /devices`, `POST /devices`, `DELETE /devices/{mac}`, `POST/DELETE /devices/{mac}/bind` | Device pool, registration, revocation, binding and unbinding |
| Workers | `POST /workers`, `GET /workers`, `GET /workers/{id}/status`, `DELETE /workers/{id}` | Worker ledger, status, batch deletion |
| System disks | `POST /workers/{id}/luns/disk`, `POST /workers/luns/disk/batch`, `DELETE /workers/{id}/luns/disk/{os_tag}`, `PUT /workers/{id}/default-disk` | Create, batch-create, delete, and default-disk selection |
| Credentials | `GET/PUT/DELETE /workers/{id}/credential` | View, rotate, revoke NVMe-oF authentication keys |
| Agents | `GET /agents`, `POST /agents`, `POST /agents/probe`, `PUT /agents/{id}`, `DELETE /agents/{id}` | Storage-node ledger and probing |
| LUNs and masters | `GET /agents/{id}/luns`, `GET /masters`, `POST /agents/{id}/luns/disk`, `PUT /agents/{id}/masters/{name}/tag` | Master tags and LUN creation/deletion |
| PKI | `POST /pki/tokens`, `GET /pki/tokens`, `GET /pki/users`, `GET /pki/components`, `GET /pki/control`, `GET /enroll/crl` | Tokens, certificate ledgers, CRL distribution |
| Settings | `GET/POST/DELETE /settings/registration-window`, `GET/PUT /settings/enforcement` | Registration window and signature enforcement |
| Operations | `GET /operations` | Operation audit log |

See the repository API docs for complete parameters and response fields.

## Error semantics

| Status | Meaning |
| --- | --- |
| `400` | Request or business validation failure |
| `401` | Missing, invalid, or expired credential |
| `403` | Insufficient principal class or permission; cross-class access |
| `404` | Object not found |
| `409` | State conflict: already exists, already bound, node draining, etc. |
| `429` | Rate limited; the response carries `Retry-After` |
| `503` | Audit write failure, unreachable Agent, or failed startup-derived self-check |

Bodies are usually `{"detail": "..."}`. Every response carries `X-Request-Id`; audit records use the same `request_id`.

Audit is fail-closed: if the audit event for a request cannot be written, the request itself returns 503. Audit never records keys, token plaintext, private keys, or PSKs.

Rate limits use a per-source-IP sliding window:

| Endpoint | Default threshold |
| --- | --- |
| `/boot-vars` | 240 requests/minute |
| `/devices/report` | 120 requests/minute |
| `/devices/challenge` | 120 requests/minute |
| `/devices/rotate` | 10 requests/minute |

Thresholds can be adjusted with `KURRENT_CP_RATE_LIMIT_*`; `<= 0` disables them. Rate limiting suppresses scanning; it is not an identity boundary.

## Boot-chain endpoints

| Endpoint | Auth | Key constraint |
| --- | --- | --- |
| `/boot-vars` | anonymous | Binding check plus optional device signature verification; unverified requests get no keys; no default disk returns reboot |
| `/devices/report` | anonymous | Registration only within the window, within quota, and with a valid public key; revoked devices stay revoked |
| `/devices/challenge` | anonymous | Unknown, unclaimed, or revoked devices return 404; the nonce is single-use and bound to the MAC |
| `/devices/rotate` | anonymous | Old key signs `rotate‖mac‖new_pubkey‖nonce`; nonce is single-use |

Field-level contracts are in the [Firmware Contract](./firmware-contract.md).

## State and configuration files

| File | Meaning |
| --- | --- |
| `config/agents.yml` | Agent ledger and scheduling roles |
| `state/devices.yml` | Device ledger; authority for bindings |
| `state/workers.yml` | Worker ledger and system disks |
| `state/credentials.yml` | NVMe-oF key store |
| `state/credential-push.yml` | Credential push state |
| `state/masters.yml` | Master tags |
| `state/settings.json` | Runtime settings |
| `state/users.yml` | Management users and groups |
| `state/sessions.yml` | Server-side session ledger |
| `state/audit.jsonl` | Authentication, authorization, credential audit and alarms |
| `state/pki/` | Three root CAs, issuance ledgers, and CRLs |
| `state/operations.jsonl` | Operation audit |
| `dnsmasq/dhcp-hosts.conf` | MAC → hostname projection; the device ledger is authoritative |

`dhcp-hosts.conf` is truncated in place to preserve its inode; it is only a derived projection of binding operations and does not participate in boot identity resolution.

## Related pages

- [Firmware Contract](./firmware-contract.md)
- [Identity and Access](../concepts/identity.md)
- [Declarative Configuration](../concepts/configuration.md)
- [Security Model](../architecture/security.md)
- [CLI Reference](./cli.md)
