# Firmware Overview

Kurrent's device firmware is built by the separate repository [kurrent-firmware](https://github.com/dutyc/kurrent-firmware). It contains no iPXE source code — only difference patches and build assets — and can be rebuilt on any pinned upstream baseline.

```text
Firmware repo builds dist/ artifacts
        │
        ▼ deploy
Platform tftp/snponly.efi
        │
        ▼ device network boot
Control plane /boot-vars + data plane
```

## Responsibility Split

| Repository | Responsibility | Deliverable |
| --- | --- | --- |
| kurrent-firmware | Protocol drivers, authentication, device info collection, NIC adaptation, firmware build | `dist/` artifacts, patch set, test tooling |
| kurrent (platform) | Control plane, boot scripts, registration window, identity/credential projection, system disks | Control-plane service, `tftp/` scripts, API |

Firmware is unaware of business policy: registration windows, Worker binding, disk selection and credential issuance live in the platform repository. Firmware provides commands and protocol capabilities; boot scripts decide the order.

## Capabilities

| Capability | Description |
| --- | --- |
| NVMe/TCP SAN boot | Native `nvmetcp` driver; upstream iPXE has no such capability |
| DH-HMAC-CHAP | SHA-256/384/512, DH groups 2048/3072/4096; DHHC-1 format validation |
| NBCT credentials table | Hands the session key and Host NQN to initramfs without a second network fetch |
| `hostnqn` setting | Per-MAC/Worker initiator identity for target strict mode |
| Device information reporting | Collects the 11-field hardware fingerprint used by `/devices/report` |
| EFI NVRAM | `device-key` and `server-fingerprint` persist across reboots |
| TOFU | Pins the server leaf fingerprint on the first HTTPS handshake |
| Device key commands | `keygen` / `pubkey` / `sign` (ECDSA P-256) |
| NIC adaptation | Native RTL8125 / RTL8126; other NICs use upstream drivers or SNP |
| Local-media boot | `direct-uefi` artifacts embed a script and enter the network boot flow immediately |

## Artifacts

The build pipeline produces seven UEFI artifacts in two groups:

| Directory | Purpose |
| --- | --- |
| `pxe-uefi/` | Loaded by the mainboard PXE chain; scripts are delivered over DHCP/TFTP |
| `direct-uefi/` | Local media or GRUB chainload; embeds `auto.ipxe` and enters the boot chain immediately |

Each group contains `ipxe.efi`, `snponly.efi` and debug variants (`direct-uefi` does not include `snponly-debug`). See [Build and Artifacts](./build.md) for the full list, selection and verification.

## Current Status

- The full production-stack chain has been verified in a controlled environment: DHCP → TFTP → iPXE → control plane → NVMe/TCP authentication (firmware + kernel) → GRUB → rootfs → login prompt.
- Device identity, TOFU, NVRAM and NBCT are exercised in the whole-machine chain.
- Mainline firmware is unsigned. With Secure Boot enabled, it must be disabled or a separately signed artifact used; the latter is not part of the current mainline deployment path.
- iPXE-side NBFT generation is still pending; the verified chain injects an NBFT table to emulate the firmware pre-generation stage.
- HTTPS uses iPXE's native TLS implementation, capped at TLS 1.2; data-plane TLS 1.3 is performed by the firmware's NVMe/TCP path.
- NIC support follows the firmware repository's matrix: RTL8126 is compile-verified, and some models rely on the SNP fallback.

## Contract with the Platform

Firmware entry and exit points on the boot chain:

```text
DHCP (next-server)
  → TFTP /boot.ipxe
  → HTTPS boot.ipxe.cfg / menu.ipxe
  → /devices/report        fingerprint + public key
  → /devices/challenge     one-shot nonce
  → /boot-vars             disk NQN, Host NQN, DHHC-1, TLS PSK
  → sanboot nvme://...
```

Firmware consumes variables projected by the platform and hands session data to the kernel through NBCT. The field-level contract is in [Firmware Contract](../reference/firmware-contract.md).

## Obtaining Firmware

- **Release artifacts**: download `pxe-uefi-snponly.efi`, `direct-uefi-ipxe.efi` and related assets from the firmware repository's Releases, and verify `SHA256SUMS`.
- **Build locally**: pin the upstream baseline, apply patches and build as described in [Build and Artifacts](./build.md).

The platform repository does not track firmware binaries; `tftp/snponly.efi` must be placed by the deployment. Selection and verification commands are in the [Deployment Guide](../guide/deployment.md#prepare-device-firmware).

## Boundaries and Limits

- Firmware does not solve first-hop trust: DHCP/TFTP content is trusted directly by the device, so deployments must isolate the PXE network. See the [Security Model](../architecture/security.md).
- Firmware does not prevent booted software from reading the device key: the key lives in NVRAM and mainline does not use TPM measurement.
- Firmware does not manage runtime identity: key generation, rotation and binding are orchestrated by the platform control plane.
- The firmware repository is GPL-2.0; third-party driver adaptations may carry stricter licensing, described in its `patches/README`.

## Related Pages

- [Build and Artifacts](./build.md)
- [Provisioning](./provision.md)
- [Firmware Contract](../reference/firmware-contract.md)
- [Boot Chain](../concepts/boot-chain.md)
- Firmware repository documentation: <https://github.com/dutyc/kurrent-firmware>
