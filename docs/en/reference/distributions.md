# Distribution Matrix

The firmware repository provides a self-contained master-image script per distribution, all following the same six phases A–F. This page summarizes the distributions verified through a full cold boot, the acceptance criteria and family differences. Master preparation is documented in [System Disk Customization](../guide/system-disk.md).

## Verified Distributions

| Distribution | Installer media | Unattended | initramfs consumer | Kernel |
| --- | --- | --- | --- | --- |
| Debian 12 (bookworm) | netinst | preseed | initramfs-tools + `nbft-connect` | 6.12 (backports) |
| Debian 13 (13.6) | netinst | preseed | initramfs-tools + `nbft-connect` | 6.12 (stock) |
| Ubuntu 24.04 LTS | live-server | autoinstall | initramfs-tools + `nbft-connect` | 6.8 GA |
| Ubuntu 26.04 LTS | live-server | autoinstall | initramfs-tools + `nbft-connect` | 7.0 |
| Fedora 44 | Server DVD | kickstart | dracut `95nbft` + `nbft-connect` | 6.19 |
| AlmaLinux 10.2 | minimal ISO | kickstart | dracut `95nbft` + `nbft-connect` | 6.12 |
| openSUSE Leap 16.0 | online installer (Agama) | Agama profile | dracut `95nbft` + `nbft-connect` | 6.12 |

Acceptance covers the full chain: PXE cold boot → firmware-phase authentication → GRUB → kernel → initramfs consuming NBFT/NBCT → rootfs → login prompt. Reaching GRUB or the kernel alone does not constitute acceptance. Exact media versions, kernel point releases and acceptance dates are recorded in the firmware repository's [distribution matrix](https://github.com/dutyc/kurrent-firmware/blob/main/docs/golden-image-matrix.md).

## Scripts and Package Sources

| Distribution | Script | Default repository | Install constraint |
| --- | --- | --- | --- |
| Debian 12 | `test/debian12/golden-image-setup-debian12.sh` | Aliyun (bookworm + backports) | ESP at least 256 MiB |
| Debian 13 | `test/debian13/golden-image-setup-debian13.sh` | Aliyun (trixie) | ESP at least 256 MiB |
| Ubuntu 24.04 | `test/ubuntu2404/golden-image-setup-ubuntu2404.sh` | Aliyun (noble) | ESP rebuild required |
| Ubuntu 26.04 | `test/ubuntu2604/golden-image-setup-ubuntu2604.sh` | Aliyun (runtime suite detection) | ESP 4K conversion required |
| Fedora 44 | `test/fedora44/golden-image-setup-fedora44.sh` | Aliyun (fedora + updates) | XFS; install on a 4K-logical-block disk |
| AlmaLinux 10.2 | `test/almalinux10/golden-image-setup-almalinux10.sh` | Huawei Cloud (BaseOS/AppStream/CRB/Extras) | XFS; install on a 4K-logical-block disk |
| openSUSE Leap 16.0 | `test/opensuse-leap16/golden-image-setup-leap16.sh` | TUNA OSS | XFS; target disk must be empty |

Every script embeds the `initramfs-nbft` components and the GRUB 2.14 artifact, so a single file can be copied into the master and executed. After changing components or the GRUB artifact, rerun `python3 test/shared/embed-components.py` in the firmware repository.

## Native NBFT and NBCT

Native NBFT consumption is not native authentication. The RHEL `nm-initrd-generator`/`95nvmf` path and the SUSE dracut `95nvmf` module can build network and discovery connections from NBFT, but the NBFT standard table carries neither DH-HMAC-CHAP keys nor TLS PSKs; their code path is a credential-less `nvme connect-all --nbft`.

This project uses `95nbft` to consume a custom NBCT table carrying the transport address, subsystem NQN, Host NQN, DH-HMAC-CHAP key and TLS handoff parameters, then issues `nvme connect --dhchap-secret` per entry. Records with `tls_trsvcid` inject the PSK into the kernel `.nvme` keyring and reconnect with `--tls`. Builds use `--omit nvmf` so the native module cannot establish an unauthenticated connection first.

