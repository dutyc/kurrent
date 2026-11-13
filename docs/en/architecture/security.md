# Security Model

Kurrent's security boundary is not a single line but four independent trusts:

| Boundary | Protected object | Anchor |
| --- | --- | --- |
| Management plane | Who may call management APIs | users CA certificates / management tokens / server-side sessions |
| Component plane | Which storage-node components may join and renew | nodes CA + bootstrap token bound to a node identity + CRL |
| Device plane | Which machine may obtain boot credentials | TPM AK + PCR baseline + registration window + binding + signed first hop and fixed boot CA |
| Data plane | Who may connect and whether content is encrypted | DH-HMAC-CHAP + TLS 1.3 PSK |

They cannot substitute for each other. A compromised management plane does not let a device claim boot credentials; a non-exportable device identity key still grants no management access.

## Threat Model

### Assumptions

- Control-plane hosts, storage-node hosts and network equipment are managed by a trusted operator; host root compromise is treated as loss of the corresponding boundary.
- The PXE network is a dedicated trust boundary, and the deployment provides Layer-2 protection.
- Physical access to devices is controlled; an attacker who can clear the TPM or reset the firmware trust store is outside this model.
- Operators use management credentials with least privilege; backups and key storage are protected by the deployment.

### Goals

- Unauthorized principals cannot read ledgers, change bindings, issue credentials or perform writes through the management plane.
- An unclaimed, unbound or un-attested device cannot obtain another Worker's boot variables.
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
| Management credential handoff | One-shot exchange directory: per-file SHA-256, 0700 directory / 0600 files, owner alignment; the CLI consumes and deletes the copies only after its self-test, while the authoritative file stays in the state root |

Reads require authentication; writes require the `admin` group; session-authenticated writes additionally require same-origin.

### Audit

Every management request writes an audit record; if the write fails the request returns 503 (fail-closed), so no write proceeds without audit. Alarms cover credentials injected without attestation, cross-class access, unknown issuer/CRL hits, authentication-failure bursts, bootstrap identity mismatch, certificate and CRL changes, user-file changes and audit failure.

## Component Plane

- Component certificates are issued by the nodes CA; first join requires a bootstrap token bound to `agent_id`, single-use by default.
- Components renew before expiry; the control plane checks the peer serial against the CRL.
- Components periodically fetch the nodes CRL; a missing or expired CRL fails closed instead of allowing traffic.
- Revoking a component also deletes its bootstrap tokens, closing the window where a certificate could be reissued immediately.
- A revoked node is recovered by issuing a new token and running `kurrent join`; the old certificate does not need manual deletion.

## Device Plane

### Anchors

- Device identity is a TPM 2.0 attestation key (AK): the private key is created inside the TPM, is not exportable, and cannot be read by a booted system.
- The registration window only decides whether a device enters the pool; admission requires a valid AK and Secure Boot enabled, and devices without a TPM or with Secure Boot off are rejected.
- The binding is `bound_worker_id` on the device side; `/boot-vars` verifies that the requesting MAC belongs to that Worker's bound device.
- Challenge-attestation uses a one-shot nonce and a PCR quote; enforcement is on by default, and no AK, an invalid quote, a replayed nonce or a PCR digest that does not match the baseline is rejected.
- The platform baseline is recorded when the first attestation succeeds (TOFU); a changed digest is applied only after an administrator approves it.
- Boot HTTPS trusts the boot CA embedded in the signed firmware (4443): only server certificates issued by that CA are accepted, with no fallback to first-contact trust; rotating the boot CA equals reflashing the firmware.

### Explicit Boundaries

1. **The first hop depends on Secure Boot.** Admission requires Secure Boot enabled; first-hop code is verified against the UEFI trust store, and TFTP delivers only signed binaries. Signed firmware, AK creation and the quote command come from the firmware repository; per-model acceptance is not complete. A device with Secure Boot off cannot be admitted, and a deployment cannot bypass first-hop protection by disabling Secure Boot.
2. **The limits of device identity.** The AK private key is not exportable, and attestation covers the boot measurements in PCR 0/2/4/7. That does not make the running system trustworthy: a compromised operating system can still obtain the current PSK within the rotation window and read the mounted system disk. The PCR baseline is TOFU—if the first attestation happens under attacker control, the baseline is poisoned, so first enrollment should be verified against the provisioning environment.
3. **Boot HTTPS trust comes from the signed firmware.** The boot CA is baked into the signed image at build time and only issues the 4443 leaf; changing it requires re-signing the firmware and re-enrolling `db` on every target, otherwise boot HTTPS always fails.
4. **Physical access.** An attacker who can clear the TPM or replace the mainboard loses the original AK; the device degrades to an unregistered machine that must be deleted from the ledger and re-admitted during the registration window. The original identity does not come back.

