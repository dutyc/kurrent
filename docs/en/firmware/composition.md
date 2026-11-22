# Firmware Composition

Firmware is not a single binary. It is the build result of a pinned baseline, a patch set and embedded content.

## Upstream Baseline

The firmware repository does not store iPXE sources. The build fetches upstream iPXE at a pinned commit and applies patches on top of it; the current baseline is `e6e51ccb`. The baseline is pinned together with the firmware version, and upgrading it means the whole patch set must be re-applied, rebuilt and re-verified.

## Patch Set

Patches are applied in filename order and cover the following capabilities:

- NIC drivers: native adaptation for Realtek RTL8125 and RTL8126, with debug targets keeping the full driver set;
- boot path: taking over SNP/NII/MNP devices during local-media boot;
- device information: SMBIOS memory fields and PCI NIC chip names;
- data plane: the NVMe over TCP SAN driver, DH-HMAC-CHAP authentication sequencing and `hostnqn`;
- device identity: TPM identity commands and the EFI variable NVS backend;
- boot trust: TOFU fingerprint pinning and verification, and the TLS 1.3 PSK client;
- hand-over: the NBCT ACPI table and kernel credential hand-over.

If any patch fails to apply to the pinned baseline, the build fails and no mixed version is produced. Per-patch numbering and responsibilities are in the [Firmware Platform Reference](../reference/firmware-platform.md#patch-set). Patch licensing boundaries are not uniform; read `patches/README` in the firmware repository before redistributing.

## Embedded Content

Two kinds of content are written into the image at build time.

A regular build embeds `embed/auto.ipxe` as the built-in script of `direct-uefi` artifacts. A signed build additionally embeds three static boot scripts, the boot CA certificate and the boot HTTPS port, then signs the result with `sbsign`; the control-plane address is left out by default.

Static scripts, trust anchors and the port are therefore part of the image: changing the menu, the variables, the boot CA or the port requires a rebuild and a new signature, and none of them can be replaced on their own. Signed build inputs and outputs are in [Secure Boot Provisioning](./secure-boot.md).

## Build Forms

The firmware repository provides two build commands:

- `build/build.sh` produces unsigned artifacts for local debugging and research; Secure Boot targets refuse to load them;
- `build/build-sb.sh` requires `SB_KEY`, `SB_CERT`, the boot CA certificate and the boot port, and produces signed artifacts plus `manifest.json`; this is the form used for deployment.

## Artifact Forms

The build produces 7 UEFI forms, split by boot path into PXE and direct-uefi groups, each with release, debug and `snponly` variants. PXE artifacts are loaded by the mainboard network boot and receive their scripts over DHCP; direct-uefi artifacts carry a built-in script for USB, ESP, local-disk or GRUB chain loading.

The artifact list, selection rules and verification are in [Build](./build.md#artifacts-and-verification).

## Driver Strategy

Firmware prefers its built-in native drivers, covering server NICs mainly at 2.5G and above. NICs with their own UEFI network stack can use the `snponly` artifact directly without porting a driver. USB NICs and modern wireless NICs are out of scope.

The NIC matrix, known issues and recorded tests are in the [Firmware Platform Reference](../reference/firmware-platform.md#nic-support).

## What's next

- [Firmware Overview](./index.md)
- [Boot Trust](./trust.md)
- [Firmware Platform Reference](../reference/firmware-platform.md)
