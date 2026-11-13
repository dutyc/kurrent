# Architecture

Kurrent organizes physical devices, storage nodes and a control plane into a bootable, migratable compute system. Devices hold no persistent state: on power-up they complete identity verification over the network, receive a system disk and boot parameters, and boot with NVMe-oF as the root device; system disks are provided by storage nodes, and block I/O stays between the device and the storage node.

This page covers the roles, main data flows, trust boundaries, state ownership and failure domains.

![Architecture](/architecture.svg)

## Control Plane and Data Plane

The system is split along one clean line: the control plane handles identity, scheduling and configuration over HTTPS; the data plane carries NVMe-oF block I/O and never touches the control plane. A Worker's disk traffic flows directly between the device and its Storager host (TCP `4421`, TLS 1.3), so a control-plane outage degrades provisioning but never in-flight I/O. Every management operation converges on nginx `443`, and the data plane does not depend on the control-plane process staying alive.

## The Three Roles

### Controller

The Controller is the brain of the cluster. The control-plane HTTP service listens only on an in-container unix socket; nginx exposes `:443` as the only external entry for the WebUI, the management API, boot-chain bare paths and iPXE menus, with `:80` only redirecting. dnsmasq runs on the host network and serves DHCP, TFTP and boot files. State is persisted as plain files (YAML / JSONL) in the state root outside the repository (`control-plane/state/`, `control-plane/config/`, `control-plane/dnsmasq/`); there is no database. The control-plane declaration is authoritative, and artifacts such as `dnsmasq.conf` are rendered from it at startup. The WebUI and CLI are just REST clients, and the state location is decoupled from the repository.

### Storager

Storager is the block-storage side. Each node runs two components: the API Agent (`:4840`, mTLS) owns the disk-file lifecycle, master scanning and reflink cloning, and drives the local backend over HTTP; nvmet-host exposes an mTLS management plane only over a shared-volume UDS, translates commands into configfs operations, and injects TLS pre-shared keys into the kernel's global `.nvme` keyring. The data-plane port is `4421`, with TLS 1.3 enforced and shared by the firmware and kernel segments. The backend of choice is nvmet; the iSCSI backend code is retained but has no live boot path on the main branch, and backend differences are encapsulated inside the Agent.

### Devices

Devices are the stateless compute side. A physical device holds no persistent state: on PXE boot it reports its fingerprint (MAC / UUID / SMBIOS / CPU / memory / NIC — 11 fields) and is admitted into the device pool; after being bound to a Worker and given a system disk, it boots with NVMe-oF as its root device. Block I/O travels the data plane, never the control plane.

## Three Entities

The role layer maps onto three objects: Device, Worker and System Disk. Disks are identified by NQN, with IQN derived from it; the binding is authoritative on the device side and projected onto the Worker. Unbinding or rebinding returns the device to the pool while the system disk stays with the Worker; a Worker may hold several system disks, and the default boot disk can be switched online. `readiness` is decided by both a bound device and a system disk.

| Entity | Identity | Notes |
| --- | --- | --- |
| Device | MAC (unique) | Physical machine, ledger entry; the authoritative side of the binding |
| Worker | `worker_id` (== hostname) | Compute identity; a bound device becomes that Worker's booting node |
| System Disk | NQN | Storage volume (master clone or empty); multiple disks per Worker |

## Data Flow

Management requests terminate at `443`; provisioning is pushed from the control plane to storage nodes. Neither path uses the data-plane port.

### Management Request

A management request reaches nginx first, which validates the certificate chain and CRL and injects identity headers only on `SUCCESS`; the request then crosses the unix socket into the control plane, which runs the authenticator chain, principal-class and RBAC checks, reads or writes ledgers, writes audit records and calls an Agent when needed. Identity headers are trusted only when injected by nginx; other proxied locations clear them.

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

Provisioning calls the Agent over `4840` mTLS; the Agent then calls nvmet-host over the UDS, which writes configfs and returns the result, while the TLS PSK goes into the kernel keyring. Disk creation issues the Worker-bound DHHC-1 credential at the same time; failed pushes are written back to the ledger and retried by background reconciliation.

```text
control plane
  → 4840 Agent (mTLS)
      → UDS nvmet-host (mTLS)
          → configfs: subsystem / namespace / port / allowed_hosts / dhchap_key
          → kernel .nvme keyring: TLS PSK
```

## Boot Chain

Boot starts with the DHCP and TFTP first hop: the device fetches iPXE and its pointer, then retrieves the configuration over HTTPS. Device identity is a non-exportable AK inside the TPM, which quotes PCR 0/2/4/7 on every boot; an unknown MAC enters the pool only while the registration window is open with a valid AK and Secure Boot enabled. `/boot-vars` checks the binding, quote signature, nonce and platform baseline in order, and only then projects root-path, NQN and credentials; the device connects with DH-HMAC-CHAP and encrypts with TLS 1.3. During sanboot the firmware installs the NBCT credential table, and initramfs consumes NBFT and NBCT to reconnect and mount the root filesystem — no machine identity is written into the disk.

```text
device
  → DHCP / TFTP (first hop)
  → HTTPS: scripts, device report, challenge, /boot-vars
  → 4421: NVMe/TCP + TLS 1.3, authentication and I/O
  → NBFT / NBCT → initramfs
```

