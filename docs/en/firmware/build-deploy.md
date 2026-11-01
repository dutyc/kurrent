# Firmware Build and Deployment

This page is one complete task: build the firmware from source, select a boot form, deploy it to a device, and verify first boot and identity enrollment. The firmware lives in the separate `kurrent-firmware` repository; the result enters the main repository's TFTP root and the control plane boot chain.

## Goal and prerequisites

When finished:

- `tftp/snponly.efi` comes from this build, and dnsmasq has been restarted to load the new file;
- a device completes one PXE or local-media boot and reaches the iPXE menu;
- the device reports its public key inside a registration window and appears in the Devices ledger;
- `/boot-assets` reports complete firmware capabilities.

Prerequisites:

- a Linux x86_64 build host with `git`, `make`, and `gcc`;
- access to the upstream iPXE repository; the first build caches the source;
- x86_64 UEFI target devices with Secure Boot disabled; mainline artifacts are unsigned;
- a control plane, dnsmasq, and an isolated PXE segment as described in [Deployment](../guide/deployment.md).

## 1. Build

```bash
git clone https://github.com/dutyc/kurrent-firmware
cd kurrent-firmware
./build/build.sh
```

The pipeline:

1. Fetch upstream iPXE and check out the pinned baseline (currently `e6e51ccb`).
2. Apply `patches/*.patch` in filename order; any patch that fails to apply aborts the build.
3. Install `embed/auto.ipxe` as the built-in script of `direct-uefi`.
4. Build the UEFI targets and copy them to `dist/`.
5. Generate `dist/SHA256SUMS`.

Optional parameters:

```bash
UPSTREAM_COMMIT=<sha> ./build/build.sh   # alternate upstream baseline; patches must still apply
JOBS=8 ./build/build.sh                  # parallelism
```

See `patches/README` in the firmware repository for the patch baseline and upgrade procedure. Before upgrading upstream, confirm every patch still applies and rerun QEMU and whole-machine validation.

Verify the artifacts:

```bash
cd dist
sha256sum -c SHA256SUMS
```

Release assets use flat names (for example `pxe-uefi-snponly.efi`), while the checksum file keeps the `pxe-uefi/` directory layout. Place files in that layout before checking, or compare only the target file digest.

## 2. Select artifacts

| Artifact | Form | Use |
| --- | --- | --- |
| `pxe-uefi/ipxe.efi` | PXE network boot | Motherboard PXE chain; script delivered by DHCP |
| `pxe-uefi/snponly.efi` | PXE network boot | Default first hop |
| `pxe-uefi/ipxe-debug.efi`, `snponly-debug.efi` | Same, debug | NIC initialization and boot diagnosis |
| `direct-uefi/ipxe.efi` | UEFI direct boot, embedded script | USB/ESP, local disk, or GRUB chainload |
| `direct-uefi/ipxe-debug.efi` | Same, debug | Diagnosis |
| `direct-uefi/snponly.efi` | UEFI direct boot | Local media |

Selection:

| Scenario | Choice |
| --- | --- |
| UEFI Network Boot is available | `pxe-uefi/snponly.efi`, the default first hop |
| No PXE entry; boot from USB/ESP | `direct-uefi/ipxe.efi` |
| NIC initialization fails | The matching `-debug` artifact |

The main repository uses `tftp/snponly.efi` by default. Different machine types may need different first-hop artifacts, while mainline delivers only `snponly.efi` and does not switch artifacts per device; machines that need `pxe-uefi/ipxe.efi` cannot share the same PXE segment with the default first hop, so the first-hop artifact must be unified before deployment.

## 3. Deploy to devices

| Condition | Path |
| --- | --- |
| UEFI Network Boot / PXE entry is available | PXE, using `snponly.efi` from the platform TFTP root |
| No PXE entry, or PXE settings unavailable | Local media (USB / ESP / local disk / GRUB chainload) |
| Batch provisioning with BMC virtual media | BMC / Redfish virtual media, optional |

### PXE

1. Take `pxe-uefi/snponly.efi`.
2. Install it into the main repository TFTP root and restart dnsmasq:

   ```bash
   sudo install -m 0644 dist/pxe-uefi/snponly.efi /path/to/kurrent/tftp/snponly.efi
   cd /path/to/kurrent
   docker compose restart kurrent-dnsmasq
   ```

   `tftp/snponly.efi` is a file-level mount; the container keeps the old inode until restarted.

