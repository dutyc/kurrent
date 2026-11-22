# Master Images

A master image is the clone source for system disks and is never booted. It lives under the storage node's `spec.agent.diskDir`, with `_tpl_` in the filename (case-insensitive):

```text
/pool1/disk_dir/_tpl_ubuntu2604.img
```

A background scan caches the master list with filename, size and mtime, the GPT sector layout (`4k` / `512` / `none`), and manually registered tags (`os`, `os_version`, remark). Tags are display and selection metadata only and play no part in identity or data-plane operations.

## Requirements

- **4K-native GPT**. The nvmet file backend exports according to the underlying filesystem block size (typically 4096 B), so a 512-byte-sector GPT is seen as having no partition table by firmware. The Agent refuses cloning and nvmet-host refuses export; the boot reports `Boot from SAN device ... failed`.
- **Reflink filesystem**. btrfs / xfs clone in near-constant time; ext4 does not support reflink and degrades to a full copy.

## Cloning a System Disk

When a disk is created, the control plane requires the master to exist locally on the target Agent and selects the node by the placement rules; the Agent performs the clone and the control plane moves no data. The rules are in [Binding and Placement](../entities/binding.md).

A blank disk is the other source: it needs no master and is used for installing a system.

## What's next

- Back to [Storage and Data Plane](../storage.md)
- System-disk identity and the default disk: [System Disks](../entities/system-disk.md)
- Troubleshooting boot failures: [Troubleshooting](../../tasks/troubleshooting.md)
