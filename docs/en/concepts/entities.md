# Devices, Workers and System Disks

Kurrent splits the compute side into three independent entities: devices, Workers and system disks. They answer which machine, under whose identity and which system disk, and binding combines them into a bootable system, so machines, identities and disks can be replaced separately.

| Entity | Identifier | Authority | Question |
| --- | --- | --- | --- |
| Device | MAC | Device ledger `state/devices.yml` | Which physical machine |
| Worker | `worker_id` (hostname) | Worker ledger `state/workers.yml` | Under which identity to run |
| System Disk | NQN | Disk file on the storage node + Worker ledger | Which system to boot |

![Entity model: identifiers, authority and binding for devices, Workers and system disks](/diagrams/entities.svg)

## Device

A device represents a physical machine. The control plane records it by MAC as the unique key, along with its fingerprint, TPM AK public key, attestation state and binding; `pooled` / `bound` / `revoked` describe its lifecycle.

Pooling only means "this machine has been here" and grants no boot authority; a device becomes a Worker's boot node only after it is bound.

Identity, enrollment and lifecycle are covered in [Devices](./entities/device.md).

## Worker

A Worker is a compute identity. `worker_id` doubles as the hostname and is the index key for the ledger and credentials; it is not a machine identity and survives machine replacement.

A Worker record holds its bound device (a projection), its system disks and default disk; `readiness` is derived from whether it has a bound device and a system disk.

Compute identity and readiness are covered in [Workers](./entities/worker.md).

## System Disk

A system disk is a disk file on a storage node, with the NQN as its authoritative data-plane identifier. A disk can come from a master image or be blank; the first disk becomes the default automatically and later disks do not rewrite it.

Disk sources, identifiers and default-disk rules are covered in [System Disks](./entities/system-disk.md).

## Binding and Placement

Binding is one-to-one, and the authority is `bound_worker_id` on the device side; rebinding is a cold migration. When a disk is created, placement is decided by the control plane: candidates must hold the master, be enabled and report a ready data plane, with the fewest disks chosen first.

Binding rules and placement are covered in [Binding and Placement](./entities/binding.md).

## What's next

- How the objects form one boot: [Boot Chain](./boot-chain.md)
- Master images, credentials and the data plane: [Storage and Data Plane](./storage.md)
- Device and human identity: [Identity and Access](./identity.md)
- Delivery steps: [Deployment](../setup/deployment.md)
