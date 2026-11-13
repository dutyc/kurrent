# Firmware Overview

Kurrent's device firmware is built by the separate [kurrent-firmware](https://github.com/dutyc/kurrent-firmware) repository. The repository does not carry iPXE source; it maintains difference patches and build assets on top of the pinned upstream baseline `e6e51ccb`.

Firmware handles the device side of the boot path: NVMe/TCP SAN boot, DH-HMAC-CHAP authentication, NBCT credential handoff, device identity keys, and TOFU. Registration windows, Worker binding, system disk selection, and credential issuance belong to the control plane.

![Firmware pipeline: from the upstream iPXE baseline to device network boot](/diagrams/firmware-pipeline.svg)

## Responsibility split

| Repository | Responsibility | Deliverable |
| --- | --- | --- |
| kurrent-firmware | Protocol drivers, authentication, device information collection, NIC adaptation, firmware build | `dist/` artifacts, patch set, test tooling |
| kurrent (platform) | Control plane, boot scripts, registration window, identity and credential projection, system disks | Control-plane service, `tftp/` scripts, API |

Firmware is unaware of business policy: registration windows, Worker binding, disk selection, and credential issuance live in the platform repository. Firmware provides commands and protocol capabilities; boot scripts decide the call order.

## Capability overview

| Capability | Description |
| --- | --- |
| NVMe/TCP SAN boot | Native `nvmetcp` driver covering ICReq negotiation, Connect, Property Set, Identify, I/O queues, and R2T reads/writes |
| DH-HMAC-CHAP authentication | One-way authentication; hashes SHA-256/384/512, DH groups 0/2048/3072/4096 |
| NBCT credential table | Hands the session key, Host NQN, and TLS handoff parameters to the kernel, avoiding a second credential fetch |
| TLS 1.3 PSK data plane | Firmware-segment `sanboot` establishes TLS 1.3 with the PSK; together with the kernel segment this covers the full path |
| Host NQN setting | Injects `hostnqn` per MAC, supporting nvmet `allow_any_host=0` strict mode |
| Device information collection | Reports SMBIOS identity and memory, CPUID, and NIC fields over HTTP |
| EFI NVS / TPM NV persistence | Stores `server-fingerprint` in NVRAM; the AK and platform state live in TPM NV across reboots |
| TOFU trust | Pins the server leaf certificate SHA-256 fingerprint on the first HTTPS handshake and rejects mismatches |
| Device identity and attestation | TCG2 access, AK creation and export, SHA-256 quote over PCR 0/2/4/7, `nvclear` |
| NIC adaptation | Native RTL8125/8126 drivers and a `snponly` local-boot fallback |
| Local-media boot | `embed/auto.ipxe`; the `direct-uefi` artifact embeds a script that enters the network boot chain immediately |

Patch numbers, the NIC support matrix, and field-level details are in the [Firmware Platform Reference](../reference/firmware-platform.md).

## Device identity and trust

Device identity is a TPM 2.0 attestation key (AK): on first boot firmware creates a restricted signing key (ECC P-256 or RSA-2048) inside the TPM and persists it in TPM NV; the private key cannot be exported, later boots reuse the existing AK and never rebuild it automatically. Firmware reports the device fingerprint, AK public key (SPKI), AK name and Secure Boot state to `/devices/report`.

Attestation reuses one challenge round: the control plane issues a one-shot nonce, the device quotes PCR 0/2/4/7 with SHA-256 and copies the nonce verbatim into `extraData`, and the quote with its signature travels with `/boot-vars`. The control plane verifies the signature and compares the digest with the platform baseline before serving boot variables. The first successful attestation records the baseline (TOFU); after a PCR change an administrator approves the new baseline.

TOFU is a global single anchor: once `server-fingerprint` exists, firmware trusts only the certificate pinned during the first HTTPS connection. Every HTTPS target must share that anchor; after rotating the control plane certificate, clear the NVRAM fingerprint first, otherwise the device cannot connect again.

Security boundaries:

- **The AK private key cannot be exported**: it stays inside the TPM, so a booted system cannot read or copy the device identity.
- **Attestation covers boot measurements, not runtime**: PCR 0/2/4/7 reflect firmware, Secure Boot policy and boot configuration; a compromised running system can still obtain the current PSK within the rotation window. See the [security model](../architecture/security.md).
- **The first hop depends on Secure Boot**: admission requires Secure Boot enabled; first-hop files are verified against the UEFI trust store and TFTP delivers only signed binaries.
- **Server TOFU first trust**: the first handshake can still be intercepted; after the control-plane certificate rotates, devices must re-trust it.
- **Nonces must be one-time**: replay requires a repeated nonce; the control plane keeps nonces one-time, short-lived, and bound to the MAC.
- **The AK cannot be replaced online**: replacing it requires deleting the ledger entry and re-admitting the device during the registration window.

Commands, TPM NV settings, quote format, challenge-attestation and TOFU states are in the [Firmware Contract](../reference/firmware-contract.md).

## NVMe-oF boot

Firmware includes a native `nvmetcp` driver for NVMe over TCP SAN boot:

- Protocol flow: ICReq/ICResp negotiation → Connect (Admin) → authentication → Property Set (CC.EN=1) → Identify (controller/namespace) → I/O queue Connect → block reads and writes.
- Authentication: one-way DH-HMAC-CHAP, hashes SHA-256/384/512, DH groups 0/2048/3072/4096.
- TCP transport only; RDMA and FC are not supported.
- The boot path uses a single controller and a single namespace; Identify parses both 512 B and 4 KB blocks.

Target configuration, `sanboot` examples, DHHC-1 format, and server-side errors are in the [Firmware Platform Reference](../reference/firmware-platform.md#target-configuration). Boot-variable projection rules are in the [Firmware Contract](../reference/firmware-contract.md#boot-variables).

## Runtime boundaries

- Build artifacts are UEFI-only, seven in total.
- NVMe/TCP only; RDMA and FC are not supported.
- The boot path uses a single controller and a single namespace; Identify parses 512 B and 4 KB blocks.
- DH-HMAC-CHAP is one-way; mutual authentication is outside the firmware implementation.
- HTTPS downloads use iPXE native TLS up to 1.2; data-plane `sanboot` uses TLS 1.3 PSK.
- Mainline firmware is unsigned and cannot load with Secure Boot enabled; disable Secure Boot before deployment.
- Firmware provides commands and protocol capabilities only; it does not implement registration windows, Worker binding, disk selection, or credential issuance.
- iPXE-side NBFT generation is still pending; the current validation path uses an injected NBFT table to simulate the firmware pre-generation stage.

## Related pages

- [Firmware Platform Reference](../reference/firmware-platform.md)
- [Firmware Contract](../reference/firmware-contract.md)
- [Firmware Build and Deployment](./build-deploy.md)
- [Distribution Matrix](../reference/distributions.md)
- [Boot Chain](../concepts/boot-chain.md)
- [Security Model](../architecture/security.md)
- Firmware repository source, patches and research notes: <https://github.com/dutyc/kurrent-firmware>
