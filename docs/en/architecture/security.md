# Security Model

Kurrent's security boundary is not a single line but four independent trusts:

| Boundary | Protected object | Anchor |
| --- | --- | --- |
| Management plane | Who may call management APIs | users CA certificates / management tokens / server-side sessions |
| Component plane | Which storage-node components may join and renew | nodes CA + bootstrap token bound to a node identity + CRL |
| Device plane | Which machine may obtain boot credentials | Device NVRAM key + registration window + binding + TOFU |
| Data plane | Who may connect and whether content is encrypted | DH-HMAC-CHAP + TLS 1.3 PSK |

They cannot substitute for each other. A compromised management plane does not let a device claim boot credentials; a leaked device key does not grant management access.

## Threat Model

### Assumptions

- Control-plane hosts, storage-node hosts and network equipment are managed by a trusted operator; host root compromise is treated as loss of the corresponding boundary.
- The PXE network is a dedicated trust boundary, and the deployment provides Layer-2 protection.
- Physical access to devices is controlled; an attacker who can reset NVRAM is outside this model.
- Operators use management credentials with least privilege; backups and key storage are protected by the deployment.

### Goals

- Unauthorized principals cannot read ledgers, change bindings, issue credentials or perform writes through the management plane.
- An unclaimed, unbound or unverified device cannot obtain another Worker's boot variables.
- Revoked components and users cannot continue over mTLS.
- NVMe/TCP data-plane content is encrypted in transit; missing credentials refuse the boot instead of falling back to plaintext.

## Management Plane

### Entry Convergence

- The only external entry is nginx `443`; the control-plane service has no TCP listener and listens on an in-container unix socket.
- Identity headers are trusted only when injected by nginx; other proxied locations clear them. Opening a second entry or allowing external header injection would bypass management authentication.
- The control-plane container does not mount `docker.sock`.

### Principal Classes

nginx derives the principal class from the certificate issuer:

- users CA → `human`;
- nodes CA → `component`;
- the control domain does not enter 443.

Every route declares its accepted classes; a route without a declaration fails at startup. Cross-class access returns 403: component certificates cannot enter the management plane, and human certificates cannot call component renewal.

### Credentials

| Credential | Protection |
| --- | --- |
| User certificate | The private key is generated and kept locally; the control plane only signs CSRs; revocation by serial |
| Management token | Only a digest is stored; scopes narrow permissions; TTL and revocation |
| Session | Cookie carries only jti/exp/epoch; identity comes from the server ledger; per-session and sign-out-all revocation |

Reads require authentication; writes require the `admin` group; session-authenticated writes additionally require same-origin.

### Audit

Every management request writes an audit record; if the write fails the request returns 503 (fail-closed), so no write proceeds without audit. Alarms cover credentials injected without verification, cross-class access, unknown issuer/CRL hits, authentication-failure bursts, bootstrap identity mismatch, certificate and CRL changes, user-file changes and audit failure.

## Component Plane

- Component certificates are issued by the nodes CA; first join requires a bootstrap token bound to `agent_id`, single-use by default.
- Components renew before expiry; the control plane checks the peer serial against the CRL.
- Components periodically fetch the nodes CRL; a missing or expired CRL fails closed instead of allowing traffic.
- Revoking a component also deletes its bootstrap tokens, closing the window where a certificate could be reissued immediately.
- A revoked node is recovered by issuing a new token and running `kurrent join`; the old certificate does not need manual deletion.

## Device Plane

### Anchors

- The device private key is generated and persisted in NVRAM (ECDSA P-256) and never leaves the machine.
- The registration window only decides whether a device enters the pool; it grants no boot rights.
- The binding is `bound_worker_id` on the device side; `/boot-vars` verifies that the requesting MAC belongs to that Worker's bound device.
- Challenge-response uses a one-shot nonce and an ECDSA signature; with enforcement on, no key, no signature or a bad signature is rejected.
- Server certificate TOFU pins the leaf fingerprint on the first handshake and rejects any later mismatch.

### Explicit Boundaries

