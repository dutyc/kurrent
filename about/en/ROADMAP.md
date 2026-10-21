# Roadmap

## Direction 1: NVMe-oF Protocol Stack and Authentication Framework

*Make NVMe-oF the primary data-plane path, implement inter-component authentication, and improve distribution support.*

- NVMe-oF becomes the primary boot path, with iSCSI narrowed to a fallback channel; NBFT handover and TLS encryption become the mainline
- Implement authentication and transport encryption across the entire chain among devices, the control plane, and storage nodes, with key lifecycle management
- Whole-machine network boot over NVMe-oF for mainstream distributions: Linux family, Windows, ESXi

## Direction 2: IPv6 and High-Performance Database

*Support IPv6 dual-stack across the boot chain, and upgrade state storage to a high-performance database.*

- Full IPv6 dual-stack support across the boot chain (including NVMe-oF over IPv6), with smooth migration for existing environments
- Unify ledger and audit streams into a database, starting with zero-ops on a single node and evolving toward high availability

## Direction 3: High Availability and Storage Management

*Make the control plane and boot services redundant, and make NVMe backend storage poolable, migratable, and self-healing.*

- Multi-node redundancy for the control plane and boot services, with automatic failover on boot failure
- Storage node redundancy and failover
- NVMe backend: disk lifecycle, storage pools, configuration consistency, and self-healing

## Direction 4: ARM Architecture Support

*Give ARM devices the same stateless boot and protocol stack capabilities as x86.*

- Adapt the ARM64 network boot chain and mainstream distributions
- Align ARM nodes' capabilities with the existing protocol stack (NVMe-oF / IPv6 / authentication)