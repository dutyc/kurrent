# Deployment and Provisioning

A signed artifact enters the boot path over a PXE segment (or a local ESP). A target in Setup Mode (or with Secure Boot off) boots it over the network, the platform enrollment window writes PK / KEK / db into firmware and reboots, and the same image then runs under signature verification. After deployment, the device should be pooled within the registration window.

## Goals and Prerequisites

- the target satisfies the [bare-metal requirements](../setup/host-prerequisites.md#bare-metal-requirements): x86_64 UEFI, TPM 2.0, and no vendor keys locked in (Setup Mode or Secure Boot off); legacy BIOS (Legacy/CSM) boot is no longer supported; machines locked to vendor keys must use the vendor flow and are outside this procedure;
- the control plane, dnsmasq and the PXE segment are set up as described in [Deployment](../setup/deployment.md);
- signed artifacts and `manifest.json` were produced as described in [Build](./build.md) (the image embeds the platform key descriptors and the artifact signing certificate).

## Deployment Paths

| Condition | Path |
| --- | --- |
| Mainboard has UEFI Network Boot / PXE | PXE: the platform TFTP serves the signed image and the platform enrollment window writes the keys (default) |
| No PXE entry | Boot the `direct-uefi` signed artifact from a local ESP; keys are still written through the platform enrollment window |
| Vendor keys already locked | Use the vendor management flow; outside this procedure |

### PXE

1. Obtain `dist/secureboot/snponly-sb.efi`, `dist/secureboot/ipxe-sb.efi` (built with `KURRENT_SB_TARGET=ipxe`) and `manifest.json`.
2. Install them into the main repository's TFTP root and restart dnsmasq:

   ```bash
   sudo install -d -m 0755 /path/to/kurrent/tftp/secureboot
   sudo install -m 0644 dist/secureboot/snponly-sb.efi dist/secureboot/ipxe-sb.efi \
     dist/secureboot/manifest.json /path/to/kurrent/tftp/secureboot/
   sudo install -m 0644 dist/secureboot/snponly-sb.efi /path/to/kurrent/tftp/snponly.efi
   sudo install -m 0644 dist/secureboot/ipxe-sb.efi /path/to/kurrent/tftp/ipxe.efi
   cd /path/to/kurrent
   eval "$(kurrent deployment show -o env)"
   docker compose restart kurrent-dnsmasq
   ```

   `tftp/snponly.efi` and `tftp/ipxe.efi` are both file-level mounts; restart the container after replacing them, or it keeps the old inode, and dnsmasq cannot start if either is missing. `tftp/secureboot/` serves as the artifact source for `/boot-assets` verification and export.

   The control plane selects the first hop by device `boot_firmware`: default devices take `snponly.efi`, `boot_firmware=full` devices take the all-driver `ipxe.efi`; both can coexist on one PXE segment.

3. Open the platform enrollment window on the control plane (`POST /settings/platform-enrollment`; the WebUI devices page exposes the same switch).
4. Boot the device over the network: the image fetches `/boot/phase`, runs `sbenroll` to write the platform keys, reports the result and reboots; after reboot Secure Boot is enforced, the same image runs under signature verification, and report and challenge complete over 4443. Close the window afterwards.

TFTP serves only the first-stage firmware (`snponly.efi`, or the all-driver `ipxe.efi` selected per device by `boot_firmware`) and the `boot.ipxe` pointer; the signed first stage embeds the static scripts, boot CA and boot HTTPS port, and leaves the controller address out of the image by default, so later requests go over 4443 HTTPS trusting only that boot CA. The PXE segment must be isolated as described in the [Security Model](../concepts/architecture/security.md).

### Local Media

Use the `direct-uefi` signed artifact (e.g. `snponly-sb.efi`): it embeds the same static scripts as the PXE path and writes the keys through the platform enrollment window.

USB / ESP:

1. prepare a FAT32 partition (a USB stick or the local ESP);
2. create `EFI/BOOT/`;
3. copy `dist/direct-uefi/snponly-sb.efi` (or `ipxe-sb.efi`) to `EFI/BOOT/BOOTX64.EFI`;
4. select the USB/ESP in the device boot menu.

If the mainboard only recognizes the fixed ESP, place the file at the same path on the internal disk and select it in the boot entries.

GRUB chainload:

```text
menuentry "Kurrent firmware" {
    chainloader /boot/efi/EFI/kurrent/snponly-sb.efi
}
```

## First Boot and Identity

The first network boot writes the platform keys first (`sbenroll`, only while the window is open), then firmware:

1. runs `akgen` to create the AK inside the TPM (non-exportable private key);
2. exports the AK public key, name and algorithm with `akpub`;
3. reports the fingerprint, AK and Secure Boot state to `/devices/report`;
4. obtains a nonce from `/devices/challenge` on every boot and produces a quote over PCR 0/2/4/7 with `quote`;
5. fetches boot variables over 4443, trusting only the boot CA pinned at build time.

A device must report `sb=1` to be pooled; with Secure Boot off, enrollment-period reports write no ledger entry and leave only a rejection audit record.

Identity semantics:

- device identity is carried by the AK inside the TPM; the boot HTTPS trust anchor is the boot CA embedded in the signed firmware; updating firmware or replacing TFTP files does not clear the AK;
- clearing the TPM or replacing the mainboard loses the AK, equivalent to a new device identity: delete the entry and re-pool during a registration window;
- the platform keys (PK / KEK / db) are written by the platform enrollment window on the first network boot, and the PK is fixed once the window closes; after a reinstall or firmware replacement the window is run again, with no per-machine certificate import;
- the boot CA only issues the 4443 leaf: rotating it requires rebuilding the signed artifacts, and the platform keys are unaffected;
- new hardware or a whole-machine replacement forms a new device; system disks and the Worker identity stay in the control plane and move by rebinding.

NVRAM cleanup should go through a firmware interactive environment or a controlled provisioning flow. AK lifecycle, quote format and trust boundaries are in the [Firmware Contract](../reference/firmware-contract.md).

## Verification

| Check | Expectation |
| --- | --- |
| Device boot | Reaches the iPXE menu and shows that Worker's system disks |
| WebUI → Devices | The device is pooled within the registration window |
| WebUI → Boot chain / `/boot-assets` | Firmware `status=ok` with all capabilities; `secureboot.status=ok` |
| Control-plane logs / audit | Records for `/devices/report`, `/devices/challenge` and `/boot-vars` |
| Data plane | After selecting the default disk, the NVMe/TCP connection completes and the system boots |

`/boot-assets` requires `status=ok` with TPM identity and quote capabilities. `incomplete` usually means the TFTP root holds stock iPXE. When `secureboot.status` is not `ok`, check the image hash, port and boot CA separately.

QEMU/OVMF verification needs KVM, OVMF firmware and an available nvmet target. The firmware repository provides scripts:

```bash
sudo AUTH=1 bash test/nvmet-setup.sh
python3 test/cred-server.py &
# Test script: test/nvmeof-auth-test.ipxe
bash test/run-qemu-auth.sh
```

Acceptance: the serial console shows authentication success, Property Set, I/O queues and GRUB output; the target shows two controller records, one for the firmware segment and one for the kernel segment; in packet captures the first byte of each client TCP stream is `0x16`, the plaintext port carries zero packets, and the payload contains no PSK or DH-HMAC-CHAP key plaintext. `test/cred-server.py` is for verification only and must not serve as a production credential endpoint.

## Update and Rollback

- replacing `tftp/snponly.efi` and restarting dnsmasq updates the PXE first stage;
- local-media devices need the new `direct-uefi` artifact written again;
- keep the previous artifact for rollback; NVRAM identity and fingerprints do not change with firmware files;
- after an update, verify at least one device reaches the menu and boots a system once.

## Common Failures

- **No DHCP response**: is the device on the PXE segment; check dnsmasq `interface=` and firewall ports 67/69.
- **No TFTP download after DHCP**: does the TFTP root mount only the first-stage images (`snponly.efi` / `ipxe.efi`) and `boot.ipxe`; check file permissions.
- **Stuck in firmware boot**: platform enrollment not completed (no PK / KEK / db), unsigned artifact, or the image signed by a certificate other than the one enrolled; verify the platform enrollment ledger, `sbverify --cert` and `tools/secureboot/check-host.sh`, and confirm the artifact matches the architecture and form.
- **Boot HTTPS certificate error**: the `BOOT_CA_CERT` used at build time differs from `state/pki/ca/boot/ca.crt`, or the 4443 leaf SAN does not cover the address the device uses, or the port differs from `BOOT_HTTPS_PORT`.
- **Device not pooled**: is the registration window open; does the report carry a valid TPM AK; is the device revoked.
- **Empty menu or boot loop**: is the device bound to a Worker; does the Worker have a system disk and default disk.
- **Data-plane connection failure**: `tlshd`, kernel TLS support, port 4421 and credential push status.

## What's next

- Back to the [Firmware Overview](./index.md)
- Artifacts and signing: [Build](./build.md)
- Trust store provisioning: [Secure Boot Provisioning](./secure-boot.md)
- Boot-chain admission: [Boot Chain](../concepts/boot-chain.md)
- Troubleshooting: [Troubleshooting](../tasks/troubleshooting.md)
