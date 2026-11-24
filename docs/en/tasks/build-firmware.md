# Build Signed Firmware

Firmware is built in the separate [kurrent-firmware](https://github.com/dutyc/kurrent-firmware) repository: pull upstream iPXE at a pinned baseline, apply the patch set in order, embed the static script and trust anchors, and produce UEFI artifacts. The first hop on the deployment path uses the signed artifacts from `build-sb.sh`.

![Firmware pipeline: from the upstream iPXE baseline to a booting device](/diagrams/en/firmware-pipeline.svg)

## Before you begin

- A Linux x86_64 build machine with `git`, `make`, `gcc`, `openssl`, `python3` and `sbsigntool` (`sbsign` / `sbverify`).
- Access to the upstream iPXE repository; the first build caches the source.
- The signed build needs the artifact signing key and certificate plus the boot CA certificate; writing platform keys on the first network boot additionally needs the offline PK / KEK keys and certificates.

## Signed build

```bash
git clone https://github.com/dutyc/kurrent-firmware
cd kurrent-firmware
SB_KEY=/path/to/artifact-signing.key \
SB_CERT=/path/to/artifact-signing.crt \
BOOT_CA_CERT=<state-root>/control-plane/state/pki/ca/boot/ca.crt \
SB_PK_KEY=/path/to/pk.key SB_PK_CERT=/path/to/pk.crt \
SB_KEK_KEY=/path/to/kek.key SB_KEK_CERT=/path/to/kek.crt \
./build/build-sb.sh
```

| Variable | Required | Meaning |
| --- | --- | --- |
| `SB_KEY` / `SB_CERT` | Yes | Artifact signing key and certificate |
| `BOOT_CA_CERT` | Yes | The control plane's boot CA certificate, embedded in the image |
| `BOOT_HTTPS_PORT` | No | Boot HTTPS port, default `4443`, embedded in the image and must match the control plane |
| `CONTROLLER_IP` | No | Pin the controller address; when omitted the device takes it from the DHCP `next-server` |
| `SB_PK_KEY` / `SB_PK_CERT` / `SB_KEK_KEY` / `SB_KEK_CERT` | As a group | Offline PK / KEK keys and certificates for platform key enrollment; all four are set together or omitted together. Without them the image carries no enrollment data and `sbenroll` always reports `failed` |
| `SB_MS_UEFI_CA_CERT` | No | The Microsoft UEFI CA certificate written into `db`, default `build/certs/MicrosoftUEFI-CA-2011.pem` |
| `SB_OWNER_GUID` | No | Owner GUID of the platform keys, default `a0baa8a3-041d-48a8-bc87-c36d121b5e3d` |
| `KURRENT_SB_TARGET` | No | `snponly` (default) or `ipxe`, selecting the build target |

The script generates the `sbenroll` PK / KEK / db enrollment descriptors from these inputs; `db` automatically contains the artifact signing certificate and the Microsoft UEFI CA. Private keys participate only in build-time signing and stay out of the repository and the image. The output is `dist/secureboot/snponly-sb.efi` (or `ipxe-sb.efi` with `KURRENT_SB_TARGET=ipxe`) plus `manifest.json`.

The unsigned `./build/build.sh` output is only for local debugging and research; Secure Boot enabled machines refuse to load it. `UPSTREAM_COMMIT=<sha>` pins the upstream baseline and `JOBS` sets parallelism.

## Pipeline

1. Pull upstream iPXE and check out the pinned baseline (currently `e6e51ccb`).
2. Apply `patches/*.patch` in file order; any patch that fails to apply fails the build.
3. Install `embed/auto.ipxe` as the built-in script of the `direct-uefi` artifacts; the signed build then embeds the static boot scripts, the boot CA and the boot port, and signs with `sbsign`.
4. Build the UEFI targets and copy them into `dist/`.
5. Produce `dist/SHA256SUMS` and, for the signed build, `manifest.json`.

For the patch baseline, per-patch responsibilities and the upgrade process see the firmware repository's `patches/README` and the [Firmware Platform Reference](../reference/firmware-platform.md#patch-set).

## Artifacts

The unsigned `build.sh` produces:

| Artifact | Form | Use |
| --- | --- | --- |
| `pxe-uefi/ipxe.efi` | PXE network boot | Loaded by the board's PXE chain; scripts come from DHCP |
| `pxe-uefi/snponly.efi` | PXE network boot | Default first hop |
| `pxe-uefi/ipxe-debug.efi`, `snponly-debug.efi` | Same, debug | NIC initialization and boot tracing |
| `direct-uefi/ipxe.efi` | Direct UEFI boot, built-in script | USB/ESP, local disk or GRUB chain loading |
| `direct-uefi/ipxe-debug.efi` | Same, debug | Fault isolation |
| `direct-uefi/snponly.efi` | Direct UEFI boot | Local-media boot |

| Scenario | Choice |
| --- | --- |
| Board supports UEFI Network Boot | `pxe-uefi/snponly.efi`, the default first hop |
| Board has no PXE entry and needs USB/ESP boot | `direct-uefi/ipxe.efi` |
| NIC initialization fails | The corresponding `-debug` artifact |

The signed `build-sb.sh` artifacts correspond to the table above: `dist/secureboot/snponly-sb.efi` matches `pxe-uefi/snponly.efi`, and the `KURRENT_SB_TARGET=ipxe` `ipxe-sb.efi` matches the all-driver first hop. The main repository's TFTP root carries only these two signed artifacts; see [Deliver Signed Firmware](./deploy-firmware.md).

## Verification

```bash
cd dist && sha256sum -c SHA256SUMS
sbverify --cert <artifact.crt> dist/secureboot/snponly-sb.efi
```

The signed artifact's hash is authoritative in `manifest.json` under `artifact_sha256`; the control plane's `/boot-assets` checks it against the actual image hash, the port and the boot CA fingerprint. Release-page assets use flat names while the checksum file keeps the directory layout.

## Next steps

- Deliver to the control plane: [Deliver Signed Firmware](./deploy-firmware.md)
- Write platform keys on the first boot: [Provision Target Trust Stores](./enroll-platform-keys.md)
- Trust anchors: [Boot Trust](../concepts/boot-chain/trust.md)
- Patch set and NIC matrix: [Firmware Platform Reference](../reference/firmware-platform.md)
