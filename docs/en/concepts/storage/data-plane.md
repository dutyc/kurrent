# Data Plane and Readiness

There is exactly one data-plane path between a device and a storage node, and the control plane never handles block I/O. Readiness follows the live capability a storage node reports; declarations are not used as evidence. If the port is not ready, the control plane withholds credentials.

## One Port, Two Layers

- **One port**. The firmware and kernel segments share `4421`; there is no second plaintext port to fall back to.
- **Encrypted by default**. TLS 1.3 is performed by firmware for the first segment; the kernel and initramfs reconnect on the same port with `--tls`.
- **Authentication and encryption are separate**. DH-HMAC-CHAP answers who may connect; TLS 1.3 protects content.
- **Host NQN**. Each Worker has its own initiator Host NQN, registered in `allowed_hosts` on the target; strict-mode authentication does not accept unknown hosts.
- **Credentials belong to the Worker**. The DHHC-1 key is stored per Worker and shared by its disks.

## When the PSK Is Released

`/boot-vars` releases the PSK only when all of the following hold:

- the Worker is bound;
- a key entry exists;
- the backend is nvmet;
- the storage node reports a ready data-plane port.

If a condition fails, the PSK is not projected; on nvmet, a missing key or an unready port also withholds the whole boot-variable payload, and the control plane refuses the boot and records an audit event. The derivation is in [Credentials and TLS PSK](./credentials.md).

## Readiness

A storage node creates the `4421` listener at startup and reports its capability. The control plane uses the live capability reported by the Agent:

- if the port is not ready, `/boot-vars` withholds credentials;
- a kernel without NVMe/TCP TLS support, `tlshd < 1.5.0`, or a stopped service makes nvmet-host report the port as unready;
- data-plane failure never falls back to plaintext or silently uses another port.

## Common Failures

| Symptom | Direction |
| --- | --- |
| Boot reports `Boot from SAN device ... failed` | Disk GPT layout does not match the export block size; check `gpt` in `/masters` |
| Authentication fails | Host NQN mismatch; key not delivered; target has no allowed host |
| TLS handshake fails | PSK missing from the keyring; `tlshd` version or service; kernel TLS support |
| An old key still connects after rotation | Push incomplete or not applied; check push status and Agent logs |

## What's next

- Back to [Storage and Data Plane](../storage.md)
- Admission and variable projection: [Data Plane](../boot-chain/data-plane.md)
- Host prerequisites and diagnostics: [Deployment](../../setup/deployment.md)
