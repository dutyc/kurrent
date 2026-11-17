# Credentials and TLS PSK

Data-plane credentials belong to the Worker: the disks of one Worker share a single DHHC-1 authentication key, and rotation or revocation applies to all of them. The control plane stores and pushes the key but never handles block I/O.

## Issuance

When a disk is created on an nvmet backend, the control plane checks whether the Worker already has an authentication key; if not, it generates a DHHC-1 key and pushes it. Existing keys are not rotated by creating another disk.

## Push and Reconciliation

The control plane pushes the key to every Agent that holds one of the Worker's disks. The Agent calls nvmet-host to write `dhchap_key`, `allowed_hosts` and the kernel keyring entry.

Push results are recorded:

- delivered: applied on the target;
- partially effective: for example, node TLS not ready;
- not delivered: the Agent is unreachable or application failed; background reconciliation retries.

The reconciliation period is `spec.credentials.reconcileIntervalSeconds`. A failed rotation cannot leave a mismatch between "new key in boot variables" and "old key in configfs", because undelivered items are replayed continuously.

## Rotation and Revocation

```bash
kurrent workers credential set worker-01 --generate
kurrent workers credential revoke worker-01
```

- Rotation generates a new key locally and never prints plaintext; the old key and old PSK are invalidated together.
- After revocation, the next `/boot-vars` withholds the payload and the device cannot connect.
- Key removal on the target is guarded by a generation number, so concurrent pushes cannot write an old key back.
- Deleting a disk, subsystem or Worker removes the associated target, key and push record; orphaned entries are garbage-collected.

## TLS PSK

The TLS pre-shared key is derived from the authentication key and is not stored separately:

```text
PSK = HMAC-SHA256(raw_dhhc1_key, "kurrent-nvme-tls-psk-v1")
```

Rotating or revoking the authentication key therefore rotates or revokes the PSK atomically. On the storage node, nvmet-host injects it into the kernel's global `.nvme` keyring; on the device, `nbft-tls-psk` and `nbft-tls-port` are projected by `/boot-vars`.

## Plaintext Boundary

Key plaintext exists only where the chain requires it:

- control-plane `state/credentials.yml`;
- Agent cache (including the derived PSK);
- storage-node configfs `dhchap_key`;
- storage-node kernel `.nvme` keyring.

These files and interfaces are not additionally encrypted at the application layer; the deployment owns disk encryption, host root boundaries and encrypted backups. APIs and logs never print keys.

## Related Pages

- Back to [Storage and Data Plane](../storage.md)
- How credentials enter boot variables: [Data Plane](../boot-chain/data-plane.md)
- Port readiness and handshake failures: [Data Plane and Readiness](./data-plane.md)
- Rotation and revocation operations: [Operations](../../guide/operations.md)
