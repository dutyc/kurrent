# Boot Chain

The boot chain determines three aspects at power-on: the identity, the target, and the disk to boot. Kurrent writes none of them into the machine or the disk; the control plane projects them on every boot from the current binding.

The boot chain obtains the connection information for the Worker's system disk under the current binding; the main path is NVMe/TCP with TLS 1.3, and firmware build and provisioning are covered under [Firmware](../firmware/index.md).

## Three Stages of a Boot

| Stage | Question | Trust anchor | Failure |
| --- | --- | --- | --- |
| First hop | Is the code trusted? | Site certificate in UEFI `db` | Firmware refuses |
| Identity | Is it that device? | TPM AK + PCR baseline | Not admitted, no credentials |
| Data plane | Which target? | DHHC-1 + TLS 1.3 | Connection refused |

![Boot chain: first hop, device identity and data plane](/diagrams/boot-chain.svg)

The three stages run in order; if any stage fails, boot stops instead of switching to another path.

## First Hop

The device gets an address and `next-server` over DHCP, then downloads the first stage `snponly.efi` over TFTP. The image must be signed with the site certificate in the target's `db`, otherwise firmware refuses to run it; the signed image embeds the static scripts, the boot CA and the boot HTTPS port, and leaves the controller address out of the image by default, so later content is fetched only over 4443 HTTPS anchored by that CA.

Signature verification and embedded content are covered in [First Hop](./boot-chain/first-hop.md).

## Device Identity

On first boot, firmware creates an attestation key (AK) inside the local TPM; the private key is non-exportable. The device reports its fingerprint and AK, and the control plane admits it by MAC, but **registration grants no privilege**. On every boot the device produces a quote over PCR 0/2/4/7, the control plane compares it with the platform baseline, and only a device that passes attestation can receive credentials.

AK creation, enrollment and attestation are covered in [Device Identity](./boot-chain/device-identity.md).

## Data Plane

`/boot-vars` checks resolution, binding, attestation and credential conditions in order, then projects the model into `disk-nqn`, `storager-ip`, the Host NQN, the DHHC-1 key and the derived TLS PSK. Firmware completes authentication and the TLS 1.3 handshake on the same port `4421`, with no plaintext fallback.

Admission conditions and boot variables are covered in [Data Plane](./boot-chain/data-plane.md).

## Hand-over to the Kernel

At `sanboot`, firmware installs an NBCT credentials table recording this session's target, NQNs and DHHC-1 key; the initramfs `nbft-connect` consumes NBCT and NBFT, reconnects on the same port with `--tls`, and mounts the root filesystem. No machine identity is written into the disk.

How credentials reach the kernel is covered in [Hand-over to the Kernel](./boot-chain/handover.md).

## Failures and Observability

No stage falls back to another path: when first-hop verification fails, firmware does not run the image; when identity or admission fails, `/boot-vars` returns an empty script; when credentials or the port are not ready, the payload is withheld; a device that keeps rebooting is usually unbound or has no default disk. Reason codes are written to the audit log and the device's attestation state; the operational entry points are in [Operations](../tasks/operations.md).

## Trust Boundary

The first hop (DHCP → TFTP `snponly.efi`) is verified by UEFI Secure Boot: the target's `db` trusts only the site signing certificate, so an unsigned or tampered first stage never runs and the embedded static scripts cannot be replaced either. The 4443 HTTPS that follows is anchored by the embedded boot CA, and device identity is protected by the TPM AK and PCR baseline.

Attackers on the same Layer 2 are therefore limited to denial of service (forged DHCP/ARP, link disruption) and cannot replace the first stage, swap scripts or impersonate the boot server. A compromised host, a compromised running device or physical access is outside this boundary. See the [Security Model](../concepts/architecture/security.md) for the full threat model.

## Firmware Split

| Capability | Provided by |
| --- | --- |
| DHCP/TFTP client, HTTPS, boot CA pinning, NVRAM | Firmware |
| TPM AK creation and PCR quote | Firmware |
| NVMe/TCP driver, DH-HMAC-CHAP, NBCT | Firmware |
| Boot scripts, registration window, `/boot-vars` admission | Platform control plane |
| System disks, credentials, menu generation | Platform control plane |

## What's next

- First-hop verification and embedded content: [First Hop](./boot-chain/first-hop.md)
- Device AK, registration and attestation: [Device Identity](./boot-chain/device-identity.md)
- Admission, variables and connection: [Data Plane](./boot-chain/data-plane.md)
- Credentials and discovery passed to the kernel: [Hand-over to the Kernel](./boot-chain/handover.md)
- Device AK, quote format and trust anchors: [Firmware Contract](../reference/firmware-contract.md)
- Disks, credentials and the data plane: [Storage and Data Plane](./storage.md)
