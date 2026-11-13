# Firmware Build and Deployment

This page is one complete task: build the firmware from source, select a boot form, deploy it to a device, and verify first boot and identity enrollment. The firmware lives in the separate `kurrent-firmware` repository; the result enters the main repository's TFTP root and the control plane boot chain.

## Goal and prerequisites

When finished:

- `tftp/snponly.efi` comes from this build, and dnsmasq has been restarted to load the new file;
- a device completes one PXE or local-media boot and reaches the iPXE menu;
- the device reports its TPM AK public key inside a registration window and appears in the Devices ledger;
- `/boot-assets` reports complete firmware capabilities.

Prerequisites:

- a Linux x86_64 build host with `git`, `make`, and `gcc`;
- access to the upstream iPXE repository; the first build caches the source;
- x86_64 UEFI target devices with Secure Boot enabled and the site signing certificate enrolled in `db`; Legacy BIOS (CSM) boot is no longer supported;
- a control plane, dnsmasq, and an isolated PXE segment as described in [Deployment](../guide/deployment.md).

## 1. Build

```bash
git clone https://github.com/dutyc/kurrent-firmware
cd kurrent-firmware
SB_KEY=/path/to/artifact-signing.key \
SB_CERT=/path/to/artifact-signing.crt \
BOOT_CA_CERT=<state-root>/control-plane/state/pki/ca/boot/ca.crt \
BOOT_HTTPS_PORT=4443 \
./build/build-sb.sh
```

`build-sb.sh` embeds the static scripts, pins the boot CA and controller address on top of the regular build, and produces a first hop signed with `sbsign`. Signing inputs, the manifest and provisioning are in [Secure Boot Boot](./secure-boot.md).

The regular `./build/build.sh` does not sign anything and its artifacts are only for local debugging and research; targets with Secure Boot enabled reject them.

```bash
./build/build.sh
UPSTREAM_COMMIT=<sha> ./build/build.sh   # alternate upstream baseline; patches must still apply
JOBS=8 ./build/build.sh                  # parallelism
```

The pipeline:

1. Fetch upstream iPXE and check out the pinned baseline (currently `e6e51ccb`).
2. Apply `patches/*.patch` in filename order; any patch that fails to apply aborts the build.
3. Install `embed/auto.ipxe` as the built-in script of `direct-uefi`.
4. Build the UEFI targets and copy them to `dist/`.
5. Generate `dist/SHA256SUMS`.

See `patches/README` in the firmware repository for the patch baseline and upgrade procedure. Before upgrading upstream, confirm every patch still applies and rerun QEMU and whole-machine validation.

Verify the artifacts:

```bash
cd dist
sha256sum -c SHA256SUMS
```

For signed artifacts the reference digest is `artifact_sha256` in `dist/secureboot/manifest.json`, which `/boot-assets` compares against the actual image hash.

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

The table above lists `build.sh` forms; deployments replace the first hop with `dist/secureboot/snponly-sb.efi` from `build-sb.sh`, which has the same form as `pxe-uefi/snponly.efi`.

## 3. Deploy to devices

| Condition | Path |
| --- | --- |
| UEFI Network Boot / PXE entry is available | PXE, using `snponly.efi` from the platform TFTP root |
| No PXE entry, or PXE settings unavailable | Local media (USB / ESP / local disk / GRUB chainload) |
| Batch provisioning with BMC virtual media | BMC / Redfish virtual media, optional |

### PXE

1. Take `dist/secureboot/snponly-sb.efi` and `manifest.json`.
2. Install them into the main repository TFTP root and restart dnsmasq:

   ```bash
   sudo install -d -m 0755 /path/to/kurrent/tftp/secureboot
   sudo install -m 0644 dist/secureboot/snponly-sb.efi dist/secureboot/manifest.json \
     /path/to/kurrent/tftp/secureboot/
   sudo install -m 0644 dist/secureboot/snponly-sb.efi /path/to/kurrent/tftp/snponly.efi
   cd /path/to/kurrent
   eval "$(kurrent deployment show -o env)"
   docker compose restart kurrent-dnsmasq
   ```

   `tftp/snponly.efi` is a file-level mount; the container keeps the old inode until restarted. `tftp/secureboot/` remains the source for `/boot-assets` artifact verification and export.

3. Enable the network stack and Secure Boot in UEFI settings and put network boot first.
4. Boot the device and confirm DHCP, the TFTP first hop, and the report and challenge over 4443.

TFTP delivers only the first hop `snponly.efi` (and the `boot.ipxe` pointer); the signed first hop embeds the static scripts, boot CA and controller address, and later requests go over 4443 HTTPS trusting only that boot CA. The TLS 1.3 `sanboot` data plane and the kernel segment keep the existing credential chain. The PXE segment must be isolated as described in the [security model](../architecture/security.md). Machines that need `pxe-uefi/ipxe.efi` cannot share the same PXE segment with the default first hop.

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

