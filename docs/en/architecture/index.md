# Architecture

Kurrent consists of a control plane, storage nodes and devices. The control plane handles identity, ledgers, scheduling and configuration over HTTPS; the data plane carries block I/O between devices and storage nodes and never traverses the control plane.

![Architecture](/architecture.svg)

## Invariants

| Invariant | Meaning |
| --- | --- |
| Control/data separation | A control-plane outage affects provisioning and boots but not established data-plane I/O |
| Nodes hold no system state | System, identity and data come from the network; devices retain only a boot identity anchor |
| One authority per fact | Binding follows the device ledger, disks follow their NQN, declarations follow YAML; derived artifacts never write back |
| Deny by default | Management access is granted by declared principal classes; data-plane boot is refused when credentials or the port are not ready |
| Idempotent convergence | `init` / `join` can be rerun; failures roll back without half-applied state |

## Components

| Component | Location | Responsibility |
| --- | --- | --- |
| Control-plane service | Control-plane container, unix socket | Device/Worker ledgers, binding, storage scheduling, boot variables, audit, configuration status |
| nginx | Control-plane container, `:443` | The only external entry: WebUI, `/api/cp/`, read-only boot endpoints, iPXE assets and menu |
| dnsmasq | Control-plane host, host network | DHCP / TFTP; `dnsmasq.conf` is rendered from the declaration |
| WebUI | nginx static assets | A REST client; session cookie or mTLS |
| CLI | Operator shell | Declaration generation, validation, convergence, credentials and ledgers |
| Agent | Storage node, `:4840` mTLS | Disk-file lifecycle, master scanning, backend commands |
| nvmet-host | Storage node, shared-volume UDS | configfs operations, host matrix, DHHC-1, TLS PSK injection |
| Host kernel nvmet | Storage-node host | NVMe/TCP target on data-plane port `4421` |
| Device firmware | Device | iPXE network boot, device key, TOFU, NVMe/TCP and NBCT |

Firmware is built from a separate repository; see [Firmware](../firmware/index.md).

## Ports

### Control-Plane Host

| Port | Protocol | Purpose |
| --- | --- | --- |
| 53 | UDP/TCP | dnsmasq DNS |
| 67 / 69 | UDP | DHCP / TFTP |
| 80 | TCP | 301 redirect to 443 only |
| 443 | TCP | The only HTTPS entry |
| — | unix socket | Control-plane management plane; nginx and health checks |

The control plane has no TCP listener and does not mount `docker.sock`.

### Storage Node

| Port | Protocol | Purpose |
| --- | --- | --- |
| 4840 | TCP | Agent HTTPS (mTLS, called by the control plane) |
| 4421 | TCP | NVMe/TCP data plane, TLS 1.3 enforced; shared by firmware and kernel |
| — | Shared-volume UDS | nvmet-host management plane (mTLS) |
| 3260 | TCP | iSCSI compatibility backend (no live boot path on mainline) |

### Device

A device only needs DHCP, TFTP, HTTPS and the storage-node data plane; it does not access the management plane directly.

## Data Flow

### Management Request

```text
browser / CLI
  → 443 nginx
      → mTLS validation (users/nodes bundle + CRL)
      → identity headers injected (SUCCESS only)
  → unix socket control plane
      → authenticator chain (certificate / token / session)
      → principal classes and RBAC
      → ledgers / audit / Agent calls
```

### Provisioning and Credentials

```text
control plane
  → 4840 Agent (mTLS)
      → UDS nvmet-host (mTLS)
          → configfs: subsystem / namespace / port / allowed_hosts / dhchap_key
          → kernel .nvme keyring: TLS PSK
```

Push results are written back to the ledger; failures are retried by background reconciliation.

### Boot

```text
device
  → DHCP / TFTP (first hop)
  → HTTPS: scripts, device report, challenge, /boot-vars
  → 4421: NVMe/TCP + TLS 1.3, authentication and I/O
  → NBFT / NBCT → initramfs
```

The control plane participates only in the HTTPS segment; the data plane connects the device directly to the storage node.

## State and Derived Artifacts

| Content | Location | Nature |
| --- | --- | --- |
| Control-plane declaration | `control_plane/kurrent.yaml` | Authoritative input |
| Node declaration | `storager/kurrent.yaml` | Authoritative input |
| Devices, Workers, disks, credentials, sessions, tokens | `control_plane/state/*.yml` | Runtime ledgers |
| Audit | `control_plane/state/audit.jsonl` | Append-only log |
| PKI | `control_plane/state/pki/` | Trust roots and component certificates |
| TOFU server certificate | `control_plane/state/certs/` | Boot-chain certificate and fingerprint |
| dnsmasq configuration | `dnsmasq/dnsmasq.conf` | Derived from the declaration; never edited by hand |
| Boot scripts | `tftp/` | Menu and scripts; firmware binaries come from the firmware repository |

There is no database. The control plane is a single node with plain-file state; state backup, permissions and single-node failure boundaries are the deployment's responsibility.

## Failure Domains

| Failure | Impact |
| --- | --- |
| Control plane stops | New provisioning, new boots, configuration changes and audit stop; established NVMe/TCP I/O continues |
| nginx stops | Management and boot-chain HTTPS unavailable; data plane unaffected |
| dnsmasq stops | New devices cannot DHCP/TFTP boot; running systems unaffected |
| One storage node stops | Disks on that node cannot boot or lose I/O; other nodes unaffected |
| Control-plane host lost | Restore from backup; losing PKI/certificates forces components to re-enroll and devices to re-trust |
| Device lost | Rebind a new device; system disk and Worker identity remain |

## Extension Points

- **Storage backends**: the Agent encapsulates nvmet / iSCSI differences; the control plane only speaks HTTP.
- **Transport**: the stateless semantics do not depend on a particular block protocol; replacing the transport does not change the entity model.
- **Boot behaviour**: menu, timeout and default disk are projected by the control plane and never written into the disk.
- **Firmware**: built from patches in a separate repository, reproducible on any upstream baseline.

Future directions (high availability, IPv6, ARM, data disks, Windows/ESXi) are in the [Roadmap](../about/roadmap.md).

## Further Reading

- Full repository architecture document: [about/en/ARCHITECTURE.md](https://github.com/dutyc/kurrent/blob/main/about/en/ARCHITECTURE.md)
- Entities and binding: [Devices, Workers and System Disks](../concepts/entities.md)
- Boot sequence: [Boot Chain](../concepts/boot-chain.md)
- Trust boundaries and threat model: [Security Model](./security.md)
- Declarations and convergence: [Declarative Configuration](../concepts/configuration.md)
