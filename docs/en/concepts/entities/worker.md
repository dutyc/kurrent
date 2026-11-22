# Workers

A Worker is a compute identity. `worker_id` doubles as the hostname and is the index key for the ledger and credentials; it is not a machine identity and survives machine replacement, with system disks and credentials attached to it.

## Identity and Record

| Field | Meaning |
| --- | --- |
| `worker_id` | Compute identity; ledger primary key |
| Bound device | Projected from the device ledger, not stored on the Worker side |
| `disks[]` | Mounted system disks |
| `default_disk` | Default boot disk (`os_tag`) |
| `boot` | Optional menu entries and timeout overrides |
| `state` | `registered` (identity only) or `ready` (has a system disk) |

## Readiness

`readiness` is a read-only projection derived from the ledger:

| Value | Condition |
| --- | --- |
| `ready` | Bound device and at least one system disk |
| `partial` | Only a bound device, or only a system disk |
| `idle` | Neither |

## What's next

- Back to the [Entity Model](../entities.md)
- System disks and the default disk: [System Disks](./system-disk.md)
- Device binding: [Binding and Placement](./binding.md)