1. runs `akgen` and creates the AK inside the TPM (the private key cannot be exported);
2. exports the AK public key, name and algorithm with `akpub`;
3. reports the fingerprint, AK and Secure Boot state to `/devices/report`;
4. requests a nonce from `/devices/challenge` and runs `quote` over PCR 0/2/4/7 on each boot;
5. fetches boot variables over 4443, trusting only the boot CA embedded at build time.

The device must report `sb=1` to enter the pool; with Secure Boot off, registration-time reports only record the fingerprint.

Identity semantics:

- Device identity is carried by the AK inside the TPM; the trust anchor of boot HTTPS is the boot CA baked into the signed firmware; updating firmware or replacing TFTP files does not clear the AK.
- Clearing the TPM or replacing the mainboard loses the original AK, which is equivalent to replacing the device identity: delete the ledger entry and re-admit the device during the registration window.
- Rotating the boot CA equals reflashing the firmware: rebuild the signed artifact and re-enroll the `db` certificate on every target.
- Replacing hardware creates a new device; the system disk and Worker identity remain in the control plane and move through rebinding.

NVRAM cleanup belongs in the firmware interactive environment or a controlled provisioning procedure. See the [firmware contract](../reference/firmware-contract.md) for AK lifecycle, quote format, and trust boundaries.

## 5. Verify

| Check | Expected |
| --- | --- |
| Device boot | Reaches the iPXE menu and shows the Worker's system disk |
| WebUI → Devices | The device enters the pool inside a registration window |
| WebUI → Boot chain / `/boot-assets` | Firmware `status=ok` with complete capabilities; `secureboot.status=ok` |
| Control plane logs / audit | Records for `/devices/report`, `/devices/challenge`, and `/boot-vars` |
| Data plane | Selecting the default disk connects over NVMe/TCP and boots the system |

`/boot-assets` must report `status=ok` with TPM identity and quote capabilities present. `incomplete` usually means stock iPXE was placed in the TFTP root. When `secureboot.status` is not `ok`, check the image hash, port and boot CA separately.

### QEMU validation

QEMU/OVMF validation requires KVM, OVMF firmware, and a working nvmet target. The firmware repository provides scripts:

```bash
sudo AUTH=1 bash test/nvmet-setup.sh
python3 test/cred-server.py &
# Test script: test/nvmeof-auth-test.ipxe
bash test/run-qemu-auth.sh
```

Acceptance: the serial console shows successful authentication, Property Set, I/O queues, and GRUB output; the target shows two controller records, one for the firmware segment and one for the kernel segment; in the packet capture, the first byte of each client TCP stream is `0x16`, plaintext-port packets are zero, and the payload contains no PSK or DH-HMAC-CHAP key plaintext. `test/cred-server.py` is for validation only and must not be used as a production credential endpoint.

## Update and rollback

- Replacing `tftp/snponly.efi` and restarting dnsmasq updates the PXE first hop.
- Local-media devices need the new `direct-uefi` artifact written again.
- Keep the previous artifact for rollback; NVRAM identity and fingerprints do not change with firmware files.
- After an update, verify at least one device reaches the menu and completes a system boot.

## Common failures

- **No DHCP response** — Device on the PXE segment; dnsmasq `interface=` and firewall 67/69.
- **No TFTP download after DHCP** — TFTP root contains only `snponly.efi` and `boot.ipxe`; file permissions.
- **Stuck at firmware boot** — The artifact is unsigned, its signing certificate is not in the target's `db`, or Secure Boot is off; check with `sbverify --cert` and `tools/secureboot/check-host.sh`, and confirm the artifact architecture and form.
- **Boot HTTPS certificate error** — The `BOOT_CA_CERT` used at build time differs from `state/pki/ca/boot/ca.crt`; or the 4443 leaf SAN does not cover the address devices use, or the port differs from `BOOT_HTTPS_PORT`.
- **Device not pooled** — Registration window open; report carries a valid TPM AK; device not revoked.
- **Empty menu or boot loop** — Device bound to a Worker; Worker has a system disk and default disk.
- **Data plane connection failure** — `tlshd`, kernel TLS options, port 4421, and credential push state.

## Related pages

- [Firmware overview](./index.md)
- [Firmware platform reference](../reference/firmware-platform.md)
- [Firmware contract](../reference/firmware-contract.md)
- [Boot chain](../concepts/boot-chain.md)
- [Security model](../architecture/security.md)
- [Troubleshooting](../guide/troubleshooting.md)
- Firmware repository build documentation: <https://github.com/dutyc/kurrent-firmware/blob/main/docs/build-artifacts.md>
