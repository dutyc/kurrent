# System Disk Customization

Kurrent does not install a system per machine. A **master image** is prepared once in a VM or offline environment, placed in the storage node's disk directory, and cloned per Worker by the control plane. The clone becomes a system disk with its own `os_tag`, NQN and credentials. The master is only a clone source; it is never booted, and this preparation does not repeat on every deployment.

> If the master's disk layout, kernel, initramfs or GRUB violates the SAN boot contract, every cloned system disk fails in the same way. Validate the master before creating disks in bulk.

## Objects

| Object | Location | Role |
| --- | --- | --- |
| Master image | Storage node `spec.agent.diskDir`, filename contains `_tpl_` | Clone source; not exported, not booted |
| System disk | Same directory, created by the control plane | Cloned from a master or created empty; has its own NQN, `os_tag` and credentials |
| Device | Bare metal on the PXE network | Obtains the connection details of its Worker's default disk at boot |

A master must be scanned by the Agent before it can be selected as a clone source.

## Need for customization

A conventional installation targets local-disk boot. A Kurrent system disk passes through iPXE `sanboot` → UEFI firmware → GRUB → kernel → initramfs, and the boot chain imposes additional constraints:

- **4K-native GPT** — nvmet exports with 4096-byte logical blocks; a 512-sector GPT is seen as having no partition table, and the boot fails with `0x7f22208e`
- **ESP contains `EFI/BOOT/BOOTX64.EFI`** — firmware finds no removable-media fallback and skips the disk
- **GRUB ≥ 2.12 (2.14 recommended)** — older GRUB hangs on the SAN path after the menu appears
- **Kernel has NVMe authentication and TLS enabled** — kernel-phase authentication fails (`ret=401`) or no TLS session is established
- **initramfs consumes NBFT/NBCT** — the kernel boots without a disk and cannot mount rootfs
- **The boot network is not reconfigured** — systemd takes over the NIC, resets TCP, and the connection enters keep-alive timeouts and `no usable path`
- **`/boot/efi` is not mounted inside the system** — the 4K FAT ESP mount fails and the system enters emergency mode
- **No identity or credentials on disk** — every clone carries the same identity, violating the stateless-disk premise

These are boot-chain contracts rather than optional tuning. The firmware repository packages the steps as six phases, A–F.

## Verified distributions

