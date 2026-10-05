# Kurrent

![Cloud Native](https://img.shields.io/badge/Cloud%20Native-True%20Cloud%20Native-18181b) [![NVMe-oF](https://img.shields.io/badge/Data%20Plane-NVMe--oF%20First-00D4FF)](https://dutyc.github.io/kurrent/guide/deployment.html) [![Stars](https://img.shields.io/github/stars/dutyc/kurrent)](https://github.com/dutyc/kurrent/stargazers) [![Release](https://img.shields.io/github/v/release/dutyc/kurrent)](https://github.com/dutyc/kurrent/releases) [![License](https://img.shields.io/github/license/dutyc/kurrent)](LICENSE) [![Docs](https://img.shields.io/badge/Docs-GitHub%20Pages-2563eb)](https://dutyc.github.io/kurrent/)

[中文版](./README.zh-CN.md) | [English](./README.md)

**Make bare metal flow.**

*周流六虚，上下无常。* — from the *I Ching*: currents flow through the six voids, above and below, without constancy.

K8s made applications cloud-native. Kurrent makes compute cloud-native — *K8s orchestrates containers; Kurrent orchestrates compute.*

**Kurrent** is a cloud-native stateless bare-metal delivery platform: compute nodes hold no persistent state — identity (Worker), OS and data are granted by the network and the control plane. Plug in a cable and a node comes alive; compute flows across bare metal like current itself.

Read our manifesto: **[about/en/Manifesto.md](./about/en/Manifesto.md)** — *Our Definition of Cloud Native* (中文: [about/zh/Manifesto.md](./about/zh/Manifesto.md)).


## NVMe-oF: The Primary Data Plane

Storage is NVMe-oF-first: nodes join with `backend=nvmet` (default) and serve disks through the in-kernel NVMe-oF target (NVMe over TCP, port 4420). One NQN namespace (`nqn.2026-07.com.kurrent`), DHHC-1 authentication per worker — the key is issued automatically when a system disk is created, and the storage node's host-service credential is derived at enroll — and a boot chain wired for `nvme://` root paths; iSCSI (`stgt`/`lio`) stays as the compatibility backend.

## Getting Started

Deploy a control plane and join storage nodes in minutes — [deployment guide (zh)](https://dutyc.github.io/kurrent/guide/deployment.html) / [en](https://dutyc.github.io/kurrent/en/guide/deployment.html). The `kurrent` CLI ships as prebuilt binaries on the [Releases](https://github.com/dutyc/kurrent/releases) page (Linux amd64/arm64, Windows amd64) — no local build needed.

- **Docs site (bilingual)** — https://dutyc.github.io/kurrent/
- **CLI reference** — [cli/README.md](./cli/README.md)
- **Control-plane API reference** — [api/control-plane-api.en.md](./api/control-plane-api.en.md) / [api/control-plane-api.zh-CN.md](./api/control-plane-api.zh-CN.md)
- **Architecture** — [about/en/ARCHITECTURE.md](./about/en/ARCHITECTURE.md) (中文: [about/zh/ARCHITECTURE.md](./about/zh/ARCHITECTURE.md))

## Key Features: Compute That Flows, Plug-and-Play

A stateless bare-metal delivery flow: machines join the device pool on first boot, then a few clicks in the Web UI bind them to a Worker with a system disk and a default OS. One Worker can carry multiple system disks and switch between them online. Diskless cold boot over NVMe-oF is verified end to end on **Debian 12/13, Ubuntu 24.04/26.04 and Fedora 44**.

## Firmware Repo

*The firmware engine for Kurrent. Make bare metal flow at the boot layer.*

The firmware at the bottom of the boot chain is built by our companion repo **[Kurrent Firmware](https://github.com/dutyc/kurrent-firmware)** — two sides of the same philosophy: this repo makes compute stateless, the firmware repo makes boot firmware stateless.

## Roadmap

A cross-platform, cross-architecture cloud-native meta-protocol: one stateless semantics, self-similarly nested across bare metal and hypervisors — that vision lives in the **[Manifesto](./about/en/Manifesto.md)**. The concrete directions we are working on: **[about/en/ROADMAP.md](./about/en/ROADMAP.md)** (中文: [about/zh/ROADMAP.md](./about/zh/ROADMAP.md)).

## Community & Contributing

Star / Watch / Discussions / PRs are all welcome. This project embraces AI-assisted development, with one hard requirement: **the AI can write the syntax, but architecture understanding must come from a human brain.** Please read [AI_POLICY.md](./about/en/AI_POLICY.md) before submitting a PR.

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
