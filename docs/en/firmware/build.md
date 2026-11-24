# Build

Firmware is built on the `main` branch of the separate `kurrent-firmware` repository: fetch the pinned upstream iPXE baseline, apply patches, embed scripts and produce UEFI artifacts. The artifacts feed the main repository's TFTP root and boot chain.

## Prerequisites

- a Linux x86_64 build host with `git`, `make`, `gcc`, `openssl`, `python3` and `sbsigntool` (`sbsign`/`sbverify`);
- network access to the upstream iPXE repository; the first build caches the sources;
- for signed builds, a signing key, a signing certificate and the boot CA certificate; the port and controller address have defaults, described below.

## Signed Build

```bash
git clone https://github.com/dutyc/kurrent-firmware
cd kurrent-firmware
SB_KEY=/path/to/artifact-signing.key \
SB_CERT=/path/to/artifact-signing.crt \
BOOT_CA_CERT=<state>/control-plane/state/pki/ca/boot/ca.crt \
./build/build-sb.sh
```

A signed build requires a signing key, a signing certificate and the boot CA certificate. The port and controller address may be omitted: the port then defaults to `4443`, while the address comes from the DHCP `next-server`; set `CONTROLLER_IP` only when the address must be fixed inside the image.

`build-sb.sh` builds on the ordinary build, embeds the static scripts, the boot CA and the boot HTTPS port, and produces a first stage signed with `sbsign`.

To write the platform keys on the device's first network boot, also provide `SB_PK_KEY` / `SB_PK_CERT` / `SB_KEK_KEY` / `SB_KEK_CERT` (as a group): the script generates the signed PK / KEK / db descriptors for `sbenroll`, and db automatically includes the artifact signing certificate and `SB_MS_UEFI_CA_CERT` (default `build/certs/MicrosoftUEFI-CA-2011.pem`). Without this group the image carries no enrollment data and cannot perform platform enrollment.

Build inputs, the manifest and provisioning steps are in [Secure Boot Provisioning](./secure-boot.md).

The ordinary build `./build/build.sh` does not sign; its artifacts are for local debugging and research only, and Secure Boot targets refuse to load them.

```bash
./build/build.sh
UPSTREAM_COMMIT=<sha> ./build/build.sh   # Pin an upstream baseline; patches must remain compatible
JOBS=8 ./build/build.sh                  # Parallelism
```

## Pipeline

1. fetch upstream iPXE and check out the pinned baseline (currently `e6e51ccb`);
2. apply `patches/*.patch` in filename order; a patch that does not apply fails the build;
3. install `embed/auto.ipxe` as the built-in script for `direct-uefi`;
4. build the UEFI targets and copy them to `dist/`;
5. generate `dist/SHA256SUMS`.

Patch baselines and the upgrade flow are in the firmware repository's `patches/README`. Before upgrading upstream, confirm every patch still applies and rerun QEMU and whole-machine verification.

## Artifacts and Verification

| Artifact | Form | Purpose |
| --- | --- | --- |
| `pxe-uefi/ipxe.efi` | PXE network boot | Loaded by the mainboard PXE chain; scripts come from DHCP |
| `pxe-uefi/snponly.efi` | PXE network boot | Default first stage |
| `pxe-uefi/ipxe-debug.efi`, `snponly-debug.efi` | Same, debug | NIC initialization and boot diagnosis |
| `direct-uefi/ipxe.efi` | Direct UEFI boot, embedded script | USB/ESP, local disk or GRUB chainload |
| `direct-uefi/ipxe-debug.efi` | Same, debug | Troubleshooting |
| `direct-uefi/snponly.efi` | Direct UEFI boot | Local media boot |

Selection rules:

| Scenario | Choice |
| --- | --- |
| Mainboard supports UEFI Network Boot | `pxe-uefi/snponly.efi`, the default first stage |
| No PXE entry; USB/ESP boot required | `direct-uefi/ipxe.efi` |
| NIC initialization fails | The matching `-debug` artifact |

The main repository uses `tftp/snponly.efi` by default. Different models may need different first-stage artifacts: the control plane selects `snponly.efi` (default) or the all-driver `tftp/ipxe.efi` per device through DHCP `boot_firmware`, so both can coexist on one PXE segment. The all-driver first hop uses the existing `direct-uefi/ipxe.efi`: it embeds `auto.ipxe` (DHCP, then `tftp://${next-server}/boot.ipxe`), so the same script serves both as a TFTP first hop and as local-media boot.

`tftp/*.efi` is not committed; place the files at deployment time: `pxe-uefi/snponly.efi` → `tftp/snponly.efi`, `direct-uefi/ipxe.efi` → `tftp/ipxe.efi`. Both are in dnsmasq's file-level mounts, so neither may be missing.

The table above describes `build.sh`; at deployment the first stages are replaced by `build-sb.sh` artifacts with the same form as `pxe-uefi/snponly.efi`: `dist/secureboot/snponly-sb.efi` over `tftp/snponly.efi`, and `dist/secureboot/ipxe-sb.efi` (built with `KURRENT_SB_TARGET=ipxe`) over `tftp/ipxe.efi`.

```bash
cd dist
sha256sum -c SHA256SUMS
```

Signed artifact hashes come from `artifact_sha256` in `dist/secureboot/manifest.json`, and `/boot-assets` compares them with the actual image hash. Release assets use flat names (such as `pxe-uefi-snponly.efi`) while the checksum file keeps the `pxe-uefi/` directory layout; place downloads in that layout before verifying, or compare the target file's digest alone.

## What's next

- Back to the [Firmware Overview](./index.md)
- Deploying artifacts and first boot: [Deployment and Provisioning](./deploy.md)
- Signing and the trust store: [Secure Boot Provisioning](./secure-boot.md)
- Patch set and NIC matrix: [Firmware Platform Reference](../reference/firmware-platform.md)
