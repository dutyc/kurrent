# Project Overview

Kurrent is a stateless delivery system for bare-metal compute: identity, system and data do not belong to the physical machine; they are granted by the control plane and the storage side at boot. The machine therefore becomes a replaceable location, and the system becomes the object that is delivered and scheduled.

> Identity lives in the device ledger, the system disk on storage, the declaration in configuration files, and credentials are issued by the control plane; the node keeps only a temporary connection for one boot.

## The problem it solves

Cloud-native practice lets applications be rebuilt, moved and upgraded, but the physical nodes underneath are still maintained the traditional way — installing systems, configuring networks, reinstalling and replacing hardware all require manual work or a separate infrastructure stack.

Kurrent addresses that layer: **make the machine that carries a system replaceable as well**. It does not replace application orchestration, and it does not hide state behind another storage abstraction; it changes the origin of identity and of the system at boot, so replacing the machine no longer requires reinstalling the system.

Kubernetes schedules application workloads and assumes that nodes already exist and systems are already installed; Kurrent delivers the system that carries applications, one layer below. The two compose, and Kurrent does not provide post-boot application orchestration.

## Core model

**A system consists of a compute identity, a system disk and boot credentials, and belongs to the identity rather than the device.**

| Object | Identifier | Meaning | Authority |
| --- | --- | --- | --- |
| Device | MAC | A replaceable physical location | Device ledger |
| Worker | `worker_id` | The compute identity | Control-plane ledger |
| System disk | NQN | The bootable system itself | Disk file on a storage node; ownership recorded by the control plane |

Four invariants:

- Device and Worker are one-to-one; the binding can change, and rebinding is one cold migration.
- The Worker owns its system disks; disks do not move with devices.
- The disk stores no fixed machine identity; each boot projects connection information and credentials from the current binding.
- Device identity lives inside the TPM and does not migrate with the disk; replacing the mainboard or clearing the TPM is equivalent to replacing the device.

The device lifecycle is `pooled → bound → revoked`. A Worker's `readiness` is derived from "has a bound device" and "has a system disk"; one Worker may carry multiple system disks, and the default disk determines the boot entry after the menu timeout.

## What the system is made of

| Role | Responsibility | Main components |
| --- | --- | --- |
| Control plane | Identity, ledger, binding, storage scheduling, boot variables, audit | Control-plane service, nginx, dnsmasq, WebUI, CLI |
| Storage node | Hosts master images and system disks, exports block devices | Agent, nvmet-host, host kernel nvmet |
| Device | Stateless compute; becomes a Worker's booting node after network boot | Device firmware (iPXE), operating system inside the system disk |

![Deployment topology: the management and data planes between control plane, storage nodes and devices](/diagrams/en/topology.svg)

The control plane handles provisioning, identity and configuration and is not on the data path between devices and storage nodes; the control plane and storage nodes may be separate or co-located, but the roles stay separate. Components, ports and failure domains are covered in the [Architecture](../concepts/architecture/index.md).

## One run: from power-on to login

1. **Network boot** — the device obtains an address and next hop over DHCP and fetches the signed first-hop iPXE from the control plane over TFTP. The control plane is the only DHCP and TFTP provider on that segment.
2. **Identity and attestation** — the firmware creates or reuses an AK inside the TPM, reports the device fingerprint and AK public key; the control plane issues a one-shot nonce, and the device quotes PCR 0/2/4/7.
3. **Admission and projection** — the control plane checks the binding, the quote signature, the nonce and the platform baseline; only then does it return the root-path, NQN, storage address and credentials. Requests that do not pass receive menu variables only.
4. **Data-plane connection** — the firmware authenticates with DH-HMAC-CHAP and connects to port `4421` on the storage node over TLS 1.3.
5. **System boot** — iPXE `sanboot` loads GRUB and the kernel from the disk.
6. **initramfs handover** — the kernel phase authenticates again and establishes TLS; NBFT and NBCT carry the connection information, and the initramfs reconnects and mounts the root filesystem.
7. **Login** — the target system comes up. The control plane is not on the I/O path: its failure affects new provisioning and boots but does not interrupt established connections.

Any credential step that fails refuses the boot instead of falling back to plaintext. Step-by-step mechanics and failure paths are in the [Boot Chain](../concepts/boot-chain.md).

![Key steps of one boot: DHCP, attestation, variable projection and the NVMe/TCP connection](/diagrams/en/boot-chain.svg)

## Capability map

- **Enrollment (device)** — within the registration window, a device with a valid TPM AK and Secure Boot enabled registers automatically; a missing TPM, disabled Secure Boot or a closed window refuses enrollment.
- **Binding (device, Worker)** — one-to-one between device and Worker; unbinding returns the device to the pool, and a bound device is not admitted twice.
- **System disks (Worker, system disk)** — master cloning, placement policy, default disk and multiple systems; no default disk falls back to `reboot`, and a non-4K GPT disk fails with `0x7f22208e`.
- **Boot (device, system disk)** — variable projection, menu and NBFT/NBCT handover; failed attestation releases no credentials.
- **Credentials (system disk)** — DHHC-1 issued at disk creation, with rotation, revocation and push reconciliation; revocation refuses the boot and does not fall back to plaintext.
- **Operations (global)** — declaration drift, audit, alarms and node draining; a control-plane outage refuses new provisioning and boots.

Entry points are the WebUI, the CLI and the REST API; commands and endpoints are in [Deployment](../setup/deployment.md), [Operations](../tasks/operations.md) and the [API Reference](../reference/api/).

## Current boundaries

- Boot architecture is x86_64 UEFI; the network is IPv4. ARM and IPv6 are not complete.
- The control plane is single-node with file state; high availability is not provided, and its failure affects new provisioning and boots.
- Post-boot application orchestration is out of scope and can be provided by an upper layer.
- Cold migration requires a manual fence; live migration is not supported.
- Platform keys for the Secure Boot first hop (PK / KEK / db) are written on the first network boot through the platform enrollment window; machines locked to vendor keys need the vendor flow; per-model validation on real hardware is not complete.
- The PXE segment must be an independent trust boundary, with the control plane as its only DHCP, TFTP and boot HTTPS provider.

See the [Roadmap](../about/roadmap.md) for complete boundaries and the [Security Model](../concepts/architecture/security.md) for trust assumptions.

## What's next

- Understand objects and binding: [Devices, Workers and System Disks](../concepts/entities.md)
- Understand boot and identity: [Boot Chain](../concepts/boot-chain.md), [Identity and Access](../concepts/identity.md)
- Understand storage and the data plane: [Storage and Data Plane](../concepts/storage.md)
- Start deploying: [Deployment](../setup/deployment.md), [System Disk Customization](../tasks/system-disk.md)
- Operate and troubleshoot: [Operations](../tasks/operations.md), [Troubleshooting](../tasks/troubleshooting.md)
- Look up parameters and interfaces: [Configuration](../reference/configuration.md), [CLI](../reference/cli.md), [API](../reference/api/)
- Firmware and branches: [Firmware Overview](../firmware/index.md), [Repositories and Branches](../reference/branches.md)
