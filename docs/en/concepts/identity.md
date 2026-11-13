# Identity and Access

Kurrent has two orthogonal identity chains:

- **Management identity**: how humans, automation and components prove themselves when calling management APIs.
- **Device identity**: how a machine proves itself on the boot chain when requesting boot credentials.

The two chains use different trust roots and cannot substitute for each other. A valid device key grants no management access; an administrator certificate cannot impersonate a device.

## Device Identity

### Trust Root

Device identity is a TPM 2.0 attestation key (AK). On first boot the firmware creates a restricted signing key inside the local TPM (ECC P-256 or RSA-2048); the private key is non-exportable. Later boots reuse the same AK, and an existing AK is never recreated. The control plane records only the AK public key (SPKI DER), the AK name and the key type.

The firmware provides three capabilities:

| Capability | Purpose |
| --- | --- |
| `akgen` | Create or reuse the AK inside the TPM (restricted signing, ECC P-256 or RSA-2048); an existing AK is never overwritten |
| `akpub` | Export the AK public key (SPKI DER), TPM2B_NAME and algorithm type for registration |
| `quote <nonce>` | Read PCR 0/2/4/7 (SHA-256) and produce a quote in one TPM call; `extraData` is the ASCII bytes of the nonce string; outputs raw TPMS_ATTEST bytes (without the TPM2B length prefix) and the AK signature |

### Registration

The device reports its fingerprint, AK public key/name/type and Secure Boot state to `/devices/report`. The control plane records it by MAC, but **registration grants no privilege**:

- only an unknown device arriving while the registration window is open, within the admission quota, carrying a valid AK and reporting `sb=1`, is admitted;
- a pooled but unbound device has no boot credentials;
- after the window closes, new devices are not accepted;
- devices without a TPM, or with Secure Boot off, are not admitted.

### Challenge-Attest

`/devices/challenge` issues a one-shot nonce, and the device produces a quote over PCR 0/2/4/7 with its TPM AK, placing the nonce string's ASCII bytes in `extraData`. `/boot-vars` validates the quote structure, the AK signature, the nonce binding and single use, and finally the PCR digest against the platform baseline.

The baseline is recorded the first time attestation passes (TOFU). A later digest change is denied as `pcr_mismatch` and kept as a pending digest until an administrator reviews it and approves it with `POST /devices/{mac}/approve-baseline`. The nonce is single-use and bound to the MAC; related endpoints are rate-limited per source IP.

The AK cannot be rotated online: device recovery or a mainboard swap goes through the administrator flow of deleting the inventory entry and re-enrolling during a registration window.

### Limits of Device Identity

- Attestation proves "the caller holds this device's AK and can produce a valid quote over the selected PCRs", not that the running system is uncompromised; a compromised runtime can still obtain the current PSK within the rotation window.
- TOFU pins the server certificate; it does not protect the first DHCP/TFTP hop.
- The PCR baseline is TOFU as well: if the first attestation happens under attacker control, the baseline is contaminated; the initial enrollment should be verified against the installed environment.
- Firmware behavior (PCR selection, AK creation) needs per-model verification; AMD fTPM and discrete TPMs behave differently.

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

1. an administrator issues a bootstrap token bound to `agent_id` (12 lowercase hex characters, allocated by the control plane at issuance and written by join to `storager/state/agent.id` under the node state root; the CLI and WebUI only consume the returned identity; nicknames belong to the control-plane ledger);
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

- **Device identity** — TPM AK (non-exportable private key); AK claim during the registration window and administrator baseline approval.
- **User certificate** — User private key + users CA; CSR reissue / serial revocation.
- **Management token** — Control-plane ledger (digest); TTL / revocation.
- **Session** — Session ledger; Logout / per-session revoke / sign-out all.
- **Component certificate** — Local on Agent / nvmet-host; Renew / nodes CRL.
- **DHHC-1** — Control-plane key store per Worker; Rotation / revocation / reconciliation.
- **TLS PSK** — Derived value; Changes atomically with DHHC-1.

## Related Pages

- How device identity is used at boot: [Boot Chain](./boot-chain.md)
- Firmware-side keys, signatures and TOFU: [Firmware Contract](../reference/firmware-contract.md)
- Credentials and the data plane: [Storage and Data Plane](./storage.md)
- Threat model and residual risk: [Security Model](../architecture/security.md)
- Day-to-day commands: [Operations](../guide/operations.md)
