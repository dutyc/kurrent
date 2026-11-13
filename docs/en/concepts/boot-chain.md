# Boot Chain

The boot chain determines three aspects at power-on: the identity, the target, and the disk to boot. Kurrent does not write them into the machine or the disk; the control plane projects them at every boot according to the current binding.

This page describes the main path (NVMe/TCP with TLS 1.3). Firmware build and provisioning are covered under [Firmware](../firmware/index.md); trust boundaries are covered in the [Security Model](../architecture/security.md).

## Overview

![Boot chain: first hop, device identity and data plane](/diagrams/boot-chain.svg)

The split is explicit: firmware provides protocol drivers, TPM AK and quote commands, NV persistence and boot CA pinning; the platform repository owns boot scripts and admission decisions.

## 1. Network Boot

The device gets an address and `next-server` over DHCP, then downloads the first-stage `snponly.efi` over TFTP (the TFTP root also exposes the `boot.ipxe` pointer). The first stage must be signed with the site certificate enrolled in the target's `db`, otherwise firmware refuses to run it.

The signed first stage embeds the static scripts, the boot CA and the controller address: menus, variables and device endpoints are fetched over 4443 HTTPS, trusting only that boot CA and never first-contact trust.

The TFTP root exposes only these two files. Restart the dnsmasq container after replacing `snponly.efi`: the file-level mount pins the inode.

## 2. Device Identity

On first boot, firmware creates an attestation key (AK, a restricted signing key; ECC P-256 or RSA-2048) inside the local TPM; the private key is non-exportable. Later boots reuse the same AK, and an existing AK is never recreated. The control plane records only the AK public key (SPKI DER), the AK name and the key type.

Firmware reports to `/devices/report`:

- the 11-field fingerprint (MAC, UUID, SMBIOS, CPU, memory, NIC and others);
- the AK public key, AK name and type;
- the Secure Boot state (self-reported; used only for admission during enrollment).

The control plane records the device in the pool by MAC. **Reporting is not authorization**:

- an unknown MAC is admitted only while the registration window is open, within the admission quota, with a valid AK and `sb=1`;
- a pooled but unbound device receives no boot credentials;
- once the window closes, reports only update fingerprints and add no entries;
- devices without a TPM, or with Secure Boot off, are not admitted.

The AK cannot be rotated online: device recovery or a mainboard swap goes through the administrator flow of deleting the entry and re-enrolling during a registration window.

## 3. Challenge-Attest

With binding and attestation enforcement on (the default), knowing a MAC is not enough to obtain credentials. The device requests a one-shot nonce from `/devices/challenge` and produces an SHA-256 quote over PCR 0/2/4/7 with its TPM AK:

```text
extraData = nonce    (verbatim; no encoding transform)
qualifiedSigner = AK
pcrDigest = SHA-256( PCR 0 / 2 / 4 / 7 )
```

Reading the PCRs and producing the quote happen in one TPM call, so no state can change between the two reads. The nonce is single-use and bound to the MAC; `report`, `challenge` and `boot-vars` are rate-limited per source IP. The quote and signature are submitted with `/boot-vars`; the control plane verifies them against the registered AK public key and compares the PCR digest with the platform baseline.

The baseline is recorded the first time attestation passes (TOFU); a digest change is denied as `pcr_mismatch` and kept as a pending digest until an administrator approves it.

## 4. Admission and Variable Projection

`/boot-vars` is the most important decision on the boot chain. The control plane checks, in order:

1. **Resolution**: the request MAC is resolved to a Worker through the device ledger; hostname is only a fallback when no MAC is supplied or the ledger has no hit.
2. **Binding is authentication**: the requesting MAC must belong to the matched Worker's bound device; another device cannot claim the Worker's boot identity.
3. **TPM attestation**: enforcement is on by default, and a missing AK, an invalid quote, a replayed nonce or a PCR mismatch is rejected.
4. **Credential conditions**: the Worker is bound and a key entry exists; the nvmet backend also requires the storage node's data-plane port to be ready. If an nvmet condition fails, the payload is withheld; non-nvmet backends project no NVMe-oF credentials.

After these checks, projection depends on the attestation result: credentials go only to devices that passed attestation, and non-credential variables are served only when attestation enforcement is explicitly disabled.

| Variable | Content |
| --- | --- |
| `disk-nqn` | Full NQN of the default disk |
| `storager-ip` | Storage node holding the disk |
| `hostnqn` | Initiator Host NQN for the Worker |
| `nbft-secret` | DH-HMAC-CHAP authentication key |
| `nbft-tls-psk` / `nbft-tls-port` | TLS PSK derived from the key, and the data-plane port |
| `menu-default` | Boot entry selected after the menu timeout |

