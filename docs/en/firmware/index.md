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
| EFI NVS persistence | Stores `device-key` and `server-fingerprint` in NVRAM across reboots |
| TOFU trust | Pins the server leaf certificate SHA-256 fingerprint on the first HTTPS handshake and rejects mismatches |
| Device identity commands | `keygen` / `pubkey` / `sign` / `nvclear` |
| NIC adaptation | Native RTL8125/8126 drivers and a `snponly` local-boot fallback |
| Local-media boot | `embed/auto.ipxe`; the `direct-uefi` artifact embeds a script that enters the network boot chain immediately |

Patch numbers, the NIC support matrix, and field-level details are in the [Firmware Platform Reference](../reference/firmware-platform.md).

## Device identity and trust

On first boot, firmware runs `keygen`, generating an ECDSA P-256 private key in EFI NVRAM; the key never leaves the machine. Later boots refuse to overwrite an existing key. `pubkey` exports the public key, and firmware reports the device fingerprint and public key to the control plane.

With binding and signature enforcement in place, knowing a MAC is not enough to obtain credentials. The control plane issues a one-shot nonce, and the device signs `nonce‖mac`; only a verified device receives boot variables. The device key can be rotated online, and the old key is invalidated immediately on success.

TOFU is a global single anchor: once `server-fingerprint` exists, firmware trusts only the certificate pinned during the first HTTPS connection. Every HTTPS target must share that anchor; after rotating the control plane certificate, clear the NVRAM fingerprint first, otherwise the device cannot connect again.

Security boundaries:

- **The private key does not leave the device**: `device-key` is used only inside firmware; the public key and signatures may be exported.
- **A booted system can read the private key**: the key lives in an EFI variable with runtime access, so the running OS can read it. Secure Boot does not close this read path; see the [security model](../architecture/security.md).
- **First contact is unauthenticated**: TOFU accepts the first certificate inside the registration window; PXE segment isolation carries the man-in-the-middle risk.
- **Nonces must be one-time**: replay requires a repeated nonce; the control plane must keep nonces one-time, short-lived, and bound to the MAC.

Commands, NVRAM settings, signature format, challenge-response, and TOFU states are in the [Firmware Contract](../reference/firmware-contract.md).

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
