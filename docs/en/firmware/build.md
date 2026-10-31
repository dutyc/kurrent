# Build and Artifacts

The firmware repository stores no iPXE source. A build fetches a pinned upstream baseline and applies the patch set, so the chain can be reproduced when upstream security updates or new hardware support arrive.

## Environment

- Linux x86_64 build host.
- `git`, `make`, `gcc`; other dependencies are checked by the upstream Makefile.
- Network access to the upstream iPXE repository; the first build caches source under `.cache/ipxe-upstream`.

## Build

```bash
git clone https://github.com/dutyc/kurrent-firmware
cd kurrent-firmware
./build/build.sh
```

Pipeline:

1. Fetch upstream iPXE and check out the pinned baseline (currently `e6e51ccb`).
2. Apply `patches/*.patch` in filename order; any failed patch aborts the build.
3. Install `embed/auto.ipxe` as the embedded script for `direct-uefi`.
4. Build the UEFI targets and copy them to `dist/`.
5. Generate `dist/SHA256SUMS`.

Options:

```bash
UPSTREAM_COMMIT=<sha> ./build/build.sh   # override the upstream baseline (patches must apply)
JOBS=8 ./build/build.sh                  # parallelism
```

Patch baseline and upgrade procedures are in the firmware repository's `patches/README`. Before moving upstream, verify each patch still applies and rerun QEMU and whole-machine validation.

## Artifacts

| Artifact | Form | Purpose |
| --- | --- | --- |
| `dist/pxe-uefi/ipxe.efi` | PXE network boot (UEFI, native drivers) | Mainboard PXE chain; scripts delivered over DHCP |
| `dist/pxe-uefi/ipxe-debug.efi` | Same, debug | Diagnosis |
| `dist/pxe-uefi/snponly.efi` | PXE network boot (SNP only) | First hop when native drivers are unavailable |
| `dist/pxe-uefi/snponly-debug.efi` | Same, debug | Diagnosis |
| `dist/direct-uefi/ipxe.efi` | Direct UEFI boot (embedded script) | USB/ESP, local disk or GRUB chainload |
| `dist/direct-uefi/ipxe-debug.efi` | Same, debug | Diagnosis |
| `dist/direct-uefi/snponly.efi` | Direct UEFI boot (SNP only, embedded script) | Local-media boot without native drivers |

Debug artifacts enable `DEBUG=realtek:3` and produce more NIC initialization logs.

### Selection

| Scenario | Choice |
| --- | --- |
| Mainboard supports PXE / UEFI Network Boot | `pxe-uefi/snponly.efi` (default first hop) |
| No PXE entry; boot from USB/ESP | `direct-uefi/ipxe.efi` |
| Native drivers unavailable; use the firmware SNP stack | `pxe-uefi/snponly.efi` or `direct-uefi/snponly.efi` |
| NIC initialization failures need diagnosis | The matching `-debug` artifact |

The platform uses `tftp/snponly.efi` by default. The full `ipxe.efi` is only needed for custom drivers or local-media boot.

## Verification

```bash
cd dist
sha256sum -c SHA256SUMS
```

Release assets use flat names (for example `pxe-uefi-snponly.efi`), while the checksum file preserves `pxe-uefi/` paths. Place downloads in matching directories before verifying, or compare the digest of the selected file directly.

## Install into the Platform Repository

```bash
sudo install -m 0644 dist/pxe-uefi/snponly.efi /path/to/kurrent/tftp/snponly.efi
cd /path/to/kurrent
docker compose restart kurrent-dnsmasq
```

`tftp/snponly.efi` is a file-level bind mount. Restart the dnsmasq container after replacing it, otherwise the container keeps the old inode.

Confirm firmware capabilities:

```bash
curl -sS \
  --cert control_plane/state/pki/users/admin/client.crt \
  --key control_plane/state/pki/users/admin/client.key \
  --cacert control_plane/state/certs/server.crt \
  https://<controller-ip>/api/cp/boot-assets | jq '.firmware'
```

`status=ok` with all six capabilities (HTTPS boot, NVS persistence, device key generation, public-key export, challenge signing, TOFU anchoring) is required by the boot chain. `incomplete` means the file exists but lacks capabilities — usually stock iPXE placed in the TFTP root.

## Update and Rollback

- **Update**: replace `tftp/snponly.efi` and restart dnsmasq. Device NVRAM keys and server fingerprints are unaffected unless explicitly cleared.
- **Rollback**: keep the previous artifact and swap it back the same way.
- **Validation**: boot a test device, confirm the menu appears and one system boots; confirm `/boot-assets` reports all capabilities.

## Testing

The firmware repository provides QEMU scripts and whole-machine records:

```bash
sudo bash test/nvmet-setup.sh        # local NVMe/TCP target
python3 test/cred-server.py 8000     # credential endpoint
bash test/run-qemu-auth.sh           # one authenticated boot round
```

Test details, pcap analysis and distribution records live in the firmware repository documentation.

## License

The firmware repository is GPL-2.0, compatible with upstream iPXE's dual license; some third-party driver adaptations are GPL-2.0 only. Read `patches/README` before redistributing.

## Related Pages

- [Firmware Overview](./index.md)
- [Provisioning](./provision.md)
- [Firmware Contract](../reference/firmware-contract.md)
- Firmware build documentation: <https://github.com/dutyc/kurrent-firmware/blob/main/docs/build-artifacts.md>