Distributions currently verified through a full cold boot: Debian 12/13, Ubuntu 24.04/26.04, Fedora 44, AlmaLinux 10.2, and openSUSE Leap 16.0. Installation media, constraints, initramfs consumers, customization scripts, and default repositories are listed under [Script acquisition](#_3-script-acquisition); versions, sources, and family differences are in the [distribution matrix](../reference/distributions.md), and the research notes on native NBFT consumption live in the firmware repository's [distribution research](https://github.com/dutyc/kurrent-firmware/blob/main/docs/nvmeof-research.md).

Versions outside this set are not verified; RHEL 9.x has not been tested. Native NBFT consumption is not native authentication: the RHEL/SUSE `95nvmf` module only runs a credential-less `nvme connect-all --nbft`, whereas this project's `95nbft` consumes the credential-bearing NBCT and is built with `--omit nvmf`.

> “Verified” denotes the full chain: PXE cold boot → firmware-phase authentication → GRUB → kernel → initramfs consumes NBFT/NBCT → rootfs → login prompt. Reaching GRUB or the kernel alone does not constitute verification.

## Procedure

### 1. Installation environment

- A UEFI VM or physical machine; use a raw/qcow2 disk or a passed-through disk.
- Install the target distribution minimally. The ESP must be at least 256 MiB; the script does not resize partitions, so a small default ESP must be expanded before execution.
- For XFS (Fedora, AlmaLinux, openSUSE Leap): install onto a **4K logical-block** virtual disk. XFS fixes its sector size at `mkfs` time, and a 512-sector XFS cannot be mounted on nvmet's 4096-byte export.
- The master requires `python3`, `lsblk` / `findmnt` / `blkid` (util-linux) and a usable package repository; `nvme-cli`, `jq`, `keyutils`, `ktls-utils`, `initramfs-tools` and similar packages are installed by phase A.
- The script is executed **inside the installed system as root**, not on the host.

After installation, complete customization of business software, drivers, services and users; this content is cloned into every system disk. Machine identity, target/NQN, authentication keys and the control-plane address must not be written to the disk; a separate `_tpl_` master is required for each role.

### 2. Kernel capability check (optional)

Phase A installs dependencies and checks the kernel's NVMe authentication capability; Debian 12 additionally installs the 6.12 kernel from bookworm-backports. To check the full capability set ahead of time:

```bash
# The kernel must enable NVMe authentication and NVMe/TCP TLS
grep -E 'CONFIG_NVME_(AUTH|TCP_TLS|TARGET_TCP_TLS|KEYRING)' /boot/config-$(uname -r)

# tlshd performs kernel-phase TLS handshakes (version >= 1.5.0)
dpkg-query -W ktls-utils 2>/dev/null || rpm -q ktls-utils
systemctl status tlshd
```

Debian 12's 6.1 kernel does not meet the authentication requirement; the other variants use their current `uname -r`. A version whose kernel lacks NVMe/TCP TLS support must not serve as a production master, and the data plane must not fall back to plaintext.

### 3. Script acquisition

```bash
git clone https://github.com/dutyc/kurrent-firmware
cd kurrent-firmware
ls test/*/golden-image-setup-*.sh
```

Scripts live at `test/<directory>/golden-image-setup-*.sh` in the firmware repository, and every directory injects `nbft-connect` into the initramfs. Select the directory and defaults matching the master:

| Distribution / directory | Installer and constraint | Kernel / repository |
| --- | --- | --- |
| Debian 12<br>`debian12` | netinst; initramfs-tools | `6.12.95+deb12-amd64` (backports)<br>`mirrors.aliyun.com/debian` |
| Debian 13<br>`debian13` | netinst; initramfs-tools | `uname -r` (stock kernel is sufficient)<br>`mirrors.aliyun.com/debian` |
| Ubuntu 24.04<br>`ubuntu2404` | live-server; autoinstall, ESP is rebuilt; initramfs-tools | `uname -r`<br>`mirrors.aliyun.com/ubuntu` |
| Ubuntu 26.04<br>`ubuntu2604` | live-server; autoinstall, runtime suite detection; initramfs-tools | `uname -r`<br>`mirrors.aliyun.com/ubuntu` |
| Fedora 44<br>`fedora44` | Server DVD; XFS/LVM; install on a 4K-logical-block disk; dracut `95nbft` | `uname -r`<br>`mirrors.aliyun.com/fedora` |
| AlmaLinux 10.2<br>`almalinux10` | minimal ISO; kickstart, XFS; install on a 4K-logical-block disk; dracut `95nbft` | `uname -r`<br>`repo.huaweicloud.com/almalinux` |
| openSUSE Leap 16.0<br>`opensuse-leap16` | online installer (Agama); Agama profile, XFS; target disk must be empty; dracut `95nbft` | `uname -r`<br>`mirrors.tuna.tsinghua.edu.cn/opensuse` |

The scripts embed the `initramfs-nbft` components and the GRUB 2.14 artifact, so **a single `.sh` file can be copied into the master and executed**; cloning the whole repository is unnecessary. The following command is required only when components are modified and the embedded payload must be regenerated:

```bash
python3 test/shared/embed-components.py
```

### 4. Script execution

Execute the script inside the master system; it may be placed in any directory:

```bash
# Show the options supported by this variant
sudo bash golden-image-setup-debian12.sh --help

# Run A–F completely; phase E asks for confirmation
sudo bash golden-image-setup-debian12.sh

# Skip the phase E confirmation; suitable for snapshots or disposable VMs
sudo bash golden-image-setup-debian12.sh --yes
```

`--help` outputs the usage block at the top of the script; all variants share the same option set.

Execution prints `==> [A] ...` through `==> [F] ...` in order. A critical step failure terminates the script immediately with a non-zero status; correct the condition and rerun; the script is idempotent. Phase E converts the GPT to 4K and rebuilds the ESP, skipping steps that already match the target format, but **a snapshot should be created before the first run**. After conversion, a 512-byte logical-block environment (VMware/QEMU default disks) can no longer read the disk; this is the expected SAN master form.

After a complete A–F run, shut down the VM; the resulting image is the master.

### 5. Common options

- **`-h`, `--help`**: print usage and exit.
- **`-k VER`**: select the kernel version; Debian 12 defaults to `6.12.95+deb12-amd64`, the others to `uname -r`. No change is normally required.
- **`-d DIR`**: select the initramfs component directory; defaults to `initramfs-nbft/` in the repository, or the embedded copy when the script is outside the repository. Used only when modifying components.
- **`--skip-disk`**: skip the phase E GPT conversion and ESP rebuild; ESP contents are still refreshed on every run. Intended for chroot, an externally maintained ESP, or refreshing boot artifacts without changing the disk layout. It does not replace a full run when the ESP capacity or format violates the contract.
- **`--yes`**: skip the phase E confirmation; used only in a disposable environment.
- **`--grub-efi FILE`**: replace the embedded GRUB 2.14 with a locally built `BOOTX64.EFI`; Debian 12's stock GRUB 2.06 does not affect the default flow, which uses the embedded 2.14. This option is used only for a specific requirement.
- **`--no-serial`**: omit `console=ttyS0,115200`; intended for physical machines without a serial port.
- **`--mirror URL`**: switch the package repository; the default is a China-based mirror per distribution (Aliyun for Debian/Ubuntu/Fedora, Huawei Cloud for AlmaLinux, TUNA for openSUSE Leap).
- **`--keep-sources`**: keep the master's original package sources; `--mirror` has no effect when set.

### 6. Typical usage

- First complete build (VM/snapshot): `sudo bash golden-image-setup-<distro>.sh`
- Unattended complete build: `sudo bash golden-image-setup-<distro>.sh --yes`
- Chroot or an ESP maintained separately: `sudo bash golden-image-setup-<distro>.sh --skip-disk --yes` (keeps GPT/ESP, still writes the latest boot artifacts)
- Physical machine without serial: `sudo bash golden-image-setup-<distro>.sh --no-serial`
- Internal or local package repository: `sudo bash golden-image-setup-<distro>.sh --keep-sources`, or `--mirror <URL>`
- Select a kernel: `sudo bash golden-image-setup-<distro>.sh -k <version>`

### 7. Script responsibilities and boundaries

The script performs A–F automatically: it installs dependencies and the kernel, installs the initramfs components, corrects fstab and GRUB, converts the GPT to a 4K-native layout and rebuilds the ESP, and runs the stateless checks. Changes to `fstab` and the GRUB defaults retain `.golden-*.bak` backups; a 512→4K GPT conversion backs up the original GPT to `/root/golden-image-gpt-512-*.bak`. These backups should be retained until the cold-boot validation passes.

The script does not perform the following operations: installing the operating system, creating or expanding the ESP, creating the root partition or filesystem, installing business software, or writing target/NQN, credentials or the control-plane address. When the ESP is smaller than 256 MiB, no GPT is found, or the root disk cannot be located, the script terminates instead of repairing the disk.

> `--skip-disk` skips only the GPT conversion and ESP rebuild, not the ESP content refresh: phase E still writes the current kernel, initrd, `BOOTX64.EFI` and grub.cfg into the ESP. After changing the kernel, rebuilding the initramfs or adjusting GRUB arguments, a `--skip-disk` rerun applies the new artifacts; if the ESP itself needs resizing or reformatting, a full phase E run is still required.

## Phases A–F

The script executes the following six phases in order.

### A. System and kernel

Phase A installs runtime dependencies such as `nvme-cli`, `jq`, `keyutils` and `ktls-utils`, and confirms that the kernel provides NVMe authentication and NVMe/TCP TLS. The kernel phase requires both DH-HMAC-CHAP authentication and TLS.

Debian 12 requires the 6.12 kernel from backports; the stock kernels in Debian 13, Ubuntu 24.04/26.04, Fedora 44, AlmaLinux 10.2 and openSUSE Leap 16.0 are sufficient. A kernel-phase `ret=401` or `authentication setup failed` means the authentication capability is missing.

### B. initramfs consumer

The initramfs must reconnect the system disk after the kernel starts. There are two integration styles:

| Framework | Integration | Key points |
| --- | --- | --- |
| initramfs-tools (Debian/Ubuntu) | `hooks/nbft` and `scripts/local-top/nbft` | `nbft_auto` kernel argument; dash/ash semantics; `tlshd` configuration |
| dracut (Fedora/AlmaLinux/Leap) | `95nbft` module | `--omit nvmf`; Fedora uses `systemd-networkd`, AlmaLinux and Leap use NetworkManager; `--no-hostonly`; explicit LVM activation on Fedora |

RHEL and SUSE additionally require `/etc/crypto-policies/back-ends/gnutls.config` in the initramfs; without it, `tlshd` exits with `gnutls: The request is invalid. (-50)` and the kernel-phase TLS session cannot be established.

`nbft-connect` checks NBCT (in-band credentials) first and issues `nvme connect` per entry; without NBCT it falls back to `connect-all --nbft`. It also injects a fixed hostid because the kernel's nvme-fabrics host tracking rejects a custom hostnqn under the default hostid.

Verify the initramfs contents:

```bash
# Debian / Ubuntu
lsinitramfs /boot/initrd.img-$(uname -r) | grep -E 'nbft|tlshd|nvme'

# dracut (Fedora / AlmaLinux / Leap)
lsinitrd /boot/initramfs-$(uname -r).img | grep -E 'nbft|tlshd|nvme'
lsinitrd /boot/initrd-$(uname -r) 2>/dev/null | grep -E 'nbft|tlshd|nvme'
```

### C. System configuration

- The root filesystem in `fstab` uses a **UUID**, not a device name such as `/dev/nvme0n1p2`.
- The `/boot/efi` line is commented out. The ESP is read by firmware and GRUB; mounting a 4K FAT ESP inside the system fails and enters emergency mode.
- DHCP remains enabled. The hostname is a placeholder; the production hostname is supplied by the control plane.
- Target addresses, ports, NQNs, authentication keys and the control-plane address are not written onto the disk.

### D. GRUB kernel arguments

```text
net.ifnames=0 biosdevname=0 ip=dhcp nbft_auto
```

dracut-based systems add `rd.neednet=1`. If NetworkManager or systemd-networkd would take over the same NIC after boot, the distribution script masks it; otherwise the connection is reset and the boot ends in keep-alive timeouts and `no usable path`.

`quiet` is removed: it suppresses info-level kernel messages, including the kernel-phase authentication success, and causes validation to misread the result.

GRUB must be ≥ 2.12. Debian 12's 2.06 was observed to hang on the SAN path. The script embeds GRUB 2.14 by default; `--grub-efi` can inject a host-built artifact.

### E. Disk layout

The master must use **4K-native GPT**: the GPT header at offset 4096, the partition entries at 8192, and the backup header at the end of the disk. The script requires an ESP of at least 256 MiB and rebuilds it as 4K FAT32 with:

```text
EFI/BOOT/BOOTX64.EFI    GRUB 2.14 (removable-media fallback)
EFI/BOOT/grub.cfg       SAN boot menu
vmlinuz-<kernel>        kernel
initramfs-<kernel>.img  initramfs (contains the NBFT consumer)
```

The initrd filename inside the ESP varies by variant (`initramfs-<kernel>.img` or `initramfs-<kernel>`) and must match the `initrd` line in grub.cfg; Leap renames `/boot/initrd-<kernel>` on copy.

The GRUB menu locates root with `root=UUID=<root-uuid>`; LVM roots also need `rd.lvm.lv=<vg>/<lv>`. The ESP volume UUID, the `search --fs-uuid` in `grub.cfg`, and the commented `fstab` line must agree.

For manual 512-GPT conversion and implementation details, see the disk chapters of the firmware repository's [master-image guide](https://github.com/dutyc/kurrent-firmware/blob/main/docs/golden-image-customization-debian12.md) and `test/shared/golden-esp-verify.sh`.

### F. Stateless check

The check covers identity and credentials that must not be present:

```bash
grep -rn "hostnqn\|dhchap\|secret\|kurrent:host" /etc /usr/local/sbin /usr/share/initramfs-tools 2>/dev/null
grep -n "HOSTNAME\|hostname" /etc/hostname /etc/hosts
```

Expect no target, NQN or key hits; the hostname should be a placeholder. The hostid is a fixed fallback inside `nbft-connect` and belongs to the firmware contract; it must not become an identity file on disk.

## Deploy the master to a storage node

1. Shut down the master VM and ensure the image is fully written.
2. Copy the image to the target storage node's `spec.agent.diskDir` with `_tpl_` in the filename. The file is a whole-disk image (GPT, ESP and root partition), not a partition image; `_tpl_` is case-insensitive:

   ```text
   /pool1/disk_dir/_tpl_debian12.img
   ```

3. Ensure the Agent container can read the file, then wait for the background scan. The master appears in `/masters` or the WebUI master list with `gpt` equal to `4k`.
4. When creating a system disk on a Worker, select this master. The control plane generates the `os_tag` and disk NQN; the Agent clones the image with reflink, nvmet creates the target, and a DHHC-1 credential is issued.
5. The master must not be exported directly to a device; it serves only as a clone source.

The control plane also supports creating an `empty` disk for manual installation; the production path is master cloning. An operating system installed onto an empty disk still has to pass the A–F checks before it can serve as a SAN system disk.

## Validation

- After cloning a single disk, the Worker's first system disk becomes the default automatically. The serial console should show one successful authentication in the firmware phase and one in the kernel phase, followed by rootfs mount and a `login:` prompt.
- No `0x7f22208e`, `ret=401`, keep-alive failures or emergency mode.
- If traffic is captured, the data plane should show TLS on `4421` only, with no plaintext `4420` and no plaintext PSK.
- The disk grep finds no identity or keys. Different system disks cloned from the same master have different NQNs, `os_tag` values and credentials.

The full cold-boot procedure belongs to the delivery workflow; this page covers master preparation and validation only.

## Troubleshooting

- **`Boot from SAN device ... failed` with `0x7f22208e`** — GPT is not 4K-native; rerun phase E or convert the GPT; confirm `/masters` reports `gpt=4k`.
- **Kernel-phase `ret=401` / `authentication setup failed`** — the kernel lacks NVMe authentication; use a kernel that meets the requirement; check `CONFIG_NVME_AUTH`.
- **Kernel-phase TLS failure** — `tlshd` missing from initramfs, too old, or `/etc/tlshd/config` missing; inspect the initramfs and the `tlshd` configuration.
- **Keep-alive timeout / `no usable path` after boot** — a systemd network service reset the boot connection; add the distribution script's `systemd.mask` arguments; check the cmdline.
- **Emergency mode, `boot-efi.mount` failed** — `fstab` still mounts `/boot/efi`; comment the line and rebuild initramfs and GRUB.
- **GRUB menu hangs** — GRUB is too old (for example 2.06); use the embedded 2.14 artifact or `--grub-efi`.
- **Master absent from the list** — filename lacks `_tpl_`, file is outside `diskDir`, or the Agent cannot read it; check the name, path and permissions; wait for the scan.
- **Clone reports 512 GPT** — master layout is invalid; run phase E on the master and confirm 4K GPT.

## Related pages

- Objects and placement: [Devices, Workers and Disks](../concepts/entities.md)
- Masters and data plane: [Storage and Data Plane](../concepts/storage.md)
- Full workflow: [First Delivery](./usage.md)
