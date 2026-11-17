# System Disk Customization

System disks are cloned from a master image. The master is prepared once in a VM or offline environment and placed in the storage node's disk directory; the control plane clones a system disk per Worker and issues that disk's credential at creation.

The master is only a clone source and is never booted.

> If the disk layout, kernel, initramfs or GRUB violates the SAN boot contract, every clone fails the same way. Validate the master before creating disks in bulk.

## Objects

| Object | Location | Role |
| --- | --- | --- |
| Master image | Storage node `spec.agent.diskDir`, filename contains `_tpl_` | Clone source; not exported, not booted |
| System disk | Same directory, created by the control plane | Cloned from a master or created empty; has its own NQN, `os_tag` and credentials |
| Device | Bare metal on the PXE network | Obtains the connection details of its Worker's default disk at boot |

## Need for customization

A system disk passes through iPXE `sanboot` → UEFI firmware → GRUB → kernel → initramfs, and the boot chain imposes additional constraints:

- **4K-native GPT** — nvmet exports with 4096-byte logical blocks; a 512-sector GPT is seen as having no partition table, and the boot fails with `0x7f22208e`
- **ESP contains `EFI/BOOT/BOOTX64.EFI`** — firmware finds no removable-media fallback and skips the disk
- **GRUB ≥ 2.12 (2.14 recommended)** — older GRUB hangs on the SAN path after the menu appears
- **Kernel has NVMe authentication and TLS enabled** — kernel-phase authentication fails (`ret=401`) or no TLS session is established
- **initramfs consumes NBFT/NBCT** — the kernel boots without a disk and cannot mount rootfs
- **The boot network is not reconfigured** — systemd takes over the NIC, resets TCP, and the connection enters keep-alive timeouts and `no usable path`
- **`/boot/efi` is not mounted inside the system** — the 4K FAT ESP mount fails and the system enters emergency mode
- **No identity or credentials on disk** — every clone carries the same identity, violating the stateless-disk premise

The firmware repository packages these constraints as six phases, A–F.

## Verified distributions

Verified distributions and versions are in the [distribution matrix](../reference/distributions.md); anything outside it is not verified, and RHEL 9.x has not been tested.

Native NBFT consumption is not native authentication: the RHEL/SUSE `95nvmf` module only runs a credential-less `nvme connect-all --nbft`; this project's `95nbft` consumes the credential-bearing NBCT and is built with `--omit nvmf`.

> "Verified" denotes the full chain: PXE cold boot → firmware-phase authentication → GRUB → kernel → initramfs consumes NBFT/NBCT → rootfs → login prompt. Reaching GRUB or the kernel alone does not count.

## Procedure

### 1. Installation environment

- A UEFI VM or physical machine; use a raw/qcow2 disk or a passed-through disk.
- Install the target distribution minimally. The ESP must be at least 256 MiB; the script does not resize partitions, so a small default ESP must be expanded first.
- For XFS (Fedora, AlmaLinux, openSUSE Leap): install onto a **4K logical-block** virtual disk; a 512-sector XFS cannot be mounted on nvmet's 4096-byte export.
- The master requires `python3`, `lsblk` / `findmnt` / `blkid` and a usable package repository; the script installs the rest.
- The script is executed **inside the master system as root**, not on the host.

After installation, complete business software, driver, service and user customization; this content is cloned into every system disk. Machine identity, target/NQN, authentication keys and the control-plane address must not be written to the disk; make a separate `_tpl_` master for each role.

### 2. Script acquisition

```bash
git clone https://github.com/dutyc/kurrent-firmware
cd kurrent-firmware
ls test/*/golden-image-setup-*.sh
```

Select the directory and defaults matching the master:

| Distribution / directory | Installer and constraint | Kernel / repository |
| --- | --- | --- |
| Debian 12<br>`debian12` | netinst; initramfs-tools | `6.12.95+deb12-amd64` (backports)<br>`mirrors.aliyun.com/debian` |
| Debian 13<br>`debian13` | netinst; initramfs-tools | `uname -r` (stock kernel is sufficient)<br>`mirrors.aliyun.com/debian` |
| Ubuntu 24.04<br>`ubuntu2404` | live-server; autoinstall, ESP is rebuilt; initramfs-tools | `uname -r`<br>`mirrors.aliyun.com/ubuntu` |
| Ubuntu 26.04<br>`ubuntu2604` | live-server; autoinstall, runtime suite detection; initramfs-tools | `uname -r`<br>`mirrors.aliyun.com/ubuntu` |
| Fedora 44<br>`fedora44` | Server DVD; XFS/LVM; install on a 4K-logical-block disk; dracut `95nbft` | `uname -r`<br>`mirrors.aliyun.com/fedora` |
| AlmaLinux 10.2<br>`almalinux10` | minimal ISO; kickstart, XFS; install on a 4K-logical-block disk; dracut `95nbft` | `uname -r`<br>`repo.huaweicloud.com/almalinux` |
| openSUSE Leap 16.0<br>`opensuse-leap16` | online installer (Agama); Agama profile, XFS; target disk must be empty; dracut `95nbft` | `uname -r`<br>`mirrors.tuna.tsinghua.edu.cn/opensuse` |

