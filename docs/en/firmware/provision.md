# Provisioning and Flashing

Firmware is in place only after it has booted on a device and completed one successful network boot. There are two provisioning paths: native PXE on the motherboard, or boot from local media.

## Choose a path

| Condition | Path |
| --- | --- |
| Motherboard has a UEFI Network Boot / PXE entry | PXE, using `snponly.efi` served by the platform TFTP |
| No PXE entry, or PXE setup is unavailable | Local media (USB / ESP / local disk / GRUB chainload) |
| NIC has no native driver, but the firmware ships an SNP stack | The `snponly` artifact |
| Batch provisioning and the BMC supports virtual media | BMC / Redfish media mount, as an option |

Prefer PXE: firmware updates only replace files in the TFTP root and never touch each device's boot media.

## PXE path

Prerequisites:

- x86_64 UEFI device;
- the NIC has a native driver, or the firmware SNP is usable;
- the device is attached to the control-plane PXE network;
- the mainline firmware is unsigned, so Secure Boot must be disabled (see below).

Steps:

1. Obtain `pxe-uefi/snponly.efi` from [Build and Artifacts](./build.md) or the Releases page.
2. Place it at `tftp/snponly.efi` in the main repository and restart the dnsmasq container.
3. In the device UEFI settings, enable the network stack and put network boot first in the boot order.
4. Boot the device and confirm DHCP assignment, the first TFTP download, and script retrieval over HTTPS.

The first hop only downloads `snponly.efi` and `boot.ipxe`; later scripts come over HTTPS. The PXE network must be isolated as described in the [Security Model](../architecture/security.md).

## Local media path

For motherboards without a PXE entry. Use a `direct-uefi` artifact: it embeds `auto.ipxe`, performs DHCP on boot, and then jumps to the control-plane TFTP boot chain.

### USB / ESP

1. Prepare a FAT32 partition (USB stick or the local ESP).
2. Create the directory `EFI/BOOT/`.
3. Copy `dist/direct-uefi/ipxe.efi` to `EFI/BOOT/BOOTX64.EFI`.
4. Select that USB/ESP from the device boot menu.

UEFI boots removable media from `EFI/BOOT/BOOTX64.EFI`. If the motherboard only recognizes a fixed ESP, place the file at the same path on the internal disk and select it as a boot entry.

### GRUB chainload

A device that already runs Linux/GRUB can chainload directly, without changing boot media:

```text
menuentry "Kurrent firmware" {
    chainloader /boot/efi/EFI/kurrent/ipxe.efi
}
```

Put `direct-uefi/ipxe.efi` at that path, update the GRUB configuration, and boot from the entry.

### BMC / Redfish

A BMC that supports virtual media can mount the USB/ISO above or point a boot entry at it. This path has only been verified at the protocol level and capabilities differ by model; it is not a deployment dependency for Kurrent and does not replace the PXE network isolation requirement.

## Secure Boot

The mainline firmware is unsigned:

- With Secure Boot enabled, the firmware load is rejected and the device cannot enter the boot chain.
- The mainline deployment path requires Secure Boot to be off, or an artifact signed by your organization; signed artifacts and their provisioning flow are outside the mainline release scope.
- Disabling Secure Boot weakens first-hop protection; deployment should compensate with PXE network isolation and Layer 2 protection, as described in the [Security Model](../architecture/security.md).

Do not treat "disable Secure Boot" as a security solution; it is only the compatibility condition of the current mainline.

## First boot and identity

On first boot the firmware:

1. runs `keygen` to generate the device private key in NVRAM;
2. exports the public key with `pubkey`;
3. reports the fingerprint and public key to the control plane at `/devices/report`;
4. pins the control-plane server leaf certificate fingerprint on the first HTTPS handshake (TOFU).

Identity semantics:

- The device private key and the server fingerprint live in NVRAM, **not on disk**; updating firmware or replacing TFTP files does not clear them.
- Clearing `device-key` is equivalent to changing the device identity and requires re-registration and rebinding.
- Clearing `server-fingerprint` removes the TOFU pin; the next HTTPS connection pins again. This is required after control-plane certificate rotation.
- Replacing hardware or a whole machine is a new device; the system disk and Worker identity remain in the control plane, and a rebind is enough.

NVRAM should be cleared through the firmware's interactive environment or a controlled provisioning flow; never copy NVRAM contents to another device.

## Verification

| Check | Expected |
| --- | --- |
| Device boot | iPXE menu appears and shows the Worker's system disks |
| WebUI → Devices | The device joins the pool within the registration window |
| WebUI → Boot chain / `/boot-assets` | Firmware `status=ok` with all capabilities present |
| Control-plane logs / audit | Records for `/devices/report`, `/devices/challenge`, `/boot-vars` |
| Data plane | After selecting the default disk, the NVMe/TCP connection completes and the system boots |

## Update and rollback

- Replace `tftp/snponly.efi` and restart dnsmasq to update the PXE first-hop firmware.
- Devices booting from local media must have the new `direct-uefi` artifact written again.
- Keep the previous artifact for rollback; NVRAM identity and fingerprints do not change with firmware files.
- After an update, verify at least one device reaches the menu and boots the system once.

## Common failures

| Symptom | Triage |
| --- | --- |
| No DHCP reply | Is the device on the PXE network; dnsmasq `interface=` and firewall ports 67/69 |
| No TFTP download after DHCP | Does the TFTP root mount only `snponly.efi` and `boot.ipxe`; file permissions |
| Stuck before iPXE starts | Is Secure Boot enabled; is the artifact the right architecture and form |
| HTTPS certificate error | TOFU fingerprint no longer matches the control-plane certificate; the certificate rotated without re-provisioning |
| Device never joins the pool | Is the registration window open; does the report carry a valid public key; is the device revoked |
| Empty menu or boot loop | Is the device bound to a Worker; does the Worker have system disks and a default disk |
| Data-plane connection fails | `tlshd`, kernel TLS configuration, port 4421, and credential push state |

## Related pages

- [Firmware Overview](./index.md)
- [Build and Artifacts](./build.md)
- [Firmware Contract](../reference/firmware-contract.md)
- [Boot Chain](../concepts/boot-chain.md)
- [Deployment Guide](../guide/deployment.md)
