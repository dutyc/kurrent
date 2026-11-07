# Deployment Prerequisites

This page collects the host, network and host-OS prerequisites for deployment. The deployment procedure itself is in [Deployment](../guide/deployment.md); the authoritative port table is in [Architecture](../architecture/index.md#ports); security boundaries and the checklist are in the [Security Model](../architecture/security.md#deployment-checklist).

## Host roles

| Role | Minimum requirements | Responsibility |
| --- | --- | --- |
| Control-plane server (×1) | Linux x86_64; Docker and Compose v2; Node.js 18+ and npm (WebUI build); one PXE-side interface (dedicated NIC or VLAN subinterface) | DHCP/TFTP, 443 WebUI/API, PKI, device ledger and boot variables |
| Storage node (×1 or more) | Linux x86_64; Docker and Compose v2; kernel NVMe/TCP TLS support; `tlshd >= 1.5.0`; one btrfs/xfs data disk | Agent (4840) and nvmet-host (4421), hosting master images and system disks |
| Bare-metal device (×N) | UEFI x86_64; UEFI network boot capable | Enters the pool over PXE and boots a system disk |

The control plane and storage nodes may run on the same host, but the roles do not merge: co-location still runs one Agent and follows the deployment order. The control plane must have an address on the PXE segment; WebUI, API and Agent callbacks use management-network addresses, and the two must not be mixed.

Bare-metal devices support UEFI boot only. Legacy BIOS (CSM) boot is no longer supported: build artifacts are UEFI-only, and device identity, EFI variable persistence and NBFT/NBCT handoff all depend on interfaces provided by UEFI.

## Network and ports

The control plane needs two reachable paths:

- **Management plane**: administrator browsers and storage nodes reach 443; the control plane issues tokens and calls Agents through this address.
- **Boot plane**: bare-metal devices reach DHCP (67/UDP) and TFTP (69/UDP); `spec.networking.interface` binds this side, and that interface must hold an address inside the PXE subnet.

Prefer two NICs or VLAN subinterfaces to separate the two sides. When one NIC carries both, the PXE segment must be isolated and must not share the office network. Booting clients must be able to route to the storage node's 4421 data-plane port; allow it explicitly in routing and firewalls across subnets.

See [Architecture](../architecture/index.md#ports) for the full port table and component responsibilities. The boundaries to remember:

- The control-plane host publishes 53 UDP/TCP (DNS), 67/69 UDP (DHCP/TFTP), 80 TCP (301 redirect to 443 only) and 443 TCP; 443 is the only HTTPS entry.
- The control-plane management plane publishes no TCP port; it listens on a container-local unix socket reached by nginx through a shared volume.
- On 443, the boot chain proxies only `/boot-vars`, `/devices/report` and `/devices/challenge`; other `/devices/*` management endpoints are not exposed.
- A storage node publishes 4840 TCP (Agent HTTPS, control plane → Agent) and 4421 TCP (NVMe/TCP data plane, TLS 1.3, shared by the firmware and kernel segments).
- The nvmet-host management plane uses a shared-volume UDS and occupies no TCP port.

## Host dependencies

Both hosts need Docker, Compose v2, `git`, `curl`, `jq` and `openssl`; the control-plane host additionally needs Node.js 18+ and npm for the WebUI build. Install Docker and Compose from the distribution or Docker's official documentation; on Debian/Ubuntu the remaining tools install in one step:

```bash
sudo apt install -y git curl jq openssl
docker --version && docker compose version && node --version && npm --version
```

`webui/app/dist` is a build artifact and is not committed; the control-plane container bind-mounts the host directory read-only into nginx. Before the first start, run `(cd webui/app && npm ci && npm run build)` at the repository root and confirm `dist/index.html` exists. If compose has already started once, the missing `dist` was created as a root-owned empty directory; run `sudo rm -rf webui/app/dist` before building.

## Storage-node kernel and tlshd

The NVMe/TCP TLS data plane depends on the host kernel and `tlshd`:

- The kernel must provide `CONFIG_NVME_TCP_TLS`, `CONFIG_NVME_TARGET_TCP_TLS` and `CONFIG_NVME_KEYRING`.
- `tlshd >= 1.5.0` must be installed and running; older versions let an old PSK shadow the new one after rotation, and the scripts treat this as a failure.
- If the distribution package is older than 1.5.0, build from source as described in `storager/nvmeof/nvmet-host/README.md`.
- If the 4421 listener cannot be created, the node reports the data-plane port as unready and the control plane refuses the boot without falling back to plaintext.

`host-prepare.sh` requires root and idempotently loads kernel modules, mounts configfs and persists both across reboot; `kurrent host check --role storage` is the read-only diagnostic and needs no root. See [Deployment](../guide/deployment.md#host-dependencies) for the commands.

## Data disk

Master cloning relies on reflink:

- btrfs or xfs support reflink/FICLONE, making clones close to instant.
- ext4 does not support reflink and falls back to a full copy, so clone time grows linearly with image size.
- Record the data disk in `/etc/fstab` (prefer a `blkid` UUID) or an equivalent systemd mount so it is mounted after reboot; otherwise `diskDir` resolves to an empty directory on the root filesystem.

## Host baseline

The control-plane host should expose only 22 (operations), 53/67/69 (dnsmasq) and 80/443 (nginx); storage nodes additionally expose 4840/4421.

- The nvmet-host management plane uses a shared-volume UDS and creates no TCP listener.
- Control-plane containers do not mount `docker.sock`.
- CA private keys, `users.yml`, `sessions.yml`, `admin.conf` and `audit.jsonl` should be 600.
- ufw does not filter Docker-published ports; restrict inbound traffic in the `DOCKER-USER` chain when needed.
- Host root compromise or nginx compromise is equivalent to management-plane compromise.

Run `sudo kurrent host check` for a read-only check (use `--role control|storage` when hosts are split, `all` when co-located); resolve any FAIL before proceeding, and `--strict` counts SKIP as failure. See the [Security Model](../architecture/security.md#deployment-checklist) and [Operations](../guide/operations.md#security-baseline) for the full checklist.

## Related pages

- [Deployment](../guide/deployment.md)
- [Architecture](../architecture/index.md#ports)
- [Security Model](../architecture/security.md#deployment-checklist)
- [Configuration](./configuration.md)
- [Operations](../guide/operations.md#security-baseline)
