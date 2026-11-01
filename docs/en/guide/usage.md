# First Delivery

> This page turns a device already attached to the PXE network into a network-bootable system: verify the master image → admit the device → bind a Worker → create a system disk → set the default disk → cold-boot acceptance → manage credentials. It assumes [Deployment](./deployment.md) is complete and the controller and storage nodes report `health=ok`.

## Delivery Path

Complete the seven steps in order; their completion signals are:

1. Verify the master image: a 4K-native GPT master exists under `diskDir` and appears in `GET /masters`.
2. Admit the device: Devices shows `pooled` with the key claimed.
3. Bind a Worker: the device and Worker are bound one-to-one.
4. Create a system disk: the master clone is complete and `os_tag` exists; nvmet also issues the credential.
5. Set the default disk: the Worker's default boot disk points at the target system.
6. Cold-boot acceptance: GRUB, kernel and initramfs reach rootfs and a login prompt.
7. Manage credentials: rotation/revocation works and push status is healthy.

## WebUI and API

Every step works through the WebUI or the control-plane REST API. **The CLI currently exposes only `workers list/get` and credential operations**; worker creation, device binding, disk creation and default-disk selection use the API or WebUI.

On the controller, in the repository root, prepare the API helper (the admin certificate is the management credential bootstrapped by `kurrent init`):

```bash
BASE_URL=https://192.168.1.10/api/cp
CERT=control_plane/state/pki/users/admin/client.crt
KEY=control_plane/state/pki/users/admin/client.key
CA=control_plane/state/certs/server.crt
api() {
  curl -sS --cert "$CERT" --key "$KEY" --cacert "$CA" \
    "$BASE_URL$1" "${@:2}"
}
```

> This assumes `spec.serverCert.san` contains the controller management address; otherwise add it as described in [Deployment](./deployment.md) and re-run `kurrent init`. For temporary troubleshooting you can drop `--cacert` and add `-k`; do not make that a permanent configuration.

## 1. Verify the Master Image

Master images are not uploaded through the API: place the disk file under the storage node's `diskDir` with `_tpl_` in the filename (for example `_tpl_ubuntu2404.img`); it appears in the master list after the background scan. It must use a 4K-native GPT. See [System Disk Customization](./system-disk.md) for construction.

```bash
api /masters | jq \
  '.agents[] | {agent, storager_ip, masters: [.masters[] | {name, gpt}]}'
```

Confirm each item:

- `gpt` is `4k`. A 512-byte-sector GPT exported by nvmet as 4096 B looks partitionless to the initiator and fails with `0x7f22208e`.
- The storage node is `enabled=true` with a ready data plane; placement only considers nodes that hold the master.
- The master must not contain machine identity, target/NQN, authentication keys or control-plane addresses.

`os` / `os_version` are display and grouping metadata written at disk creation. A Worker can hold at most one disk per `(os, os_version)` pair.

## 2. Admit the Device

If the device was admitted during deployment acceptance, skip to the next step. For additional devices, open the registration window before the device network-boots. The window must be open before `/devices/report`.

```bash
# Open the window: 30 minutes, at most 20 new devices
api /settings/registration-window -X POST \
  -H 'Content-Type: application/json' \
  -d '{"ttl_minutes": 30, "max_devices": 20}'

# Inspect the window and admission count
api /settings/registration-window | jq
```

If a window is already open, `POST` returns 409; inspect the current state, and close it before reopening if needed.

On network boot the device runs: DHCP → TFTP pulls iPXE and the `boot.ipxe` pointer → iPXE fetches `boot.ipxe.cfg` over HTTPS → `keygen` creates an ECDSA P-256 private key on first use (kept in EFI NVRAM) → `/devices/report` sends the fingerprint and public key.

```bash
# Confirm the device is pooled and has claimed its key
api /devices | jq
```

After every device has claimed its key, close the window and enable signature enforcement:

```bash
api /settings/registration-window -X DELETE

# Enable only after every device has claimed its key
api /settings/enforcement -X PUT \
  -H 'Content-Type: application/json' -d '{"enabled": true}'
```

> After the window closes, reports only refresh the fingerprint and never admit the device; there is no enrollment channel outside the window. With enforcement on, bound devices without a key, without a signature, or with an invalid signature are also refused. A revoked device does not recover merely by reporting again.

## 3. Bind a Worker

Devices and Workers are strictly **one-to-one**. The WebUI offers two paths:

- **Devices page**: select a device → bind it to an existing Worker. An already-bound device is rejected by default; `force` performs an atomic rebind (new binding written before the old one is cleared).
- **Workers page**: create a Worker with a MAC to bind in one step; leave the MAC empty to get an idle Worker and bind a device later.

API equivalents:

```bash
# Create a Worker and bind the device in one call (the device must already be pooled)
api /workers -X POST \
  -H 'Content-Type: application/json' \
  -d '{
    "worker_id": "worker-01",
    "hostname": "worker-01",
    "mac": "00:0c:29:b9:8b:2d"
  }'

# Or bind a pooled device to an existing Worker
api '/devices/00:0c:29:b9:8b:2d/bind?worker_id=worker-01&force=false' \
  -X POST
```

