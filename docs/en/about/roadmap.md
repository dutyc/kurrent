# Roadmap

The roadmap is organized by technical direction, not by version or date commitments. Each direction lists what exists and what remains; completion is judged by end-to-end verification, not by whether code has merged.

## Direction 1: NVMe-oF protocol stack and authentication

**Goal**: NVMe/TCP as the only primary data plane, with authentication, encryption, and credential lifecycle covering both the firmware and kernel segments.

Available today:

- Firmware-side `nvmetcp` driver, DH-HMAC-CHAP authentication, and NBCT credential relay;
- A single data-plane port 4421 with NVMe/TCP TLS 1.3 and PSK derived from the authentication key;
- Control-plane issuance, push, reconciliation, rotation, and revocation;
- Cold-boot verification from power-on to a login prompt.

Remaining:

- iPXE-side NBFT generation, replacing the injected simulation used in current verification chains;
- A broader distribution matrix, including Windows and ESXi NVMe-oF boot;
- More NICs with native drivers and an expanded firmware support matrix.

## Direction 2: IPv6 and state storage

**Goal**: IPv6 dual-stack across the boot chain, with ledgers and audit evolving smoothly from single-host files.

Available today:

- IPv4 PXE and NVMe/TCP data plane;
- Single-host YAML/JSONL state with no external deployment dependency.

Remaining:

- IPv6 dual-stack for DHCP, TFTP, HTTPS, and NVMe-oF;
- A migration path for existing environments;
- State and audit ingestion into a database while preserving the zero-ops single-host starting point.

## Direction 3: High availability and storage management

**Goal**: Redundant control plane and boot services; storage nodes that can be pooled, migrated, and healed.

Available today:

- A single control plane with file state;
- Storage-node health probing, drain, and credential reconciliation;
- Device-to-Worker rebinding (cold migration, fenced manually);

Remaining:

- Multi-node redundancy and failover for the control plane and boot services;
- Storage-node redundancy and failover;
- Further convergence of disk lifecycle, storage pools, and configuration consistency.

## Direction 4: ARM

**Goal**: ARM64 devices get stateless boot and protocol capabilities on par with x86.

Available today:

- x86_64 UEFI boot chain and firmware artifacts.

Remaining:

- ARM64 network boot chain and distribution adaptation;
- ARM nodes aligned with NVMe-oF, IPv6, and authentication capabilities.

## Current boundaries

| Item | Status |
| --- | --- |
| Boot architecture | x86_64 UEFI |
| Network | IPv4 |
| Data plane | NVMe/TCP primary; the iSCSI compatibility backend is outside the main boot path |
| Control plane | Single host, file state |
| High availability | Not provided |
| Post-boot orchestration | Out of scope; can be provided by a layer above |
| Firmware signing | Mainline unsigned; Secure Boot artifacts not in mainline |
| Verified distributions | Debian 12/13, Ubuntu 24.04/26.04, Fedora 44 |

## How progress is judged

A direction is complete only when:

- a real machine completes a cold boot to a usable system;
- failure paths have explicit refusal semantics and do not rely on manual intervention;
- configuration, credentials, and state do not acquire a second source of truth;
- regression tests cover the new boundary and fail under mutation.

Progress is not measured by commit count, lines of code, or checked roadmap boxes.

## Related pages

- [Project Overview](../guide/overview.md)
- [Architecture Overview](../architecture/index.md)
- [Security Model](../architecture/security.md)
- [Contributing](./contributing.md)
