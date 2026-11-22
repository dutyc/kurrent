# Kurrent

![Cloud Native](https://img.shields.io/badge/Cloud%20Native-True%20Cloud%20Native-18181b) [![NVMe-oF](https://img.shields.io/badge/Data%20Plane-NVMe--oF%20First-00D4FF)](https://dutyc.github.io/kurrent/setup/deployment.html) [![Stars](https://img.shields.io/github/stars/dutyc/kurrent)](https://github.com/dutyc/kurrent/stargazers) [![Release](https://img.shields.io/github/v/release/dutyc/kurrent)](https://github.com/dutyc/kurrent/releases) [![License](https://img.shields.io/github/license/dutyc/kurrent)](LICENSE) [![Docs](https://img.shields.io/badge/Docs-GitHub%20Pages-2563eb)](https://dutyc.github.io/kurrent/)

[中文版](./README.zh-CN.md) | [English](./README.md)

**Make bare metal flow.**

*The six lines flow, above and below without constancy.* — the I Ching

K8s made applications cloud-native; Kurrent makes compute cloud-native.

Kurrent is a cloud-native, stateless bare-metal delivery paradigm. It carries "stateless" down to the physical compute layer: a compute node (Device) holds no persistent state, and identity (Worker), system and data are granted by the network and the control plane. Its unit of scheduling is not the application but the system itself — a system belongs to an identity, not to a machine. A device starts as soon as it is connected to the network, and compute flows between bare-metal nodes like current. See the [manifesto](./about/en/Manifesto.md).

## NVMe-oF: The Primary Data Plane

Storage is NVMe-oF-first: an in-kernel NVMe/TCP target exports system disks, with TLS 1.3 on a single port `4421` shared by the firmware and kernel boot stages. DHHC-1 credentials follow the Worker and are issued at disk creation on nvmet backends; the root path uses the full NQN from the disk record. iSCSI (stgt/lio) remains as a compatibility backend.

## Security

Device identity is an Attestation Key inside the TPM: the device reports its public key during a registration window, and on every boot answers a single-use nonce with a PCR 0/2/4/7 quote before the control plane releases credentials. Secure Boot verifies the signed first hop against the UEFI database, and boot HTTPS trusts only the boot CA embedded at build time. The management plane uses mutual TLS; the NVMe/TCP data plane uses TLS 1.3 with per-Worker DHHC-1 credentials. See the [security model](https://dutyc.github.io/kurrent/en/concepts/architecture/security.html).

## Getting Started

Deploy a control plane and join storage nodes in minutes — see [Deployment](https://dutyc.github.io/kurrent/en/setup/deployment.html) on the documentation site. The `kurrent` CLI ships prebuilt binaries on [Releases](https://github.com/dutyc/kurrent/releases) for Linux amd64/arm64 and Windows amd64.

## Documentation

The [documentation site](https://dutyc.github.io/kurrent/en/) covers deployment, system disk customization, operations, troubleshooting, and reference material: [CLI reference](https://dutyc.github.io/kurrent/en/reference/cli.html), [API reference](https://dutyc.github.io/kurrent/en/reference/api/), [architecture overview](https://dutyc.github.io/kurrent/en/concepts/architecture/).

## Delivery Flow

A device joins the pool on first boot; after binding a Worker, creating a system disk and setting the default disk in the Web UI, it boots. One Worker can carry multiple system disks and switch between them in the menu. [System Disk Customization](https://dutyc.github.io/kurrent/en/tasks/system-disk.html) and [Deployment](https://dutyc.github.io/kurrent/en/setup/deployment.html) cover image preparation and the first boot. Cold boot is verified on Debian 12/13, Ubuntu 24.04/26.04, Fedora 44, AlmaLinux 10.2, openSUSE Leap 16.0; see the [distribution matrix](https://dutyc.github.io/kurrent/en/reference/distributions.html) for details, and the verification set does not limit which distributions can boot.

## Firmware Repo

The boot-chain firmware is built by the companion repository **[Kurrent Firmware](https://github.com/dutyc/kurrent-firmware)**: Kurrent makes compute stateless, the firmware repository makes boot firmware stateless. Build and deployment steps are documented in [Firmware Build and Deployment](https://dutyc.github.io/kurrent/en/firmware/index.html).

## Roadmap

The direction is a cross-platform, cross-architecture cloud-native meta-protocol: one stateless semantics across bare metal and hypervisors. The vision lives in the **[Manifesto](./about/en/Manifesto.md)**; concrete directions are in the **[ROADMAP](./about/en/ROADMAP.md)** (中文: [about/zh/ROADMAP.md](./about/zh/ROADMAP.md)).

## Community & Contributing

Issues, Discussions and pull requests are welcome. AI may write the syntax, but architecture must be understood by a human; read [AI_POLICY.md](./about/en/AI_POLICY.md) before submitting.

## License

[Apache License 2.0](./LICENSE)

## Star History

<a href="https://www.star-history.com/?repos=dutyc%2Fkurrent&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=dutyc/kurrent&type=date&theme=dark&legend=top-left&sealed_token=zkQGmbPm0yH3EjnLTKc1DTe9hOaLnAeUdZlSlo92lycj2vyTy9VkyIW-uvH3P09ByCS5CiBI8QnhVbVNFyM211tGwSJ1yp7qE6ZsukdPCxJWkopIpVQepMXjrDwOAVENpL87Tr8qmmIYxQy6DawB8PaqrlfuVmGZPdnh9fPfJ8GtvnCIwkENEeVPSVp7" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=dutyc/kurrent&type=date&legend=top-left&sealed_token=zkQGmbPm0yH3EjnLTKc1DTe9hOaLnAeUdZlSlo92lycj2vyTy9VkyIW-uvH3P09ByCS5CiBI8QnhVbVNFyM211tGwSJ1yp7qE6ZsukdPCxJWkopIpVQepMXjrDwOAVENpL87Tr8qmmIYxQy6DawB8PaqrlfuVmGZPdnh9fPfJ8GtvnCIwkENEeVPSVp7" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=dutyc/kurrent&type=date&legend=top-left&sealed_token=zkQGmbPm0yH3EjnLTKc1DTe9hOaLnAeUdZlSlo92lycj2vyTy9VkyIW-uvH3P09ByCS5CiBI8QnhVbVNFyM211tGwSJ1yp7qE6ZsukdPCxJWkopIpVQepMXjrDwOAVENpL87Tr8qmmIYxQy6DawB8PaqrlfuVmGZPdnh9fPfJ8GtvnCIwkENEeVPSVp7" />
 </picture>
</a>
