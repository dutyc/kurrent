# Secure Boot Boot

This page explains how the first-hop firmware and the boot HTTPS endpoint get verifiable trust anchors, and how they combine with TPM attestation. Admission requires `sb=1`, so Secure Boot is a prerequisite of the platform rather than an option.

## Trust anchors

The boot chain has three trust anchors that do not replace one another:

| Object | Anchor | Failure consequence |
| --- | --- | --- |
| First-hop binary (iPXE) | Site signing certificate in the target's UEFI `db` | Unsigned or mismatched firmware never runs |
| Boot HTTPS (4443) | Boot CA embedded in the signed firmware (`TRUST=` / `CERT=`) | Server certificates outside that CA are rejected; there is no TOFU fallback |
| Device identity | TPM AK and PCR 0/2/4/7 quote | No AK or a failing quote means no credentials |

Changing the firmware signing key requires updating the target's `db`; changing the boot CA means reflashing the firmware, because the fingerprint is baked into the image. Changing firmware or Secure Boot policy changes the PCRs, so attestation is rejected as `pcr_mismatch` until an administrator reviews and approves the new baseline.

## Control-plane artifacts

The control plane generates these idempotently at startup, separate from the component CA roots:

- `state/pki/ca/boot/ca.crt|ca.key`: the boot CA, valid for 10 years, used only to sign boot endpoint certificates.
- `state/certs/boot-server.crt|boot-server.key`: the 4443 leaf certificate, valid for 1 year, with SANs mirroring `spec.serverCert.san`; it is reissued automatically when less than 30 days remain, without changing the boot CA.
- The nginx 4443 server proxies only `/boot-vars`, `/devices/report`, and `/devices/challenge`, does not request client certificates, and returns 404 for any other path; the management plane, WebUI, and `/api/cp/` are not on this port.

Adjust the host port with `KURRENT_BOOT_HTTPS_PORT`, and keep it identical to `BOOT_HTTPS_PORT` at firmware build time: the port is baked into the signed image, and `secureboot.port_matches` in `/api/cp/boot-assets` verifies it.

## Building signed firmware

The firmware repository builds signed artifacts with `build/build-sb.sh`:

```bash
cd kurrent-firmware
SB_KEY=/path/to/artifact-signing.key \
SB_CERT=/path/to/artifact-signing.crt \
BOOT_CA_CERT=/path/to/boot-ca.crt \
BOOT_HTTPS_PORT=4443 \
./build/build-sb.sh
```

Required inputs:

- `SB_KEY` / `SB_CERT`: the offline code-signing key and certificate; the certificate must be enrolled in the target's `db`.
- `BOOT_CA_CERT`: the control plane's boot CA certificate (`state/pki/ca/boot/ca.crt`), pinned into the firmware as `TRUST=` / `CERT=`.
- `CONTROLLER_IP`: set it when the control-plane address is fixed; leave it empty to use DHCP `next-server`.

The script embeds `auto.ipxe`, `sb-boot.ipxe.cfg` (port and controller address), and `sb-menu.ipxe`, then signs the result with `sbsign`. Artifacts land in `dist/secureboot/`: `snponly-sb.efi` and `manifest.json`; the manifest records the artifact hash, signer, build time, port, and boot CA fingerprint.

The script is the only entry point for the static boot logic: changing the menu, a variable, or a jump requires a rebuild and a new signature; the logic cannot be replaced on its own.

## Deploying to the control plane

1. Place `dist/secureboot/snponly-sb.efi` and `manifest.json` in the control-plane host's repository `tftp/secureboot/`. That directory is the source for artifact verification and export, and is not committed.
2. Have TFTP deliver the signed image as the first hop: `install -m 0644 tftp/secureboot/snponly-sb.efi tftp/snponly.efi`, then `docker compose restart kurrent-dnsmasq` so the file-level mount points at the new inode.
3. Query `/api/cp/boot-assets`; `secureboot.status` should be `ok`: the image hash matches the manifest, the port matches the deployment, and the boot CA exists with a matching fingerprint. The WebUI boot-chain page shows the Secure Boot row whenever artifacts are present.

## Provisioning the target trust store

The signing certificate must be in the target's `db` first, otherwise the firmware refuses to run (a one-time provisioning step, not a per-boot dependency):

- Generate the offline signing key: `tools/secureboot/keygen-offline.sh ~/offline/kurrent-sb`. Keep the private key offline; the script refuses to write it into the repository or a container.
- **USB/BIOS (supported path, no BMC required)**: `tools/secureboot/export-usb.sh <usb> <artifact-signing.crt> <snponly-sb.efi> <boot-ca.crt>` produces an import package containing only public certificates; import `db-cert.cer` in the target's Key Management.
- **Redfish (optional, requires BMC and vendor support)**: `KURRENT_BMC_USER=... KURRENT_BMC_PASSWORD=... tools/secureboot/enroll-redfish.sh --bmc <bmc> --cert <artifact-signing.crt> --enable-secureboot`. The script reads the Secure Boot and `db` state, confirms the Microsoft UEFI CA is present, then imports the certificate idempotently and reads the fingerprint back; `--dry-run` only reads. The tool has only been validated at protocol level (simulated BMC); confirm vendor behavior per model.
- Target: disable CSM, enable Secure Boot, and verify the state and `db` with `tools/secureboot/check-host.sh <artifact-signing.crt>`.

The loader after `sanboot` is decided by the distribution signing chain: the golden disk's ESP shim/GRUB/kernel must be vendor-signed (the Ubuntu 26.04 golden disk uses a Microsoft UEFI CA 2011 signed shim → Canonical-signed GRUB and kernel), and the target `db` must contain that Microsoft CA. An unsigned GRUB is rejected after `sanboot`.

## Verification

- The device reports `sb=1` with a valid TPM AK before it can enter the device pool.
- The `secureboot` section of `/api/cp/boot-assets` is `ok`.
- The first cold boot reaches the iPXE menu, passes attestation, and reaches `login:`; negative cases are in [Troubleshooting](../guide/troubleshooting.md).

## Boundaries

- Secure Boot covers only the first iPXE segment and the boot HTTPS endpoint; the loader after `sanboot` is decided by the distribution signing chain.
- There is no rollback protection: a previously signed firmware still verifies, so rollback control depends on the release process.
- Rotating the boot CA equals reflashing the firmware; firmware upgrades or trust-store changes alter the PCRs and require an administrator to approve a new baseline.
- Some PXE firmware does not verify the downloaded NBP; prefer local media or controlled HTTP boot where possible. Per-model validation on real hardware is not complete.

## Related pages

- [Firmware Build and Deployment](./build-deploy.md)
- [Identity and Access](../concepts/identity.md)
- [Boot Chain](../concepts/boot-chain.md)
- [Security Model](../architecture/security.md)
