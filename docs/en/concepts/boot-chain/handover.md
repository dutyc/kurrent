# Hand-over to the Kernel

The data-plane connection firmware builds only serves the boot phase; the root filesystem has to be mounted by the operating system itself. The hand-over leaves this session's connection facts to the kernel: a credentials table, discovery information, and a reconnect on the same port.

## The NBCT Credentials Table

At `sanboot`, firmware installs an **NBCT** credentials table (an ACPI table) recording for this session:

- target address and port;
- subsystem NQN and host NQN;
- DH-HMAC-CHAP key.

The TLS PSK and data-plane port travel with the `sanboot` URI; NBFT carries network and subsystem discovery.

## initramfs Reconnect

The initramfs `nbft-connect` consumes **NBFT** (network and subsystem discovery) and **NBCT** (credentials and host NQN relay), reconnects on the same port with `--tls`, and mounts the root filesystem.

No machine identity is written into the disk: identity, credentials and connection data all come from this boot's projection.

## Current Status

> NBCT is produced by firmware; iPXE-side NBFT generation is still pending. The verified whole-machine chain injects an NBFT table to emulate the "firmware pre-generated" stage.

## What's next

- Back to the [Boot Chain](../boot-chain.md)
- How the connection is established: [Data Plane](./data-plane.md)
- Disk and credential ownership: [Storage and Data Plane](../storage.md)
- NBCT and NBFT configuration: [Firmware Platform Reference](../../reference/firmware-platform.md)