With no default disk, `menu-default=reboot` is returned; a pooled but unbound device also loops on reboot until bound. On nvmet, a missing key or an unready port withholds the payload; non-nvmet backends project no NVMe-oF key or PSK. No path assembles a plaintext URI.

## 5. Menu

`menu.ipxe` is loaded over HTTPS and shows the OS label of the current default disk:

- the default disk boots after the timeout;
- to switch system disks, set another disk as default in the Worker detail page in the WebUI; it takes effect on the next boot;
- the boot entry comes from the ledger projection, not from configuration inside the disk.

## 6. Data-Plane Connect

The `sanboot` target looks like:

```text
nvme://<storager>:<tls_port>/<disk_nqn>?secret=<dhhc1>&tls_psk=<psk>&tls_trsvcid=<tls_port>
```

The connection has two layers:

- **Authentication**: DH-HMAC-CHAP controls which initiators may connect.
- **Encryption**: TLS 1.3 protects content on the same port `4421`.

Firmware performs the TLS handshake itself. Authentication failure, a missing PSK or an unready port fails the connection; there is no plaintext fallback port.

## 7. Hand-off to the Operating System

At `sanboot`, firmware installs an **NBCT** credentials table (an ACPI table) recording for this session:

- target address and port;
- subsystem NQN and host NQN;
- DH-HMAC-CHAP key.

The TLS PSK and data-plane port travel with the `sanboot` URI; NBFT carries network and subsystem discovery.

The initramfs `nbft-connect` consumes **NBFT** (network and subsystem discovery) and **NBCT** (credentials and host NQN relay), reconnects on the same port with `--tls`, and mounts the root filesystem.

No machine identity is written into the disk: identity, credentials and connection data come from this boot's projection.

> Current status: NBCT is produced by firmware; iPXE-side NBFT generation is still pending. The verified whole-machine chain injects an NBFT table to emulate the "firmware pre-generated" stage.

## Failure Paths

- **Device never enters the pool** — Registration window closed; report lacks a valid AK or `sb!=1`; device revoked.
- **Device pooled but cannot boot** — No Worker binding; or the Worker has no system disk.
- **Device reboots repeatedly** — The Worker has no default disk; or the pooled device is not yet bound.
- **`ak_unregistered` / `quote_invalid` under enforcement (the default)** — Device has not claimed its AK; the boot script did not obtain a nonce or produce a quote.
- **`pcr_mismatch`** — Firmware, Secure Boot or boot configuration changed; an administrator approves the new baseline from the device detail page.
- **Boot variables withheld** — Worker has no credential; backend is not nvmet; data-plane port is not ready.
- **Reboot after the menu timeout** — No default disk set.
- **Data-plane connect fails** — PSK not in the storage node keyring; `tlshd` version or kernel TLS support missing.
- **HTTPS handshake fails** — The boot CA differs (the certificate used at firmware build time and `state/pki/ca/boot/ca.crt` are not the same), or the 4443 leaf SAN or port differs from the firmware build.

## Trust Boundary

The first hop (DHCP → TFTP `snponly.efi`) is verified by UEFI Secure Boot: the target's `db` trusts only the site signing certificate, so an unsigned or tampered first stage never runs and the embedded static scripts cannot be replaced either. The 4443 HTTPS that follows is anchored by the embedded boot CA, and device identity is protected by the TPM AK and PCR baseline:

- device identity (TPM AK + PCR baseline + registration window + binding);
- boot server identity (fixed boot CA, no fallback to first-contact trust);
- PCR attestation and binding-as-authentication;
- data-plane encryption with no plaintext fallback.

Attackers on the same Layer 2 are therefore limited to denial of service (forged DHCP/ARP, link disruption) and cannot replace the first stage, swap scripts or impersonate the boot server. A compromised host, a compromised running device or physical access is outside this boundary. See the [Security Model](../architecture/security.md) for the full threat model.

## Firmware Split

| Capability | Provided by |
| --- | --- |
| DHCP/TFTP client, HTTPS, boot CA pinning, NVRAM | Firmware |
| TPM AK creation and PCR quote | Firmware |
| NVMe/TCP driver, DH-HMAC-CHAP, NBCT | Firmware |
| Boot scripts, registration window, `/boot-vars` admission | Platform control plane |
| System disks, credentials, menu generation | Platform control plane |

Firmware build, artifacts and device provisioning are covered in [Build and Artifacts](../firmware/build-deploy.md) and [Provisioning](../firmware/build-deploy.md); trust anchors and provisioning for Secure Boot are in [Secure Boot Boot](../firmware/secure-boot.md); the full semantics of the device key, quote format and trust anchors are in the [Firmware Contract](../reference/firmware-contract.md); NVMe/TCP and NBCT configuration is in the [Firmware Platform Reference](../reference/firmware-platform.md).
