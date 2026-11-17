# Storage and Data Plane

Storage nodes host master images and system disks and export block devices to devices. The control plane handles orchestration and credential issuance; block I/O never traverses it. In the model from the overview, this is where the system-disk object lives: the disk stays on the storage node, and devices connect to it under each boot's projection.

| Component | Location | Responsibility |
| --- | --- | --- |
| Storager Agent | Storage node `:4840` (mTLS) | Disk-file lifecycle, master scanning, cloning, deletion; drives the local backend |
| nvmet-host | Shared-volume UDS (mTLS) | Kernel configfs operations: subsystem / namespace / port / allowed_hosts / DHHC-1; injects TLS PSKs |
| Host kernel nvmet | Storage-node host | NVMe/TCP target on data-plane port `4421` |

![Storage path: cloning a system disk from a master and exporting it to the device data plane](/diagrams/storage-data-plane.svg)

nvmet is the primary backend. iSCSI (stgt / lio) is retained as a compatibility backend, but the main branch has no live boot path; the historical implementation lives in `iscsi-classic`. Backend differences are encapsulated in the Agent — the control plane only speaks HTTP.

## Master Images

A master image is the clone source and is never booted. It lives under the storage node's `spec.agent.diskDir`, with `_tpl_` in the filename. It must use 4K-native GPT and sit on a reflink-capable filesystem; otherwise cloning fails or degrades to a full copy.

Preparing and placing master images is covered in [Master Images](./storage/master-images.md).

## Credentials and TLS PSK

The DHHC-1 authentication key is stored per Worker and issued when a disk is created. The TLS PSK is derived from the authentication key and is not stored separately; rotating or revoking the key rotates or revokes the PSK with it. Push results are recorded and failures are retried by background reconciliation.

Key and PSK derivation, delivery and rotation are covered in [Credentials and TLS PSK](./storage/credentials.md).

## Data Plane

There is exactly one data-plane path between a device and a storage node: the firmware and kernel segments share `4421`, DH-HMAC-CHAP answers who may connect, TLS 1.3 protects content, and there is no plaintext fallback. When the port is not ready or the key is missing, `/boot-vars` withholds the payload.

Ports and readiness conditions are covered in [Data Plane and Readiness](./storage/data-plane.md).

## Related Pages

- The system-disk object and the default disk: [System Disks](./entities/system-disk.md)
- Credential projection at boot: [Boot Chain](./boot-chain.md)
- Node deployment and host prerequisites: [Deployment](../guide/deployment.md)
- Credential operations: [Operations](../guide/operations.md)
