# Secure Boot Provisioning

Admission requires `sb=1`, so Secure Boot is a prerequisite of the platform. `build-sb.sh` builds the signed artifact, which is then delivered to the control plane and its signing certificate provisioned into the target trust store. The trust anchors and rotation effects are in [Boot Trust](./trust.md).

## Control-plane artifacts

The control plane generates these idempotently at startup, separate from the component CA roots:

- `state/pki/ca/boot/ca.crt|ca.key`: the boot CA, valid for 10 years, used only to sign boot endpoint certificates.
- `state/certs/boot-server.crt|boot-server.key`: the 4443 leaf certificate, valid for 1 year, with SANs mirroring `spec.serverCert.san`; it is reissued automatically when less than 30 days remain, without changing the boot CA.
- The nginx 4443 server proxies only `/boot-vars`, `/devices/report`, and `/devices/challenge`, does not request client certificates, and returns 404 for any other path; the management plane, WebUI, and `/api/cp/` are not on this port.

Adjust the host port with `KURRENT_BOOT_HTTPS_PORT`, and keep it identical to `BOOT_HTTPS_PORT` at firmware build time: the port is embedded in the signed image, and `secureboot.port_matches` in `/api/cp/boot-assets` verifies it.

## Building signed firmware

The firmware repository builds signed artifacts with `build/build-sb.sh`:

```bash
cd kurrent-firmware
SB_KEY=/path/to/artifact-signing.key \
SB_CERT=/path/to/artifact-signing.crt \
BOOT_CA_CERT=/path/to/boot-ca.crt \
./build/build-sb.sh
```

A signed build requires a signing key, a signing certificate and the boot CA certificate. The port and controller address may be omitted: the port defaults to `4443`, and the address comes from the DHCP `next-server` unless `CONTROLLER_IP` fixes it inside the image. Changing the boot CA, the port or a fixed address requires a rebuild.

The script embeds `auto.ipxe`, `sb-boot.ipxe.cfg`, and `sb-menu.ipxe`, then signs the result with `sbsign`. Artifacts land in `dist/secureboot/`: `snponly-sb.efi` and `manifest.json`; the manifest records the artifact hash, signer, build time, port, and boot CA fingerprint.

The same image can therefore be pre-provisioned across devices within one deployment (same boot CA, same port). The script is the only entry point for the static boot logic: changing the menu, a variable, or a jump requires a rebuild and a new signature; the logic cannot be replaced on its own.

## Deploying to the control plane

1. Place `dist/secureboot/snponly-sb.efi` and `manifest.json` in the control-plane host's repository `tftp/secureboot/`. That directory is the source for artifact verification and export, and is not committed.
2. Have TFTP deliver the signed image as the first hop: `install -m 0644 tftp/secureboot/snponly-sb.efi tftp/snponly.efi`, then `docker compose restart kurrent-dnsmasq` so the file-level mount points at the new inode. The all-driver first hop follows the same step: the firmware repo builds `ipxe-sb.efi` with `KURRENT_SB_TARGET=ipxe`; install it over `tftp/ipxe.efi` for devices with `boot_firmware=full`.
3. Query `/api/cp/boot-assets`; `secureboot.status` should be `ok`: the image hash matches the manifest, the port matches the deployment, and the boot CA exists with a matching fingerprint. The WebUI boot-chain page shows the Secure Boot row whenever artifacts are present.

## Provisioning the target trust store

The signing certificate must be in the target's `db` first, otherwise the firmware refuses to run. For a fleet, the default path is network enrollment: a machine in Setup Mode (or with Secure Boot disabled) network-boots the same signed image; the embedded `sbenroll` command writes the platform keys (PK / KEK / db) and reboots, after which the firmware verifies the signature and production boot continues.

1. Open the platform enrollment window on the control plane (`POST /settings/platform-enrollment`; the WebUI devices page exposes the same switch).
2. Network-boot the signed image on each machine: after writing the keys it reboots and the MAC appears in the devices page "Platform enrollment" list.
3. Close the window when done: new machines stop enrolling, and enrolled devices and the list are kept.

The platform keys and signed descriptors are supplied at image build time (firmware repo `build-sb.sh` inputs `SB_PK_*` / `SB_KEK_*` / `SB_MS_UEFI_CA_CERT`). Keep the private keys offline; the image embeds only public certificates and signature descriptors. Writes go PK → KEK → db, with db containing the artifact signing certificate and the Microsoft UEFI CA 2011. Run enrollment on a controlled segment: in Setup Mode any host on the same segment could write keys; once the window is closed the PK is fixed and later changes require the offline keys.

Machines that ship locked to vendor keys with Secure Boot enabled cannot enroll this way and must use the vendor's own management flow, which is outside this procedure. Verify target state with `tools/secureboot/check-host.sh`.

The loader after `sanboot` is decided by the distribution signing chain: the master disk's ESP shim/GRUB/kernel must be vendor-signed (the Ubuntu 26.04 master disk uses a Microsoft UEFI CA 2011 signed shim → Canonical-signed GRUB and kernel), and the target `db` must contain that Microsoft CA. An unsigned GRUB is rejected after `sanboot`.

## Verification

- The device reports `sb=1` with a valid TPM AK before it can enter the device pool.
- The `secureboot` section of `/api/cp/boot-assets` is `ok`.
- The first cold boot reaches the iPXE menu, passes attestation, and reaches `login:`; negative cases are in [Troubleshooting](../tasks/troubleshooting.md).

## What's next

- [Firmware Overview](./index.md), [Firmware Composition](./composition.md)
- [Boot Trust](./trust.md)
- [Build](./build.md), [Deployment and Provisioning](./deploy.md)
- [Identity and Access](../concepts/identity.md)
- [Boot Chain](../concepts/boot-chain.md)
- [Security Model](../concepts/architecture/security.md)