### First Hop and Attestation

The first hop is DHCP → TFTP `snponly.efi`. UEFI firmware verifies the signature against the site certificate in the target's `db`, so an unsigned or tampered image never runs; the static scripts, boot CA and controller address are embedded in that signed binary and cannot be replaced separately.

After the first hop, two mechanisms take over:

- **Server identity**: firmware trusts only the boot CA embedded at build time, so boot HTTPS is anchored by certificates issued by that CA rather than first-contact trust.
- **Device identity**: the TPM AK quotes PCR 0/2/4/7 with the nonce copied verbatim into `extraData`; the control plane verifies the signature and the one-shot nonce, then compares the PCR digest against the platform baseline. The private key is not exportable, so a booted system cannot copy the device identity elsewhere.

An attacker on the same Layer 2 is therefore limited to denial of service: forged DHCP/ARP or a link disruption can stall a device, but cannot replace the first hop, swap scripts or impersonate the boot server. An attacker who gains runtime control of an admitted device can still obtain the current PSK within the rotation window; a shorter rotation period and revocation are the main countermeasures.

### Platform Baseline and Firmware Changes

The PCR baseline covers firmware, Secure Boot policy and boot configuration. Upgrading firmware, changing Secure Boot keys or adjusting boot entries changes the PCRs; attestation is then rejected with `pcr_mismatch` and boot credentials stop flowing. After verifying the change, an administrator approves the device's latest reported digest in the device detail page, and the next boot is checked against the new baseline. This separates legitimate platform changes from unexplained measurement drift and never allows a silent pass.

## Data Plane

- Authentication: DH-HMAC-CHAP; the target restricts initiator Host NQNs through `allowed_hosts`.
- Encryption: TLS 1.3 on a single port `4421`, shared by firmware and kernel segments.
- The PSK is derived from the authentication key and not stored separately; rotating or revoking the key rotates or revokes the PSK atomically.
- On the storage node the PSK lands in the kernel `.nvme` keyring; `tlshd >= 1.5.0` with process-keyring priority is required so a stale key cannot shadow a new one.
- On nvmet, a missing key or an unready port withholds the full boot-variable payload; non-nvmet backends project no NVMe-oF credentials. `menu.ipxe` fails when required variables are missing rather than assembling a plaintext URI.

### Plaintext at Rest

DHHC-1 keys, derived PSKs and CA keys exist in plaintext where the chain requires them:

- control-plane state files (0600);
- Agent cache (0600);
- storage-node configfs and kernel keyring.

Kurrent does not add another application-layer encryption layer. Deployments own disk/backup encryption, host root boundaries and physical security.

## Not Solved Today

- **First-hop replacement** — Closed by Secure Boot verifying signed firmware; a device with Secure Boot off cannot be admitted.
- **Boot server impersonation** — Closed by the boot CA embedded in the signed firmware; there is no fallback to first-contact trust.
- **Boot-plane denial of service** — Forged DHCP/ARP and link disruption can still block boot; there is no complete DoS design.
- **Runtime compromise** — TPM attestation does not protect the running system; a compromised system can obtain the current PSK within the rotation window.
- **Platform baseline and firmware validation** — Per-model PCR behavior (discrete TPM / AMD fTPM) and signed-firmware acceptance are not complete.
- **Control-plane or storage-host root** — Treated as loss of the corresponding boundary.
- **Physical access and TPM clear / mainboard replacement** — The original AK is lost; the ledger entry must be deleted and the device re-admitted.
- **Availability attacks** — DHCP pool exhaustion, endpoint rate limits and resource exhaustion have no complete DoS design.
- **Control-plane high availability** — Single node with file state; host failure requires backup recovery.
- **Encryption of keys at rest** — Deployment responsibility.

## Deployment Checklist

- [ ] The PXE network is dedicated, and the control plane is the only DHCP/TFTP provider.
- [ ] Switches enable DHCP snooping, Dynamic ARP Inspection, RA guard and port security; 802.1X/MAB where available.
- [ ] The control-plane host exposes only 22, 53/67/69 and 80/443/4443; 4443 is reachable only from the boot segment; storage nodes expose only 4840/4421 (source-restricted as needed).
- [ ] `sudo kurrent host check --strict` reports neither FAIL nor SKIP; CA keys, `admin.conf`, credentials and audit files are 600.
- [ ] The registration window is opened on demand with an admission cap and closed afterwards.
- [ ] TPM attestation enforcement stays on by default; disable it only temporarily for migration or troubleshooting, then restore it.
- [ ] Targets have Secure Boot enabled with the site signing certificate and the Microsoft UEFI CA enrolled; devices without a TPM are not admitted.
- [ ] Boot-chain artifacts come from a signed build (`build-sb.sh`), and `secureboot.status` in `/boot-assets` is `ok`.
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