The binding authority is the device ledger's `bound_worker_id`. The dnsmasq hostname is only a derived projection of the binding operation; it may lag and **does not participate in boot identity resolution**. Unbinding returns the device to the pool and keeps the Worker's system disks. Use `force=true` to rebind, and confirm the original device is powered off or isolated from the data plane first.

## 4. Create a System Disk

On the **Workers** page, select a Worker → create a system disk:

- Pick the master image and `os` / `os_version`; a Worker can hold at most one disk per `(os, os_version)` pair.
- Placement is decided by the control plane: candidates must hold that master image locally, be healthy and not drained; the node with the fewest existing system disks wins; the preview allows pinning rows by hand.
- Batch mode: select several Workers → batch-create system disks → review the plan (`dry_run`, same logic as execution) → confirm.

API for a single disk:

```bash
api /workers/worker-01/luns/disk -X POST \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "master",
    "os": "ubuntu",
    "os_version": "24.04",
    "name": "_tpl_ubuntu2404.img"
  }'
```

- Omitting `disk_agent` lets placement choose a node; passing it pins the disk to a storage node.
- The response carries the 12-hex `os_tag` and the full disk NQN. On nvmet backends the control plane **issues the DHHC-1 authentication key at disk creation** and pushes it to the Agent holding the disk.
- The Worker's first system disk is set as the default automatically; the second and later disks do not rewrite it.

For batch creation use `POST /workers/luns/disk/batch`; `dry_run=true` returns the plan only. See the [API Reference](../reference/api.md) for fields and response shapes.

```bash
# Inspect the Worker's disks and os_tag
api /workers/worker-01 | jq \
  '.disks[] | {os, os_version, os_tag, agent}'
```

## 5. Set the Default Boot Disk

A single-disk Worker already has a default disk from creation; skip this step. With multiple disks, select the one to boot:

```bash
api /workers/worker-01/default-disk -X PUT \
  -H 'Content-Type: application/json' \
  -d '{"disk": "<os_tag>"}'
```

WebUI path: Worker detail → system disks → **Set as default**. The default disk determines:

- which entry the iPXE menu selects on timeout;
- which disk's connection data `/boot-vars` projects (the full `disk-nqn`, `storager-ip`, TLS PSK and port).

Without a default disk, the control plane returns `menu-default=reboot` (a 1 ms reboot loop) so the machine never silently boots the wrong system. If the default disk is deleted, a remaining disk is promoted automatically.

## 6. First System Boot and Acceptance

Reboot the device with network boot enabled. Expected chain:

```text
DHCP → iPXE → /boot-vars → NVMe/TCP TLS 1.3 connection to 4421
     → GRUB → kernel → initramfs (consumes NBFT/NBCT) → rootfs → login prompt
```

**Delivery is complete only when a cold boot reaches the login prompt** — not when the device is pooled, not when GRUB appears, and not when the kernel starts. Reporting completion at an intermediate stage leaves data-plane or initramfs problems for the next power-on.

First checks on failure:

| Symptom | Check |
| --- | --- |
| Empty menu or reboot loop | Is the device bound to a Worker? Does the Worker have a system disk and a default disk? Does `/boot-vars` project complete data? |
| Menu appears but the data plane does not connect | Client-to-storage routing and firewall for 4421; `tlshd` status; credential push status |
| Authentication failure | `kurrent workers credential show <worker>`; was the authentication key pushed to the Agent holding the disk? |
| Boot error `0x7f22208e` | Is the master/system disk GPT 4K-native? Is `gpt` in `GET /masters` equal to `4k`? |

For other symptoms see [Troubleshooting](./troubleshooting.md).

## 7. Manage Credentials

Credentials follow the Worker and take effect immediately on rotation or revocation:

```bash
kurrent workers credential show worker-01
kurrent workers credential set worker-01 --generate
kurrent workers credential revoke worker-01
```

`show` reports the key hash prefix and per-Agent push status; `set --generate` rotates in place without printing plaintext; `revoke` makes the next boot fail.

The TLS PSK is derived from the authentication key, so rotating or revoking the key rotates or revokes the PSK atomically. Failed pushes are retried by the control plane's periodic reconciliation, and the status is visible on the Worker detail page under "NVMe-oF credentials". The management plane also exposes equivalent REST endpoints for automation; see the [API Reference](../reference/api.md). The CLI is the preferred interactive path.

Plaintext keys exist only where the chain requires them (the 0600 state files on the control plane and Agent, the storage node's configfs and the kernel `.nvme` keyring); no application-layer encryption is added — disk and host access boundaries are the deployment's responsibility.

## Related Pages

- [System Disk Customization](./system-disk.md)
- [Operations](./operations.md)
- [Troubleshooting](./troubleshooting.md)
- [Boot Chain](../concepts/boot-chain.md)
- [API Reference](../reference/api.md)
