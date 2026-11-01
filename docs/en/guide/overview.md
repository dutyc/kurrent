# Project Overview

Kurrent is a stateless delivery system for bare-metal compute. It separates identity, system and data from the physical machine: a compute node holds no persistent business state, and boot-time identity, system disk and credentials are granted by the network and the control plane. The node therefore becomes a replaceable location, and the system becomes the object that is delivered and scheduled.

```text
Identity lives in the device ledger, the system disk on storage,
the declaration in configuration files, and credentials are issued by the control plane.
What remains on the node is a temporary connection for one boot.
```

## The Problem

Cloud-native practice over the past decade made applications elastic: containers can be rebuilt, moved and upgraded without being tied to a machine. The physical nodes underneath, however, are still maintained the traditional way — installing systems, configuring networks, reinstalling, replacing hardware and migrating by hand or through a separate infrastructure stack.

Kurrent addresses that layer: **make the machine that carries a system replaceable as well**. It does not replace application orchestration, and it does not hide state behind another storage abstraction. It redefines the origin of identity and the system at boot, so that replacing the machine no longer requires reinstalling the system.

## Core Propositions

### The node holds no persistent state

A compute node can be powered off, replaced or re-racked at any time. Its system comes from the network, its identity is granted by the control plane according to the binding, and its data lives on the system disk. The node keeps no local copy of the system and needs no per-machine installation record.

What remains in device NVRAM is the boot identity key and the server certificate fingerprint. These are **identity anchors**, not system state: they carry no business data, do not migrate with the system, and require re-registration if lost.

### The system is the unit of delivery

What is delivered is not an application process but a bootable system: compute identity (Worker), system disk, boot credentials and the device binding together define it. A system belongs to an identity, not to a machine; the machine is merely where the system currently resides.

This determines the control plane's object model: Device, Worker and system disk are three independent entities, the binding has a single authority, and disks are decoupled from machines. Applications and higher-level orchestration run inside the system and are outside Kurrent's scheduling scope.

### Identity and system disk are decoupled

A device is identified by its MAC, compute identity is represented by a Worker, and a system disk is identified by its NQN. The binding between them can change: rebinding a device to another Worker is a cold migration; the system disk stays where it is and does not need to be recreated.

Machine identity is not written into the disk. At boot the control plane projects connection information according to the current binding, so the same disk can boot on a different device provided that identity and credentials match.

### The data plane does not traverse the control plane

Block I/O between devices and storage nodes uses an independent data plane; the control plane handles provisioning, identity and configuration. A control-plane outage affects new provisioning and boots but does not interrupt established I/O.

The data plane is encrypted by default: a single NVMe/TCP port `4421` uses TLS 1.3, shared by the firmware and kernel segments. Authentication uses DH-HMAC-CHAP, and the TLS pre-shared key is derived from the authentication key. A missing key or an unready port refuses the boot instead of falling back to plaintext.

## Relationship to Kubernetes

Kubernetes schedules application workloads and assumes that nodes already exist and systems are already installed. Kurrent delivers the system that carries applications; it addresses the layer beneath Kubernetes.

The two operate at different layers and can be combined: Kurrent converts bare metal into replaceable system carriers, and Kubernetes or another orchestrator runs inside those systems. Kurrent does not provide post-boot application orchestration and does not require replacing an existing upper-layer platform.

## Three Roles

| Role | Responsibility | Main components |
| --- | --- | --- |
| Control plane | Identity, ledger, binding, storage scheduling, boot variables, audit | Control-plane service, nginx, dnsmasq, WebUI, CLI |
| Storage node | Hosts master images and system disks, exports block devices | Agent, nvmet-host, host kernel nvmet |
| Device | Stateless compute; becomes a Worker's booting node after network boot | Device firmware (iPXE), operating system inside the system disk |

