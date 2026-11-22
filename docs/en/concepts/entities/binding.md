# Binding and Placement

Binding decides which identity a device boots under; placement decides which storage node holds a system disk. Both are decided by the control plane: binding lives in the device ledger, placement is computed whenever a disk is created.

## Binding

Binding is one-to-one: a device belongs to exactly one Worker, and a Worker has exactly one bound device.

- **The authority is on the device side.** `bound_worker_id` in `devices.yml` is the single fact; the bound device in a Worker response is only a projection. The dnsmasq hostname mapping is also a derived projection that may lag and plays no part in boot identity.
- **Binding is authentication.** `/boot-vars` checks that the request MAC belongs to the matched Worker's bound device, so another device cannot claim the identity.
- **Rebinding is atomic.** With `force=true`, the new binding is written before the old one is cleared; on failure the ledger snapshot is restored.
- **Unbinding only affects the device.** The device returns to the pool, while the Worker's system disks, default disk and credentials stay.

### Rebinding as Cold Migration

Rebinding a Worker from device A to device B is a cold migration:

1. Confirm B is registered with a device AK; the Worker's credentials are already effective on the storage node.
2. Shut down or isolate A (there is no live migration or data-plane fencing today).
3. Rebind to B on the Devices page; the system disk and Worker identity stay unchanged.
4. After B boots and passes attestation, it obtains the connection information for the same system disk.

Moving back from B to A follows the same path; there is no automation.

## Placement

When a system disk is created, the control plane decides its location (unless `disk_agent` or a pin is given explicitly):

1. A candidate must be a disk-role Agent that is enabled (not drained), has a reachable `base_url`, and reports a ready data-plane port.
2. When cloning, the master image must already exist locally on that node.
3. Among candidates, the node with the fewest existing system disks is chosen (spreading).
4. An explicit pin overrides automatic placement, but the pinned node must still pass the availability checks.

Batch creation uses the same placement implementation for `dry_run` and execution, so the preview is the execution plan.

## Deletion Semantics

| Operation | Result |
| --- | --- |
| Delete a Worker | Unbind the device, remove the Worker's credential entry and push records; disk files handled by option |
| Unbind a device | The device returns to the pool; the Worker and its system disks stay unchanged |
| Revoke a device | The device becomes `revoked` and accepts no reports; cleanup only removes `revoked` entries |

After a Worker is deleted, recreating a Worker with the same name does not inherit old credentials: the credential entry was removed with the Worker. A kept disk file can be mounted again, but its NQN and ledger record must be re-established.

## What's next

- Back to the [Entity Model](../entities.md)
- Device registration and revocation: [Devices](./device.md)
- System disks and the default disk: [System Disks](./system-disk.md)
- Master image presence requirements: [Master Images and System Disks](../storage/master-images.md)