3. Enable the network stack in UEFI settings and put network boot first.
4. Boot the device and confirm DHCP, the TFTP first hop, and the HTTPS script fetch.

The first hop downloads `snponly.efi` and `boot.ipxe`; later scripts come over HTTPS. The PXE segment must be isolated as described in the [security model](../architecture/security.md). Machines that need `pxe-uefi/ipxe.efi` cannot share the same PXE segment with the default first hop.

### Local media

Use a `direct-uefi` artifact. It embeds `auto.ipxe`, performs DHCP after boot, and jumps to the control plane TFTP boot chain.

USB / ESP:

1. Prepare a FAT32 partition (USB stick or local ESP).
2. Create `EFI/BOOT/`.
3. Copy `dist/direct-uefi/ipxe.efi` to `EFI/BOOT/BOOTX64.EFI`.
4. Select the USB/ESP from the device boot menu.

If the motherboard only recognizes a fixed ESP, place the file at the same path on the internal disk and select it from the boot entries.

GRUB chainload:

```text
menuentry "Kurrent firmware" {
    chainloader /boot/efi/EFI/kurrent/ipxe.efi
}
```

BMC / Redfish virtual media has only protocol-level validation; capabilities vary by model. It is not a Kurrent deployment dependency and does not replace PXE segment isolation.

## 4. First boot and identity

On first boot the firmware:

1. runs `keygen` and creates the device private key in NVRAM;
2. exports the public key with `pubkey`;
3. reports the fingerprint and public key to `/devices/report`;
4. pins the control plane server leaf certificate fingerprint on the first HTTPS handshake (TOFU).

Identity semantics:

- The device private key and server fingerprint live in NVRAM, **not on disk**; updating firmware or replacing TFTP files does not clear them.
- Clearing `device-key` is equivalent to replacing the device identity; it must be registered and bound again.
- Clearing `server-fingerprint` removes TOFU; the next HTTPS connection pins again. This is required after rotating the control plane certificate.
- Replacing hardware creates a new device; the system disk and Worker identity remain in the control plane and move through rebinding.

NVRAM cleanup belongs in the firmware interactive environment or a controlled provisioning procedure; never copy NVRAM contents to another device. See the [firmware contract](../reference/firmware-contract.md) for key lifecycle, signature format, and trust boundaries.

## 5. Verify

| Check | Expected |
| --- | --- |
| Device boot | Reaches the iPXE menu and shows the Worker's system disk |
| WebUI → Devices | The device enters the pool inside a registration window |
| WebUI → Boot chain / `/boot-assets` | Firmware `status=ok` with complete capabilities |
| Control plane logs / audit | Records for `/devices/report`, `/devices/challenge`, and `/boot-vars` |
| Data plane | Selecting the default disk connects over NVMe/TCP and boots the system |

`/boot-assets` must report `status=ok` with all six capabilities (HTTPS boot, NVS persistence, device key generation, public-key export, challenge signing, TOFU pinning). `incomplete` usually means stock iPXE was placed in the TFTP root.

## Update and rollback

- Replacing `tftp/snponly.efi` and restarting dnsmasq updates the PXE first hop.
- Local-media devices need the new `direct-uefi` artifact written again.
- Keep the previous artifact for rollback; NVRAM identity and fingerprints do not change with firmware files.
- After an update, verify at least one device reaches the menu and completes a system boot.

## Common failures

- **No DHCP response** — Device on the PXE segment; dnsmasq `interface=` and firewall 67/69.
- **No TFTP download after DHCP** — TFTP root contains only `snponly.efi` and `boot.ipxe`; file permissions.
- **Stuck at firmware boot** — Secure Boot enabled (mainline artifacts are unsigned); artifact architecture and form.
- **HTTPS certificate error** — TOFU fingerprint does not match the control plane certificate; re-provision after rotation.
- **Device not pooled** — Registration window open; report carries a valid public key; device not revoked.
- **Empty menu or boot loop** — Device bound to a Worker; Worker has a system disk and default disk.
- **Data plane connection failure** — `tlshd`, kernel TLS options, port 4421, and credential push state.

## Related pages

- [Firmware overview](./index.md)
- [Firmware contract](../reference/firmware-contract.md)
- [Boot chain](../concepts/boot-chain.md)
- [Security model](../architecture/security.md)
- [Troubleshooting](../guide/troubleshooting.md)
- Firmware repository build documentation: <https://github.com/dutyc/kurrent-firmware/blob/main/docs/build-artifacts.md>
