# Identity and Access

Kurrent has two orthogonal identity chains:

- **Management identity**: how humans, automation and components prove themselves when calling management APIs.
- **Device identity**: how a machine proves itself on the boot chain when requesting boot credentials.

The two chains use different trust roots and cannot substitute for each other. A valid device key grants no management access; an administrator certificate cannot impersonate a device.

## Device Identity

### Key

On first boot, firmware generates an ECDSA P-256 private key and stores it in EFI NVRAM. The key never leaves the machine, and later boots refuse to overwrite it.

Firmware provides three commands:

| Command | Purpose |
| --- | --- |
| `keygen` | Generate the device key; refuses to overwrite an existing one |
| `pubkey` | Export the public key (uncompressed point, hex) |
| `sign <data>` | SHA-256 + ECDSA signature over the data, output as base64 DER |

### Registration

The device reports its fingerprint and public key to `/devices/report`. The control plane records it by MAC, but **registration grants no privilege**:

- only an unknown device arriving while the registration window is open, with a valid public key, is admitted;
- a pooled but unbound device has no boot credentials;
- after the window closes, new devices are not accepted.

### Challenge-Response

`/devices/challenge` issues a one-shot nonce, and the device signs `nonce‖mac` with its private key. The result is used during `/boot-vars` admission. The nonce is single-use and bound to the MAC; related endpoints are rate-limited per source IP.

The key can be rotated: the old key signs `rotate‖mac‖new_pubkey‖nonce`, and is invalidated on success.

### Limits of Device Identity

- Challenge-response proves "the caller holds this device key", not "the code running with this key is trustworthy".
- TOFU pins the server certificate; it does not protect the first DHCP/TFTP hop.
- The device key lives in NVRAM, so an operating system that can read firmware variables after boot can also use it. Mainline does not yet provide TPM or Secure Boot measurement to close this path.

These boundaries are expanded in the [Security Model](../architecture/security.md).

## Management Identity

### Single Entry

The management plane is reachable only through HTTPS `443`. The control-plane service itself has no TCP listener; it listens on an in-container unix socket, and nginx is the only peer. `X-Client-Cert-*` and `X-Client-Class` headers are trusted only when injected by nginx; other proxied locations clear them by default.

### Three CAs

| CA | Issues | Purpose |
| --- | --- | --- |
| users | Human users and admin | Management mTLS; nginx classifies it as `human` |
| nodes | agent / nvmet-host | Component enroll / renew and component mTLS; classified as `component` |
| control | The control plane's own client | Control-plane outbound connections to components; not exposed on 443 |

nginx trusts only the users + nodes bundle and derives the principal class from the issuer. Every control-plane route declares its accepted classes, **denying by default**:

- component certificates cannot reach management routes;
- human certificates cannot call component renewal endpoints;
- control-domain certificates do not enter 443.

### Three Credential Types

| Credential | Carrier | Lifetime and storage |
| --- | --- | --- |
| mTLS client certificate | 443 mutual TLS | User certificates default to 365 days, capped by declaration; the private key stays with the user |
| Management token | `Authorization: Bearer` | Default 24 hours, capped by declaration; only a sha256 digest is stored |
| Session | `__Host-kurrent_session` cookie | Default 12 hours; the cookie carries only `{jti, exp, epoch}`, identity comes from the server ledger |

The authenticator chain tries certificate → token → session in order and stops at the first success. An invalid credential returns 401; there is no downgrade.

### User Certificates

An administrator or user generates the private key and CSR locally:

```bash
kurrent cert keygen alice --out ./certs
kurrent cert issue alice --csr ./certs/alice.csr
```

The control plane receives only the CSR and never sees the private key. The CN must already exist in `state/users.yml`; groups also come from that file.

Revocation can target one certificate (by serial) or all certificates for a user. The CRL is re-signed and nginx reloaded, so the next TLS handshake is rejected. Revocation also removes that user's sessions.

### Management Tokens

```bash
kurrent token create --usage admin [--scope admin,readonly,enroll] [--ttl 24h]
```

- Scope is an upper bound: `enroll` can only issue bootstrap tokens, cannot issue admin tokens, and cannot read management APIs.
- `last_used_at` is written back with throttling, making unused credentials visible.
- Revocation deletes the ledger entry; Bearer verification has no cache.

### Sessions

After exchanging a certificate or token for a session, the client carries only the cookie:

- the server records `jti`, user, groups, certificate fingerprint and scopes;
- every request rechecks that the user still exists and groups are current;
- deleting the entry or incrementing the epoch invalidates sessions immediately;
- write requests require `Origin` and `Host` to match.

### Authorization

- Reads require authentication only.
- Writes require the `admin` group.
- The `enroll` scope only permits issuing bootstrap tokens.
- Session-authenticated writes additionally require same-origin.

## Component Identity

The storage node's Agent and nvmet-host use certificates issued by the nodes CA. On first join:

1. an administrator issues a bootstrap token bound to `agent_id`;
2. the node calls `/enroll` with the token and receives a component certificate;
3. the token is single-use by default; TTL and use count are capped by declaration;
4. the nvmet-host credential is derived by the agent from the enroll response and is not issued separately.

In steady state:

- the agent renews before expiry according to the configured threshold;
- components periodically fetch the nodes CRL and fail closed when it is missing or expired;
- the control plane checks the peer serial on every outbound connection;
- a revoked component is rejected on the next handshake or application check, and its bootstrap tokens are deleted.

Recovering a revoked node: issue a new bootstrap token and rerun `kurrent join`. The agent startup self-check discovers the local certificate in the public CRL and re-enrolls with the new token.

## Audit and Alarms

Every management request appends one `state/audit.jsonl` record with principal class, principal, issuer, serial, fingerprint, route, decision, reason and request_id. Secrets and token plaintext are never recorded.

If the write fails, the request returns 503 (fail-closed). Eight event classes are written as `event=alarm`:

- boot credentials injected but not signature-verified;
- component or control principals on a management route;
- unknown issuer / CRL hit;
- a burst of authentication failures from the same source;
- bootstrap identity or CN mismatch;
- user/admin certificate issuance, CRL update, CA generation;
- `users.yml` changes;
- audit write failure.

With `KURRENT_CP_ALARM_WEBHOOK` set, alarm events are POSTed as JSON by a background queue; delivery stays outside the request path.

## Credential Ownership

| Credential | Owner | Rotation / invalidation |
| --- | --- | --- |
| Device private key | Device NVRAM | `POST /devices/rotate` |
| User certificate | User private key + users CA | CSR reissue / serial revocation |
| Management token | Control-plane ledger (digest) | TTL / revocation |
| Session | Session ledger | Logout / per-session revoke / sign-out all |
| Component certificate | Local on Agent / nvmet-host | Renew / nodes CRL |
| DHHC-1 | Control-plane key store per Worker | Rotation / revocation / reconciliation |
| TLS PSK | Derived value | Changes atomically with DHHC-1 |

## Related Pages

- How device identity is used at boot: [Boot Chain](./boot-chain.md)
- Credentials and the data plane: [Storage and Data Plane](./storage.md)
- Threat model and residual risk: [Security Model](../architecture/security.md)
- Day-to-day commands: [Operations](../guide/operations.md)