The control plane participates only in the HTTPS segment; the data plane connects the device directly to the storage node. Step-by-step detail is in [Boot Chain](../concepts/boot-chain.md).

## Security Boundary

Management authentication tries three credentials in order — mTLS client certificate, management token and server-side session — and yields one `Identity` for principal-class and RBAC checks. Credential domains are split into three independent roots (users, nodes, control); nginx trusts only the users and nodes bundles, derives the principal class from the issuer, and every control-plane route declares the classes it accepts with deny-by-default. Every management request writes an audit record; if the write fails the request returns 503 (fail-closed).

Device identity rests on the TPM AK, single-use nonce PCR quotes, the platform baseline and binding-as-authentication; fingerprint reports grant no authority and only feed the device pool. Authentication and encryption are independent layers: DH-HMAC-CHAP answers who may connect, TLS 1.3 protects the content, and the PSK is a pure function of the authentication key with no plaintext fallback. The full threat model is in [Security Model](./security.md).

## State and Derived Artifacts

The control-plane declaration and the node declaration are the only inputs; the former defines networking, PKI, the registration window and boot policy, while the latter defines the data directory, backend and NQN namespace. Runtime state lives under `<state>`, defaulting to `~/.local/state/kurrent` and pinned by `~/.kurrent/deployment.yaml`; ledgers, sessions, tokens, audit and PKI all live there, decoupled from the repository.

Trust material also lives under the state root: three root CAs (users, nodes, control) issue human, component and control-plane certificates; the TOFU server certificate and CRLs serve nginx and the boot chain; node identity, bootstrap credentials, component PKI and logs live under `<state>/storager`. On first deployment the management credential is handed to the deploying user through the one-shot exchange at `<state>/control-plane/bootstrap`; the bundle is deleted once consumed, while the authoritative `admin.conf` stays in the control-plane state directory.

Derived artifacts flow one way from authority: the dnsmasq configuration is rendered from `spec.networking`, the boot menu and default disk are projected by the control plane, and the WebUI and boot scripts are built from the repository. There is no database; the control plane is a single node with plain-file state, so state backup, file permissions and single-node failure boundaries are the deployment's responsibility. With state outside the repository, moving the checkout no longer affects ledgers or boot identities.

## Ports

Ports are grouped by ownership to make the exposed surface explicit: the control-plane host offers 443 (80 only redirects) plus the DNS, DHCP and TFTP ports required for boot, while the management plane itself has no TCP listener; storage nodes expose 4840 and 4421, where 4840 accepts only control-plane mTLS and 4421 enforces TLS 1.3; devices need only DHCP, TFTP, HTTPS and the storage-node data plane.

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

## Failure Domains

Failure impact follows process ownership: a stopped control plane, nginx or dnsmasq affects only new provisioning, the management plane and booting, never established data-plane I/O; a single storage node going down affects only the disks on that node; losing the control-plane host requires a restore from backup, and losing PKI or server certificates forces components to re-enroll and devices to re-trust; a lost device is recovered by rebinding, while the Worker identity and its system disks remain.

| Failure | Impact |
| --- | --- |
| Control plane stops | New provisioning, new boots, configuration changes and audit stop; established NVMe/TCP I/O continues |
| nginx stops | Management and boot-chain HTTPS unavailable; data plane unaffected |
| dnsmasq stops | New devices cannot DHCP/TFTP boot; running systems unaffected |
| One storage node stops | Disks on that node cannot boot or lose I/O; other nodes unaffected |
| Control-plane host lost | Restore from backup; losing PKI/certificates forces components to re-enroll and devices to re-trust |
| Device lost | Rebind a new device; system disk and Worker identity remain |

## Protocol Evolution

The data plane has moved from iSCSI to NVMe-oF (NVMe/TCP). The firmware main branch carries the complete patch set (`0001`–`0010`), and cold boot has been verified on several distributions. The semantics — stateless compute, disk/machine decoupling and an identity chain from MAC to boot — do not depend on a particular protocol: the Storager backend is abstracted by the Agent, so replacing the transport does not touch the architecture. iSCSI has no live boot path on the main branch; its historical implementation lives in the `iscsi-classic` branch.

## Extension Points

Storage backend differences are encapsulated inside the Agent, and the control plane only speaks HTTP; the boot menu, timeout and default disk are projected by the control plane and never written into the disk; firmware is built from patches in a separate repository and can be rebuilt on any upstream baseline. Future directions (high availability, IPv6, ARM, data disks) are in the [Roadmap](../about/roadmap.md).

This page describes the architecture at the current stage: the better solution under today's constraints, not a final form. The goal is stateless cloud-native operation — compute, storage and identity decoupled from individual machines, so the system as a whole can be scheduled, migrated and rebuilt; today's components, protocols and deployment model are one implementation on that path and will keep changing. There is no best architecture, only a better one.

## Further Reading

- Entities and binding: [Devices, Workers and System Disks](../concepts/entities.md)
- Boot sequence: [Boot Chain](../concepts/boot-chain.md)
- Trust boundaries and threat model: [Security Model](./security.md)
- Declarations and convergence: [Declarative Configuration](../concepts/configuration.md)
