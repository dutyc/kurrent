# Deliver Signed Firmware

Signed artifacts are delivered to devices over the TFTP first hop (the default path) or via local media. After delivery, the device completes its first boot and joins the pool within the platform enrollment and registration windows.

## Before you begin

- The control plane is deployed and the PXE network is ready (see [Deployment](../setup/deployment.md)).
- You have `snponly-sb.efi`, `ipxe-sb.efi` and `manifest.json` from [Build Signed Firmware](./build-firmware.md).
- The target devices meet the [bare-metal device requirements](../setup/host-prerequisites.md#bare-metal-device-requirements): x86_64 UEFI, TPM 2.0, in Setup Mode or with Secure Boot off; machines locked to vendor keys use the vendor flow.

## Deliver to the control plane

1. Put `snponly-sb.efi`, `ipxe-sb.efi` and `manifest.json` into the main repository's `tftp/secureboot/`. That directory is the verification and export source for `/boot-assets` and is not committed.
2. Overlay the TFTP first hops and restart dnsmasq:

   ```bash
   sudo install -m 0644 tftp/secureboot/snponly-sb.efi tftp/snponly.efi
   sudo install -m 0644 tftp/secureboot/ipxe-sb.efi tftp/ipxe.efi
   eval "$(kurrent deployment show -o env)"
   docker compose restart kurrent-dnsmasq
   ```

   `tftp/snponly.efi` and `tftp/ipxe.efi` are dnsmasq file-level mounts; after replacing them the container must be restarted or it keeps the old inode. dnsmasq fails to start if either is missing.

3. Verify the artifacts: `GET /api/cp/boot-assets` should report `secureboot.status=ok` (image hash matches the manifest, the port matches the deployment, and the boot CA fingerprint matches).

The control plane selects the first hop per device's `boot_firmware`: default devices take `snponly.efi`, `boot_firmware=full` devices take the all-driver `ipxe.efi`; the two can be mixed on one PXE network.

## Delivery paths

| Condition | Path |
| --- | --- |
| Board has a UEFI network boot entry | PXE: TFTP delivers the signed first hop, the default path |
| Board has no PXE entry | Local media: the `direct-uefi` signed artifact written to USB / ESP |
| Factory-locked vendor keys | The vendor management flow |

### Local media

The `direct-uefi` artifacts embed the same static script as PXE, so they perform key enrollment the same way while the platform enrollment window is open.

1. Prepare a FAT32 partition (USB stick or local ESP).
2. Create the `EFI/BOOT/` directory.
3. Copy `dist/direct-uefi/snponly-sb.efi` (or `ipxe-sb.efi`) to `EFI/BOOT/BOOTX64.EFI`.
4. Select that media in the device boot menu.

If the board only recognizes the fixed ESP, place the file at the same path on the built-in disk. GRUB chain loading:

```text
menuentry "Kurrent firmware" {
    chainloader /boot/efi/EFI/kurrent/snponly-sb.efi
}
```

## First boot

After delivery, the device's first network boot completes these steps in order:

1. Platform key enrollment: while the platform enrollment window is open, `sbenroll` writes PK / KEK / db and reboots on the first success (see [Provision Target Trust Stores](./enroll-platform-keys.md)).
2. Device identity: `akgen` creates the AK inside the TPM and `akpub` exports the public key.
3. Report and attest: the device reports its fingerprints, AK and Secure Boot state to `/devices/report`; on every later boot it fetches a nonce from `/devices/challenge` and produces a `quote` over PCR 0/2/4/7.
4. Projection: the device fetches boot variables over 4443, trusting only the embedded boot CA.

The device must report `sb=1` to join the pool; with Secure Boot off, a registration-period report writes no ledger entry and only leaves a rejection audit. Identity semantics are in [Device Identity](../concepts/boot-chain/device-identity.md).

## Acceptance

| Check | Expected |
| --- | --- |
| Device boot | Reaches the iPXE menu and can see that Worker's system disk |
| WebUI → Devices platform enrollment | Lists the MACs that completed key enrollment |
| WebUI → Devices | The device joins the pool within the registration window |
| WebUI → boot chain / `/boot-assets` | Firmware `status=ok` with all capabilities; `secureboot.status=ok` |
| Control-plane logs / audit | Records exist for `/devices/report`, `/devices/challenge` and `/boot-vars` |
| Data plane | After selecting a default disk, the NVMe/TCP connection completes and the system starts |

When `secureboot.status` is not `ok`, check the image hash, the port and the boot CA separately.

## Update and rollback

- Replace `tftp/snponly.efi` or `tftp/ipxe.efi` and restart dnsmasq to update the PXE first hop.
- Local-media devices need the new `direct-uefi` artifact written again.
- Keep the previous artifact for rollback; the TPM identity and fingerprints do not change with the firmware file.
- After an update, verify at least one device completes the menu and one system boot.

## Failures

- No DHCP answer, no TFTP download after DHCP, or a boot HTTPS certificate error: locate via [Troubleshooting](./troubleshooting.md).
- Stuck at firmware start: the artifact is unsigned, the signing certificate is not in the device's `db`, or Secure Boot is off; re-check with `sbverify --cert` and the firmware repository's `tools/secureboot/check-host.sh`.
- Empty menu or boot loop: the device is not bound to a Worker, or the Worker has no system disk or default disk.

## Next steps

- Build and sign: [Build Signed Firmware](./build-firmware.md)
- Write platform keys on the first boot: [Provision Target Trust Stores](./enroll-platform-keys.md)
- Boot-chain admission: [Boot Chain](../concepts/boot-chain.md)
- Troubleshooting: [Troubleshooting](./troubleshooting.md)
