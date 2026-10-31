# Usage Guide

> Prerequisites: the [Deployment Guide](./deployment) is complete, the controller and storage nodes report `health=ok`, and the bare-metal machines are connected to the PXE network.

## 1. First Boot and Enrollment

On network boot the machine runs: DHCP → TFTP pulls iPXE and the `boot.ipxe` pointer → iPXE fetches `boot.ipxe.cfg` over HTTPS → `keygen` creates an ECDSA P-256 private key on first use (kept in EFI NVRAM, never leaves the machine) → `/devices/report` sends the fingerprint and public key.

A device **enters the pool only while the registration window is open and the report carries a valid public key**:

1. WebUI → **Devices** → open the registration window (TTL is bounded by the declared `spec.registration.windowMaxTtlMinutes`, default 60 minutes; optional admission quota `max_devices`).
2. The device appears as `pooled`; an already-pooled device reporting with its public key during the window completes key claim.
3. Once every device has claimed its key, enable **signature enforcement**: `/boot-vars` then serves only bound devices that pass the challenge-response verification.

> After the window closes, reports only refresh the fingerprint; there is no enrollment channel outside the window.

## 2. Bind a Worker

Devices and Workers are strictly **one-to-one**:

- **Devices page**: select a device → bind it to an existing Worker. An already-bound device is rejected by default; `force` performs an atomic rebind (new binding written before the old one is cleared).
- **Workers page**: create a Worker with a MAC to bind in one step; leave the MAC empty to get an idle Worker and bind a device later.

The binding authority is the device ledger's `bound_worker_id`. The dnsmasq hostname is only a derived projection of the binding operation; it may lag and **does not participate in boot identity resolution**.

Unbinding returns the device to the pool; the Worker's system disks are kept. See [Devices, Workers and System Disks](../concepts/entities.md) for the full entity semantics and rebinding rules.

## 3. Create a System Disk

First make sure the storage node holds a usable **master image**: put the disk file under the node's `diskDir` with `_tpl_` in the filename (for example `_tpl_ubuntu2604.img`); it shows up in the master list after the background scan. Master images must use 4K-native GPT.

On the **Workers** page, select a Worker → create a system disk:

- Pick the master image and `os` / `os_version` (display and grouping metadata for the disk). A Worker can hold at most one disk per `(os, os_version)` pair.
- Placement is decided by the control plane: candidates must hold that master image locally, be healthy and not drained; the node with the fewest existing system disks wins; the preview allows pinning rows by hand.
- Batch mode: select several Workers → batch-create system disks → review the plan (`dry_run`, same logic as execution) → confirm.

The control plane generates a 12-hex `os_tag` and the full disk NQN for each disk. On nvmet backends a **DHHC-1 authentication key is issued automatically at disk creation** and pushed to the Agent holding the disk.

## 4. Set the Default Boot Disk

Worker detail → system disks → **Set as default**. The default disk determines:

- which menu entry the iPXE menu selects on timeout;
- which disk's connection data `/boot-vars` projects (the full `disk-nqn`, `storager-ip`, TLS PSK and port).

Without a default disk, the control plane returns `menu-default=reboot` (a 1 ms reboot loop) so the machine never silently boots the wrong system. A Worker can carry multiple system disks; to switch, set another disk as default in the Worker detail page, effective on the next boot.

When a Worker has only one system disk, it is set as the default automatically after creation. If the default disk is deleted, a remaining disk is promoted automatically.

See [Storage and Data Plane](../concepts/storage.md) for master images, system disks and data-plane semantics.

## 5. Boot Chain

```text
DHCP → TFTP (snponly.efi + boot.ipxe pointer) → iPXE
  → HTTPS boot.ipxe.cfg        variables and device identity chain entry
  → /devices/report            device info and public key
  → /devices/challenge         one-time nonce
  → sign(nonce‖mac)            signed with the device key
  → /boot-vars                 after verification: disk-nqn / storager-ip / hostnqn / TLS PSK and port
  → menu.ipxe                  default disk selected on timeout
  → sanboot nvme://<storager>:4421/<disk-nqn>   TLS 1.3 + DH-HMAC-CHAP
  → GRUB → operating system
```

TFTP carries only the first-stage firmware `snponly.efi` and the `boot.ipxe` pointer; everything from `boot.ipxe.cfg` onward uses HTTPS + TOFU.

The data plane has a single port, `4421`, shared by the firmware and kernel stages. A missing credential, missing PSK or unready port **refuses the boot** — there is no plaintext fallback.

## 6. Credentials and Encryption Posture

Credentials follow the Worker and take effect immediately on rotation or revocation:

```bash
kurrent workers credential show worker-01             # key hash prefix and per-Agent push status
kurrent workers credential set worker-01 --generate   # rotate in place (never prints plaintext)
kurrent workers credential revoke worker-01           # revoke: the next boot is refused
```

The TLS PSK is derived from the authentication key, so rotating or revoking the key rotates or revokes the PSK atomically. Failed pushes are retried by the control plane's periodic reconciliation, and the status is visible on the Worker detail page under "NVMe-oF credentials".

See [Storage and Data Plane](../concepts/storage.md) and [Identity and Access](../concepts/identity.md) for credential issuance, rotation and revocation semantics.

Plaintext keys exist only where the chain requires them (the 0600 state files on the control plane and Agent, the storage node's configfs and the kernel `.nvme` keyring); no application-layer encryption is added — disk and host access boundaries are the deployment's responsibility.

## 7. Day-to-Day Operations

| Operation | Location | Semantics |
| --- | --- | --- |
| Delete a system disk | Worker detail → system disks | Keep or delete the backing file; if it was the default disk, a remaining disk is promoted, otherwise `default_disk` is cleared |
| Delete a Worker | Workers page | Unbinds devices, removes the Worker's credential entries and push ledger; disk files are handled per option |
| Drain a node | Agents page → disable | `enabled=false`: no new disks, existing disks keep serving |
| Audit | `kurrent ops list` | Device registration/claim, binding, disk creation, credential issue/push/revoke, and more |
