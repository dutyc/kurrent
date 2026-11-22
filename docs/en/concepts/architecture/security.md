# Security Model

Kurrent's security boundary consists of four independent trusts:

| Boundary | Protected object | Anchor |
| --- | --- | --- |
| Management plane | Who may call management APIs | users CA certificates / management tokens / server-side sessions |
| Component plane | Which storage-node components may join and renew | nodes CA + bootstrap token bound to a node identity + CRL |
| Device plane | Which machine may obtain boot credentials | TPM AK + PCR baseline + registration window + binding; the boot chain adds a signed first hop and a fixed boot CA |
| Data plane | Who may connect and whether content is encrypted | DH-HMAC-CHAP + TLS 1.3 PSK |

They cannot substitute for each other. A compromised management plane does not let a device claim boot credentials; a non-exportable device identity key still grants no management access.

## Threat Model

### Assumptions

- Control-plane hosts, storage-node hosts and network equipment are managed by a trusted operator; host root compromise is treated as loss of the corresponding boundary.
- The PXE network is a dedicated trust boundary, and the deployment provides Layer-2 protection.
- Physical access to devices is controlled; clearing the TPM or replacing the mainboard is outside the defended range, and its consequences are listed under residual risks.
- Operators use management credentials with least privilege; backups and key storage are protected by the deployment.

### Goals

- Unauthorized principals cannot read ledgers, change bindings, issue credentials or perform writes through the management plane.
- An unclaimed, unbound or un-attested device cannot obtain another Worker's boot variables.
- Revoked components and users cannot continue over mTLS.
- NVMe/TCP data-plane content is encrypted in transit; missing credentials refuse the boot instead of falling back to plaintext.

### Residual Risks

- **Runtime compromise**: TPM attestation covers boot measurements only. A compromised operating system can still obtain the current PSK within the rotation window and read the mounted system disk; a shorter rotation period and revocation are the countermeasures.
- **Boot-plane and availability denial of service**: forged DHCP/ARP, link disruption, DHCP pool exhaustion and endpoint resource exhaustion can block or disturb boot; there is no complete DoS design.
- **Platform baseline acceptance**: per-model PCR behavior for discrete TPMs and AMD fTPM, and per-model signed-firmware acceptance, are not complete.
- **Host root and a single control plane**: control-plane or storage-host root is treated as loss of the corresponding boundary; the control plane is a single node with file state, and host failure requires backup recovery.
- **Physical access**: clearing the TPM or replacing the mainboard loses the original AK; the device degrades to an unregistered machine that must be deleted from the ledger and re-admitted during the registration window. The original identity does not return.
- **Encryption of keys at rest**: DHHC-1 keys, derived PSKs and CA keys exist in plaintext where the chain requires them (see the data plane); disk and backup encryption belong to the deployment.

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

### Three Trust Objects

Admission at boot rests on three trust objects that cannot substitute for each other:

| Object | Anchor | Verified when | On failure |
| --- | --- | --- | --- |
| First-hop code | Site signing certificate in the UEFI `db` | Firmware loads the TFTP first hop | An unsigned or tampered image never runs |
| Boot server | Boot CA embedded in the signed firmware | Boot HTTPS handshake on 4443 | Boot variables are refused, with no fallback to first-contact trust |
| Device identity | TPM AK, private key not exportable | Every boot: nonce + PCR 0/2/4/7 quote | Boot credentials are withheld |

The first hop signs the static scripts, boot CA and boot HTTPS port into one binary and leaves the controller address out by default, and TFTP delivers only that binary, so its contents cannot be replaced separately. The boot CA only issues the 4443 leaf; rotating it equals re-signing the firmware and re-enrolling `db` on every target. Admission requires Secure Boot enabled, so a deployment cannot bypass first-hop protection by disabling it.

With all three anchors in place, an attacker on the same Layer 2 is limited to denial of service: forged DHCP/ARP or a link disruption can stall a device or make it retry, but cannot replace the first hop, swap scripts or impersonate the boot server.

### Device Identity and Admission

- Device identity is a TPM 2.0 attestation key (AK): the private key is created inside the TPM, is not exportable, and cannot be read by a booted system.
- The registration window only decides whether a device enters the pool; admission requires a valid AK and Secure Boot enabled, and devices without a TPM or with Secure Boot off are rejected.
- The binding is `bound_worker_id` on the device side; `/boot-vars` verifies that the requesting MAC belongs to that Worker's bound device.
- Challenge-attestation uses a one-shot nonce and a PCR quote with the nonce copied verbatim into `extraData`; enforcement is on by default, and no AK, an invalid quote, a replayed nonce or a PCR digest that does not match the baseline is rejected.
- The control plane validates the quote structure and signature without side effects, then consumes the nonce, and only then compares the baseline, so a malformed quote does not burn the nonce.

### Platform Baseline and Firmware Changes

The PCR baseline covers firmware, Secure Boot policy and boot configuration. Upgrading firmware, changing Secure Boot keys or adjusting boot entries changes the PCRs; attestation is then rejected with `pcr_mismatch` and boot credentials stop flowing. After verifying the change, an administrator approves the device's latest reported digest in the device detail page, and the next boot is checked against the new baseline. This separates legitimate platform changes from unexplained measurement drift and never allows a silent pass.

The baseline is recorded when the first attestation succeeds. If that first attestation happens under attacker control, the baseline is poisoned, so first enrollment must be verified against the provisioning environment; later digest changes only go through administrator approval.

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

## What's next

- Admission decisions at boot: [Boot Chain](../boot-chain.md)
- Firmware trust boundary and residual risk: [Device Identity and Trust](../../firmware/index.md)
- Identity types and lifecycle: [Identity and Access](../identity.md)
- Credentials and the data plane: [Storage and Data Plane](../storage.md)
- Deployment steps and the security checklist: [Deployment](../../setup/deployment.md)
- Components and ports: [Architecture](./index.md)