The scripts embed the initramfs components and the GRUB 2.14 artifact, so a single `.sh` file can be copied into the master and run; cloning the whole repository is unnecessary.

### 3. Script execution

Execute inside the master system:

```bash
sudo bash golden-image-setup-debian12.sh --help
sudo bash golden-image-setup-debian12.sh          # full A–F; phase E asks for confirmation
sudo bash golden-image-setup-debian12.sh --yes    # skip confirmation; snapshots or disposable VMs
```

- A critical step failure terminates the script with a non-zero status; correct the condition and rerun; the script is idempotent.
- Phase E converts the GPT to 4K and rebuilds the ESP; **create a snapshot before the first run**.
- After conversion, a 512-byte logical-block environment (VMware/QEMU default disks) can no longer read the disk; this is the expected form of a SAN master.

After a complete A–F run, shut down the VM; the image is the master.

Common options:

- `--skip-disk`: skip the GPT conversion and ESP rebuild (ESP contents are still refreshed); not a substitute for a full run when the ESP violates the contract.
- `--yes`: skip the phase E confirmation; disposable environments only.
- `--no-serial`: omit `console=ttyS0,115200`; for physical machines without a serial port.
- `--mirror URL` / `--keep-sources`: switch or keep the package sources.

Other options are in `--help`.

### 4. What the script does

A–F: install dependencies and the kernel → write the initramfs consumer → fix fstab and GRUB → convert to 4K GPT and rebuild the ESP → stateless checks.

The script does not install the OS, create partitions, expand the ESP, install business software, or write target/NQN, credentials or the control-plane address.

Changes to fstab and GRUB keep `.golden-*.bak` backups; the pre-conversion GPT is saved to `/root/golden-image-gpt-512-*.bak`, and both are kept until the cold-boot validation passes.

Disk contracts and implementation details are in the firmware repository's [master-image guide](https://github.com/dutyc/kurrent-firmware/blob/main/docs/golden-image-customization-debian12.md).

## Deploy the master to a storage node

1. Shut down the master VM and ensure the image is fully written.
2. Copy it to the target storage node's `spec.agent.diskDir` with `_tpl_` in the filename; it must be a whole-disk image (GPT, ESP and root partition), and `_tpl_` is case-insensitive:

   ```text
   /pool1/disk_dir/_tpl_debian12.img
   ```

3. Wait for the Agent's background scan; the master appears in `/masters` or the WebUI master list with `gpt` equal to `4k`.
4. When creating a system disk on a Worker, select this master; the control plane generates the `os_tag` and disk NQN, the Agent clones with reflink, and a DHHC-1 credential is issued.
5. The master must not be exported directly to a device.

The control plane also supports an `empty` disk for manual installation; the production path is master cloning. An OS installed onto an empty disk still has to pass the checks on this page.

## Validation

- The serial console shows one successful authentication in the firmware phase and one in the kernel phase, followed by rootfs mount and a `login:` prompt; the first system disk is the default automatically.
- No `0x7f22208e`, `ret=401`, keep-alive failures or emergency mode.
- Captured traffic shows TLS on `4421` only; the disk grep finds no identity or keys, and clones have distinct NQNs, `os_tag` values and credentials.

## Troubleshooting

- **`Boot from SAN device ... failed` with `0x7f22208e`** — GPT is not 4K-native; rerun phase E; confirm `/masters` reports `gpt=4k`.
- **Kernel-phase `ret=401` / `authentication setup failed`** — the kernel lacks NVMe authentication; use a kernel that meets the requirement.
- **Kernel-phase TLS failure** — `tlshd` missing from initramfs, too old, or unconfigured.
- **Keep-alive timeout / `no usable path` after boot** — a systemd network service reset the boot connection; add the distribution script's `systemd.mask` arguments.
- **Emergency mode, `boot-efi.mount` failed** — `fstab` still mounts `/boot/efi`; comment the line and rebuild initramfs and GRUB.
- **GRUB menu hangs** — GRUB is too old; use the embedded 2.14 or `--grub-efi`.
- **Master absent from the list** — filename lacks `_tpl_`, the file is outside `diskDir`, or the Agent cannot read it.
- **Clone reports 512 GPT** — run phase E on the master and confirm 4K GPT.

## Related pages

- Objects and placement: [Devices, Workers and Disks](../concepts/entities.md)
- Masters and data plane: [Storage and Data Plane](../concepts/storage.md)
- Full workflow: [Deployment](./deployment.md)