```text
                Management plane 443 (the only external entry)
  browser / CLI ───────────────► control-plane server
                                   │  dnsmasq: DHCP / TFTP
                                   │  control plane: identity / ledger / scheduling
                                   ▼
  bare-metal device ── PXE / HTTPS boot ──► boot variables (disk NQN, host NQN, credentials)
      │
      └──── NVMe/TCP 4421 (TLS 1.3) ────► storage node (Agent + nvmet-host)
```

## Three Entities

| Entity | Identifier | Authority |
| --- | --- | --- |
| Device | MAC | Device ledger; the binding is authoritative on the device side |
| Worker | `worker_id` (hostname) | Control-plane ledger; represents compute identity |
| System disk | NQN | Disk file on a storage node; ownership recorded by the control plane |

The device lifecycle is `pooled → bound → revoked`; a Worker's `readiness` is derived from "has a bound device" and "has a system disk". One Worker may carry multiple system disks, and the default disk determines the boot entry after the menu timeout.

## Delivery Roles

The control plane owns identity, ledgers and connection information; the storage node clones the system disk from a master and exports the block device; the device obtains the boot script over DHCP/TFTP, completes the identity exchange over HTTPS, and boots the system disk of its Worker from the network. Migrating to another machine only requires rebinding the device to the same Worker; the system disk and identity remain unchanged.

Deployment follows a single standard procedure: the control plane and storage nodes may be separate or co-located, and deployment leads into the delivery workflow. Master image customization is one-time preparation and is not repeated for every deployment.

## Capabilities

- **Zero-touch enrollment** — Within the registration window, a device with a valid public key registers automatically; once the window closes, no new device is admitted.
- **Device identity** — The private key is generated in NVRAM; challenge-response signatures; key rotation supported.
- **Binding and rebinding** — One-to-one between device and Worker; rebinding is atomic; unbinding returns the device to the pool.
- **System disks** — Master cloning, 4K-native GPT validation, placement policy, default disk and multiple systems per Worker.
- **Data plane** — NVMe/TCP + TLS 1.3 on a single port `4421`; no plaintext fallback.
- **Credential lifecycle** — DHHC-1 issued at disk creation; rotation, revocation, push reconciliation and restart recovery.
- **Declarative configuration** — One YAML each for the control plane and the node; full validation before writing, rollback on failure.
- **Management entry points** — WebUI, CLI and REST API; certificate, token and session authentication.
- **Identity and audit** — Three CAs, deny-by-default principal classes, per-serial revocation, request-level audit and alarms.
- **Firmware** — A separate repository maintains iPXE patches and build artifacts; device firmware provides boot-layer capabilities.

## Current Boundaries

Kurrent currently focuses on the minimal closed loop of the control plane and data plane. It does not assume that every operational scenario is already covered.

- **Verified distributions**: Debian 12/13, Ubuntu 24.04/26.04, Fedora 44, AlmaLinux 10.2 and openSUSE Leap 16.0 have completed whole-machine cold boot; the verification set does not limit which distributions can boot.
- **Deployment prerequisites**: a dedicated PXE network and storage-host prerequisites (kernel NVMe/TCP TLS support, `tlshd >= 1.5.0`, a reflink-capable filesystem).
- **Not yet provided**: post-boot application orchestration, control-plane high availability, IPv6, ARM, data disks, live migration; Windows and ESXi are on the roadmap.
- **State storage**: the control plane is a single node with plain-file ledgers; there is no database or multi-node consistency mechanism.
- **Boot trust boundary**: the first DHCP/TFTP hop has no cryptographic protection; deployments must treat the PXE network as an independent trust boundary. Mainline firmware is unsigned.
- **Firmware status**: device firmware is built from a separate repository; mainline has no operational Secure Boot path, and iPXE-side NBFT generation is still pending.

## Related Pages

- Objects and binding: [Devices, Workers and System Disks](../concepts/entities.md)
- Boot and storage: [Boot Chain](../concepts/boot-chain.md), [Storage and Data Plane](../concepts/storage.md)
- Deployment and delivery: [Deployment](./deployment.md), [First Delivery](./usage.md)
- Security boundary: [Security Model](../architecture/security.md)
- Firmware: [Firmware Overview](../firmware/index.md)
