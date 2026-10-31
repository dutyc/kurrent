# Quick Start

This page gives the shortest runnable path: bring up the control plane and a storage node on Linux, place the device firmware, and prepare the first device for network boot.

> Single-host deployment is for functional validation only. Production deployments should separate the control plane from storage nodes and follow the [Deployment Guide](./deployment.md) for networking, firewalls and host baseline.

## 0. Prerequisites

- An x86_64 Linux host with Docker and Compose v2.
- A NIC and a dedicated network for PXE (example `192.168.80.0/24`, control plane at `192.168.80.2`).
- A filesystem that supports reflink (btrfs or xfs) for the disk directory.
- A writable repository root; the commands below run from there.

```bash
git clone https://github.com/dutyc/kurrent
cd kurrent
```

## 1. Host Prerequisites

```bash
# nvmet kernel modules, configfs, tlshd version and boot persistence (idempotent, root required)
sudo bash storager/nvmeof/host-prepare.sh

# Read-only diagnostics
bash storager/nvmeof/host-prepare.sh --check

# The kernel needs NVMe/TCP TLS support; tlshd must be >= 1.5.0 and running
grep -E 'CONFIG_NVME_(TCP_TLS|TARGET_TCP_TLS|KEYRING)' /boot/config-$(uname -r)
sudo apt install -y ktls-utils
sudo systemctl enable --now tlshd

# Read-only check of host exposure
bash control_plane/host-baseline.sh
```

Example disk directory:

```bash
sudo mkfs.btrfs /dev/sdb
sudo mkdir -p /pool1
sudo mount /dev/sdb /pool1
```

## 2. Install the CLI

```bash
# Prebuilt binary (Linux amd64; see Releases for arm64 and Windows)
curl -sL -o cli/kurrent https://github.com/dutyc/kurrent/releases/download/v0.3.0/kurrent-linux-amd64
chmod +x cli/kurrent
sudo install -m 0755 cli/kurrent /usr/local/bin/kurrent

# Or build from source
# cd cli && go build -o kurrent .
```

## 3. Control Plane

Generate the control-plane declaration, edit the five `spec.networking` keys, then converge:

```bash
kurrent config print init-defaults > control_plane/kurrent.yaml
$EDITOR control_plane/kurrent.yaml

kurrent config validate        # full validation before writing
kurrent init                   # start and wait for health; roll back on failure
kurrent config status          # effective declaration, configHash, artifacts and certificate drift
```

Verify:

```bash
curl -k https://127.0.0.1/healthz
```

The WebUI is at `https://<control-plane-address>/`. For the first login, issue an admin token:

```bash
kurrent token create --usage admin
```

## 4. Storage Node

Issue a bootstrap credential bound to the node identity on the control plane:

```bash
kurrent token create --agent-id node-01 --cp-url https://192.168.80.2
```

Run the printed command on the storage node (the same host for this validation):

```bash
kurrent join https://192.168.80.2 --token <bootstrap-token> --agent-id node-01
```

`kurrent join` generates the node declaration, writes the bootstrap credential, synchronizes `.env` and starts the agent and nvmet-host. The merged declaration is validated with the same schema used by the component image before anything is written.

Verify:

```bash
kurrent agents list
# ID       REMARK  HEALTH  BASE_URL                         ROLE  ENABLED  TAGS
# node-01          ok      https://192.168.80.2:4840        disk  true     auto,nvmet,storage
```

> On a single host with the control plane on a bridge network, if `HEALTH` is not `ok`, set `spec.agent.advertiseUrl` in `storager/kurrent.yaml` to `https://host.docker.internal:4840` and run `kurrent join https://192.168.80.2` again.

## 5. Device Firmware

The platform repository does not contain firmware binaries. Fetch an artifact matching the upstream baseline and place it under `tftp/`:

```bash
mkdir -p /tmp/kurrent-fw/pxe-uefi && cd /tmp/kurrent-fw
BASE=https://github.com/dutyc/kurrent-firmware/releases/latest/download
curl -sLO "$BASE/SHA256SUMS"
curl -sL -o pxe-uefi/snponly.efi "$BASE/pxe-uefi-snponly.efi"
sha256sum -c SHA256SUMS --ignore-missing

sudo install -m 0644 pxe-uefi/snponly.efi /path/to/kurrent/tftp/snponly.efi
cd /path/to/kurrent
docker compose restart kurrent-dnsmasq
```

> `tftp/snponly.efi` is a file-level bind mount. Restart the dnsmasq container after replacing it, otherwise the container keeps the old inode.
> For custom NIC drivers or local-media boot, build the firmware as described in [Build and Artifacts](../firmware/build.md).

Confirm the firmware status in the WebUI **Boot Chain** page, or through the API:

```bash
curl -sS \
  --cert control_plane/state/pki/users/admin/client.crt \
  --key control_plane/state/pki/users/admin/client.key \
  --cacert control_plane/state/certs/server.crt \
  https://192.168.80.2/api/cp/boot-assets | jq '.firmware'
```

## 6. First Device

1. Connect the device to the PXE network and set it to network boot.
2. WebUI → **Devices** → open a registration window (default limit 60 minutes, adjustable by declaration).
3. Once the device appears in the pool, bind it to a Worker; create a Worker first if none exists.
4. Create a system disk on the Worker: choose a master image and `os` / `os_version`; the nvmet backend also issues the DHHC-1 credential.
5. Set the disk as default and reboot the device.

See [Usage](./usage.md) for the full workflow and verification.

## Common Failures

| Symptom | Where to look |
| --- | --- |
| `kurrent agents list` is not `ok` | Whether the control plane can reach `advertiseUrl`; port 4840 on the storage node; `docker logs storager-agent` |
| No DHCP response | Whether `interface=` in `dnsmasq.conf` matches the real NIC; UDP 67/69 |
| Device not admitted to the pool | Whether the registration window is open; whether the report carries a valid public key; `/devices/report` logs and audit |
| Firmware capabilities missing from `/boot-assets` | Use the firmware artifact rather than stock iPXE; confirm the six capabilities: HTTPS boot, NVS persistence, device key generation, public-key export, challenge signing, TOFU anchoring |
| Data-plane connect fails | `tlshd` version and state; kernel TLS build options; `4421` port creation in nvmet-host logs |

For other issues, see [Troubleshooting in the Deployment Guide](./deployment.md#troubleshooting).