1. **The first hop has no cryptographic protection.** DHCP and the TFTP-delivered `snponly.efi` / `boot.ipxe` are trusted directly by device firmware. An attacker on the same Layer 2 can forge DHCP/ARP and replace the first-stage files. Deployments must isolate the PXE network and enable DHCP snooping, Dynamic ARP Inspection, RA guard and port security; this is a deployment boundary, not a cryptographic closure inside the product.
2. **The device key is a bearer credential.** Challenge-response proves "the caller holds this private key", not "the code running with this key is trustworthy". An operating system that can read firmware variables after boot can use the key. Mainline firmware is unsigned and does not use TPM measurement, so this path has no in-product closure yet.
3. **TOFU trusts the first connection.** The first handshake does not validate a CA; it pins the leaf fingerprint and may be intercepted. After the control-plane certificate rotates, devices must re-trust it or refuse the connection.
4. **Physical access.** An attacker who can reset NVRAM erases the key and fingerprint and degrades the device to an unregistered machine. It is still bounded by the registration window and binding, but the original identity anchor is gone.

## Data Plane

- Authentication: DH-HMAC-CHAP; the target restricts initiator Host NQNs through `allowed_hosts`.
- Encryption: TLS 1.3 on a single port `4421`, shared by firmware and kernel segments.
- The PSK is derived from the authentication key and not stored separately; rotating or revoking the key rotates or revokes the PSK atomically.
- On the storage node the PSK lands in the kernel `.nvme` keyring; `tlshd >= 1.5.0` with process-keyring priority is required so a stale key cannot shadow a new one.
- On nvmet, a missing key or an unready port withholds the full boot-variable payload; non-nvmet backends project no NVMe-oF credentials. `menu.ipxe` fails when required variables are missing rather than assembling a plaintext URI.

### Plaintext at Rest

DHHC-1 keys, derived PSKs, device keys and CA keys exist in plaintext where the chain requires them:

- control-plane state files (0600);
- Agent cache (0600);
- storage-node configfs and kernel keyring;
- device NVRAM.

Kurrent does not add another application-layer encryption layer. Deployments own disk/backup encryption, host root boundaries and physical security.

## Not Solved Today

- **First-hop replacement and attacks on the same Layer 2 segment** — Depend on PXE network isolation and Layer-2 controls.
- **Reading the device key after boot** — Unsigned firmware; no Secure Boot/TPM measurement.
- **Control-plane or storage-host root** — Treated as loss of the corresponding boundary.
- **Physical access and NVRAM reset** — Requires re-registration.
- **Availability attacks** — DHCP pool exhaustion, endpoint rate limits and resource exhaustion have no complete DoS design.
- **Control-plane high availability** — Single node with file state; host failure requires backup recovery.
- **Encryption of keys at rest** — Deployment responsibility.

## Optional Hardening Directions

The following are not merged into mainline and are not deployment prerequisites:

- **Secure Boot with a pinned trust anchor**: signed iPXE, embedded boot script, fixed CA. This closes first-hop replacement; it does not stop a booted system from reading and exfiltrating the device key.
- **TPM measured boot / remote attestation**: binds device identity to the code being run and closes the path where booted software uses the key.
- **Single-key UKI approach**: collapses boot components and configuration into a signable unit, reducing mutable parts of the chain of trust.

These directions redesign trust roots and provisioning. They must not be described as available until end-to-end acceptance is complete.

## Deployment Checklist

- [ ] The PXE network is dedicated, and the control plane is the only DHCP/TFTP provider.
- [ ] Switches enable DHCP snooping, Dynamic ARP Inspection, RA guard and port security; 802.1X/MAB where available.
- [ ] The control-plane host exposes only 22, 53/67/69 and 80/443; storage nodes expose only 4840/4421 (source-restricted as needed).
- [ ] `host-baseline.sh` reports no FAIL; CA keys, `admin.conf`, credentials and audit files are 600.
- [ ] The registration window is opened on demand with an admission cap and closed afterwards.
- [ ] Signature enforcement is enabled after devices have claimed their keys.
- [ ] User certificates are issued on demand and revoked immediately on departure or compromise.
- [ ] CRL refresh, credential pushes and audit alarms are reviewed regularly.
- [ ] Backups are encrypted and recovery is tested.
- [ ] Firmware artifacts come from a trusted source and are verified with `SHA256SUMS`.

## Related Pages

- Admission decisions at boot: [Boot Chain](../concepts/boot-chain.md)
- Firmware trust boundary and residual risk: [Device Identity and Trust](../firmware/index.md)
- Identity types and lifecycle: [Identity and Access](../concepts/identity.md)
- Credentials and the data plane: [Storage and Data Plane](../concepts/storage.md)
- Deployment prerequisites and baseline: [Deployment](../guide/deployment.md)
- Components and ports: [Architecture](./index.md)
