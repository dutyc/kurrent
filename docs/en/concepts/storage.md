# Storage and Data Plane

Storage nodes host master images and system disks and export block devices to devices. The control plane handles orchestration and credential issuance; block I/O never traverses it.

| Component | Location | Responsibility |
| --- | --- | --- |
| Storager Agent | Storage node `:4840` (mTLS) | Disk-file lifecycle, master scanning, cloning, deletion; drives the local backend |
| nvmet-host | Shared-volume UDS (mTLS) | Kernel configfs operations: subsystem / namespace / port / allowed_hosts / DHHC-1; injects TLS PSKs |
| Host kernel nvmet | Storage-node host | NVMe/TCP target on data-plane port `4421` |

nvmet is the primary backend. iSCSI (stgt / lio) is retained as a compatibility backend, but the main branch has no live boot path; the historical implementation lives in `iscsi-classic`. Backend differences are encapsulated in the Agent — the control plane only speaks HTTP.

## Master Images

A master image is a clone source and is never booted. It lives under the storage node's `spec.agent.diskDir`, with `_tpl_` in the filename (case-insensitive):

```text
/pool1/disk_dir/_tpl_ubuntu2604.img
```

A background scan caches the master list with:

- filename, size and mtime;
- GPT sector layout (`4k` / `512` / `none`);
- manually registered tags (`os`, `os_version`, remark).

Requirements:

- **4K-native GPT**. The nvmet file backend exports according to the underlying filesystem block size (typically 4096 B), so a 512-byte-sector GPT is seen as having no partition table by firmware. The Agent refuses cloning and nvmet-host refuses export; the boot reports `Boot from SAN device ... failed`.
- **Reflink filesystem**. btrfs / xfs clone in near-constant time; ext4 does not support reflink and degrades to a full copy.
- Master tags are display and selection metadata only; they do not participate in identity or data-plane operations.

## System Disks

The control plane creates system disks from two sources:

| Type | Description |
| --- | --- |
| `master` | Clone from a named master; the master must exist locally on that Agent |
| `empty` | Create an empty disk of the requested size for OS installation |

At creation the control plane generates `os_tag`, the disk NQN and the backing filename, and records them in the Worker ledger:

```text
nqn      = <nqnBase>:<worker_id>.<os>.<os_tag>
filename = <worker_id>.<os>.<os_tag>.img
```

- A Worker holds at most one disk per `(os, os_version)`; different versions can coexist.
- The first disk becomes the default automatically; later disks do not rewrite the default.
- Placement is decided by the control plane: candidates must hold the master, be enabled and report a ready data plane, with the fewest disks chosen first. See [Devices, Workers and System Disks](./entities.md#placement).

Deleting a disk can optionally delete the backing file. Removing only the target keeps the file mountable again; deleting the default disk promotes a remaining one.

## Data Plane

There is exactly one data-plane path between a device and a storage node:

```text
device firmware / kernel ── NVMe/TCP :4421 (TLS 1.3) ──► nvmet target
```

- **One port**. The firmware and kernel segments share `4421`; there is no second plaintext port to fall back to.
- **Encrypted by default**. TLS 1.3 is performed by firmware for the first segment; the kernel and initramfs reconnect on the same port with `--tls`.
- **Authentication and encryption are separate**. DH-HMAC-CHAP controls which initiators may connect; TLS 1.3 protects content.
- **Host NQN**. Each Worker has its own initiator Host NQN, registered in `allowed_hosts` on the target; strict-mode authentication does not accept unknown hosts.
- **Credentials follow the Worker**. The DHHC-1 key is stored per Worker and shared by its disks; rotation or revocation applies to all of them.

### TLS PSK

The TLS pre-shared key is not a separately stored field; it is a pure function of the authentication key:

```text
PSK = HMAC-SHA256(raw_dhhc1_key, "kurrent-nvme-tls-psk-v1")
```

Rotating or revoking the authentication key therefore rotates or revokes the PSK atomically. On the storage node, nvmet-host injects it into the kernel's global `.nvme` keyring; on the device, `nbft-tls-psk` and `nbft-tls-port` are projected by `/boot-vars`.

`/boot-vars` releases the PSK only when all of the following hold:

- the Worker is bound;
- a key entry exists;
- the backend is nvmet;
- the storage node reports a ready data-plane port.

If a condition fails, the PSK is not projected; on nvmet, a missing key or an unready port also withholds the whole boot-variable payload, and the control plane refuses the boot and records an audit event.

## Credential Lifecycle

### Issuance

When a disk is created on an nvmet backend, the control plane checks whether the Worker already has an authentication key; if not, it generates a DHHC-1 key and pushes it. Existing keys are not rotated by creating another disk.

### Push and Reconciliation

The control plane pushes the key to every Agent that holds one of the Worker's disks. The Agent calls nvmet-host to write `dhchap_key`, `allowed_hosts` and the kernel keyring entry.

Push results are recorded:

- delivered: applied on the target;
- partially effective: for example, node TLS not ready;
- not delivered: the Agent is unreachable or application failed; background reconciliation retries.

The reconciliation period is `spec.credentials.reconcileIntervalSeconds`. A failed rotation cannot leave a mismatch between "new key in boot variables" and "old key in configfs", because undelivered items are replayed continuously.

### Rotation and Revocation

```bash
kurrent workers credential set worker-01 --generate
kurrent workers credential revoke worker-01
```

- Rotation generates a new key locally and never prints plaintext; the old key and old PSK are invalidated together.
- After revocation, the next `/boot-vars` withholds the payload and the device cannot connect.
- Key removal on the target is guarded by a generation number, so concurrent pushes cannot write an old key back.
- Deleting a disk, subsystem or Worker removes the associated target, key and push record; orphaned entries are garbage-collected.

### Plaintext Boundary

Key plaintext exists only where the chain requires it:

- control-plane `state/credentials.yml`;
- Agent cache (including the derived PSK);
- storage-node configfs `dhchap_key`;
- storage-node kernel `.nvme` keyring.

These files and interfaces are not additionally encrypted at the application layer; the deployment owns disk encryption, host root boundaries and encrypted backups. APIs and logs never print keys.

## Data-Plane Readiness

A storage node creates the `4421` listener at startup and reports its capability. The control plane uses the live capability reported by the Agent, not the declaration:

- if the port is not ready, `/boot-vars` withholds credentials;
- a kernel without NVMe/TCP TLS support, `tlshd < 1.5.0`, or a stopped service makes nvmet-host report the port as unready;
- data-plane failure never falls back to plaintext or silently uses another port.

Host prerequisites and diagnostics are in [Deployment](../guide/deployment.md).

## Common Failures

| Symptom | Direction |
| --- | --- |
| Disk creation reports no candidate | Master missing on candidates; node drained; data plane not ready |
| Clone fails | Master uses 512-byte-sector GPT; filesystem lacks reflink; target already exists |
| Boot reports `Boot from SAN device ... failed` | Disk GPT layout does not match the export block size; check `gpt` in `/masters` |
| Authentication fails | Host NQN mismatch; key not delivered; target has no allowed host |
| TLS handshake fails | PSK missing from the keyring; `tlshd` version or service; kernel TLS support |
| An old key still connects after rotation | Push incomplete or not applied; check push status and Agent logs |

## Related Pages

- Objects and placement: [Devices, Workers and System Disks](./entities.md)
- Credential projection at boot: [Boot Chain](./boot-chain.md)
- Node deployment and host prerequisites: [Deployment](../guide/deployment.md)
- Credential operations: [Operations](../guide/operations.md)