## Family Differences

### Debian and Ubuntu

- initramfs integration uses the initramfs-tools `hooks/nbft` and `scripts/local-top/nbft`.
- Debian 12's 6.1 kernel lacks NVMe authentication support and requires the 6.12 backports kernel; Debian 13 and Ubuntu use their stock kernels.
- The Ubuntu 24.04 cloud image ships a 106 MiB ESP and requires a 4K FAT32 rebuild; Ubuntu 26.04 meets the size requirement and needs only the 4K conversion.
- GRUB arguments include `net.ifnames=0 ip=dhcp nbft_auto`; Debian 12's stock GRUB 2.06 hangs on the SAN path, so the script embeds GRUB 2.14.

### Fedora

- The dracut `95nbft` module mounts at both `initqueue/settled` and `pre-mount`, deduplicated by `/run/nbft-done`.
- It depends on `dracut-network` and `systemd-networkd`; the initrd configures the network through `09-nbft.network` (DHCP + KeepConfiguration).
- XFS fixes its sector size at `mkfs` time, so the installer must use a 4K-logical-block disk; otherwise the master cannot mount on nvmet's 4096-byte export.
- LVM roots require an explicit `lvm vgchange -ay` and `rd.lvm.lv=<vg>/<lv>` on the kernel command line.

### RHEL and SUSE

| Difference | AlmaLinux 10.2 | openSUSE Leap 16.0 |
| --- | --- | --- |
| Installer | Anaconda + kickstart | Agama + JSON profile |
| Package manager | dnf | zypper |
| initrd networking | `35network-manager` + `nm-initrd-generator` | Same |
| Native NBFT module | `95nvmf` + `nm-initrd-generator` | dracut `95nvmf` |
| Project approach | `--omit nvmf`, install `95nbft` | Same |
| TLS packages | `keyutils` + `ktls-utils` | Same |
| crypto-policies | `gnutls.config` must be in the initrd | Same |
| initrd path | `/boot/initramfs-<KVER>.img` | `/boot/initrd-<KVER>`, renamed on copy |
| Layout constraint | XFS, 4K-logical-block disk | XFS, 4K-logical-block empty disk |

On RHEL, `crypto-policies` is a hard initrd dependency: GnuTLS `@SYSTEM` priorities come from `/etc/crypto-policies/back-ends/gnutls.config`. Without it, `tlshd` exits immediately with `gnutls: The request is invalid. (-50)`, `nvme connect --tls` fails and the system enters emergency mode.

RHEL 9.x has not been tested. Its 5.14 kernel predates the upstream NVMe/TCP TLS feature (6.7+); whether the vendor backported it must be checked against `CONFIG_NVME_TCP_TLS` and the `ktls-utils` version before use.

## Adding a Distribution

1. Derive the script from the closest existing variant and check the installer kernel and initrd names, unattended data, package manager source format and initrd toolchain.
2. Use a 4K-logical-block virtual disk; XFS and Agama scenarios impose additional disk constraints.
3. After the unattended installation, run A–F inside the master. `--skip-disk` skips only the GPT conversion and ESP rebuild; ESP contents are still refreshed.
4. Complete PXE cold-boot acceptance on an isolated network: two controller records on the target, the packet-capture criteria and a login prompt.
5. After component changes, rerun `embed-components.py` and run `test/test-nbft-connect-nbct.sh` under both dash and bash.

## Related Pages

- [System Disk Customization](../guide/system-disk.md)
- [Firmware Overview](../firmware/index.md)
- [Firmware Contract](./firmware-contract.md)
- Firmware distribution matrix: <https://github.com/dutyc/kurrent-firmware/blob/main/docs/golden-image-matrix.md>
