# Deployment

> This page starts from blank hosts and walks through preparation, the control plane, storage nodes, firmware and device admission. The deployment units are a **controller server + storage nodes**; they may be separate or run on the same machine, with one Agent per storage server.

Deployment is declaration-driven: `control_plane/kurrent.yaml` and `storager/kurrent.yaml` are the only inputs, and `kurrent init` / `kurrent join` validate and converge them. Container orchestration is an internal detail. Admission means the platform is ready; master images, binding, disk creation and the first system boot belong to system delivery, covered in [System Disk Customization](./system-disk.md) and [First Delivery](./usage.md). Isolate deployment failures by component per [Troubleshooting](./troubleshooting.md); declaration fields and defaults are in the [Configuration Reference](../reference/configuration.md).

## Preparation

### Hosts and roles

| Role | Minimum | Responsibility |
| --- | --- | --- |
| Controller server (×1) | Linux x86_64; Docker with Compose v2; a PXE-side interface (dedicated NIC or VLAN subinterface) | DHCP/TFTP, 443 WebUI/API, PKI, device ledger and boot variables |
| Storage node (×1 or more) | Linux x86_64; Docker with Compose v2; NVMe/TCP TLS kernel support; `tlshd >= 1.5.0`; one btrfs/xfs data disk | Agent (4840) and nvmet-host (4421), holding master images and system disks |
| Bare-metal device (×N) | UEFI x86_64; network boot | Enters the pool over PXE and boots a system disk |

The controller and storage node may share a host, but the roles stay separate: co-location still runs one Agent and follows this page. The controller must have an address on the PXE subnet; browsers, the API and Agent callbacks use the management address. Do not mix the two.

### Network planning

The controller needs two reachable paths:

- **Management plane**: administrator browsers and storage nodes reach 443; the controller uses this address to issue tokens and call Agents.
- **Boot plane**: bare-metal devices reach DHCP (67/UDP) and TFTP (69/UDP); `spec.networking.interface` binds this side, and that interface must hold an address on the PXE subnet.

Prefer two NICs or VLAN subinterfaces. If one NIC carries both planes, the PXE segment must be isolated and must not share the office network. Storage nodes do not serve PXE, but booting clients must be able to route to their **4421** data-plane port; add explicit routes and firewall rules when the segments differ.

Replace these with the actual environment:

- PXE subnet `192.168.80.0/24`: `spec.networking.subnet`
- Controller PXE address `192.168.80.1`: dnsmasq bind address, inside the PXE subnet
- DHCP pool `192.168.80.50 – .100`: handed out to bare-metal devices
- Gateway / DNS `192.168.80.2` / `223.5.5.5`: DHCP options 3 / 6
- Controller management address `192.168.1.10`: WebUI / API / `--cp-url`
- Storage management address `192.168.1.20`: how the controller reaches the Agent (4840)
- Storage data-plane address `192.168.1.20` (or a dedicated data IP): how booting clients reach 4421; must be client-reachable
- Storage hostname `storage-01`: arbitrary; join generates the node identity independently

**Ports**: the controller publishes 67/69 UDP (DHCP/TFTP on the host network), 80 TCP (automatic 301 to 443, carrying no content or credentials) and 443 TCP (the only HTTPS entry: WebUI, `/api/cp`, component enroll/renew and the boot chain). Storage nodes publish 4840 TCP (Agent HTTPS, controller → Agent) and 4421 TCP (NVMe/TCP data plane, TLS 1.3, shared by the firmware and kernel boot stages).

The boot chain behind 443 only proxies three boot endpoints: `/boot-vars`, `/devices/report` and `/devices/challenge`. The remaining `/devices/*` management endpoints are not exposed. The control-plane management plane publishes no TCP port; it listens on an in-container unix socket (`/run/kurrent/cp.sock`) that nginx reaches through a shared volume. nginx clears identity headers by default for every proxied location and injects DN/issuer/serial/fingerprint only when mTLS verification succeeds on `/api/cp/`. The interactive control-plane API docs (`/openapi.json`, `/docs`) are off by default and can be enabled for debugging with `KURRENT_CP_OPENAPI=1`.

### Repository and CLI

Both the controller and storage nodes need the repository and the `kurrent` CLI. On split hosts, run this once per host; on a co-located host, run it once.

```bash
git clone https://github.com/dutyc/kurrent
cd kurrent

# Release CLI binary (v0.3.0+; Linux amd64/arm64, Windows)
BASE=https://github.com/dutyc/kurrent/releases/download/v0.3.0
curl -sL -o cli/kurrent "$BASE/kurrent-linux-amd64"
chmod +x cli/kurrent
# Global install; the rest of this guide uses plain `kurrent`
sudo install -m 0755 cli/kurrent /usr/local/bin/kurrent
kurrent version   # verify
```

> Versions move with the Releases page (https://github.com/dutyc/kurrent/releases; Windows: `kurrent-windows-amd64.exe`). Alternatively build from source instead of downloading (`cd cli && go build -o kurrent .`, needs Go 1.27+).

### Host dependencies

Both hosts need Docker, Compose v2, `git`, `curl`, `jq` and `openssl`. Install Docker and Compose from the distribution or the official Docker instructions; the remaining tools on Debian/Ubuntu:

```bash
sudo apt install -y git curl jq openssl
docker --version && docker compose version
```

**Storage nodes** additionally prepare the data-plane host. Confirm the data-disk device name first; the `mkfs` example below is for an empty data disk and must never point at a system disk:

```bash
# 1. nvmet host prerequisites (modules, configfs, tlshd; idempotent)
sudo bash storager/nvmeof/host-prepare.sh
#    Read-only diagnosis (no root, for pre-deployment checks):
#    bash storager/nvmeof/host-prepare.sh --check

# 2. NVMe/TCP TLS: the kernel needs CONFIG_NVME_TCP_TLS /
#    CONFIG_NVME_TARGET_TCP_TLS / CONFIG_NVME_KEYRING,
#    and tlshd >= 1.5.0 must be running
grep -E 'CONFIG_NVME_(TCP_TLS|TARGET_TCP_TLS|KEYRING)' \
  /boot/config-$(uname -r)
# If the distro package is older than 1.5.0, build from source per
# storager/nvmeof/nvmet-host/README.md
sudo apt install -y ktls-utils
sudo systemctl enable --now tlshd

# 3. Prefer btrfs or xfs for the data disk (both support reflink/FICLONE,
#    so master clones are instant; ext4 has no reflink and falls back to
#    full copies, clone time growing linearly with image size)
sudo mkfs.btrfs /dev/sdb
sudo mkdir -p /pool1
sudo mount /dev/sdb /pool1
sudo mkdir -p /pool1/storager_img
```

Persist the data-disk mount in `/etc/fstab` (prefer the UUID from `blkid`) or an equivalent systemd mount so it returns after reboot; otherwise `diskDir` lands in an empty directory on the root filesystem.

> `host-prepare.sh` and `kurrent join` check the nvmet modules, configfs and tlshd (**>= 1.5.0 and running**); the kernel TLS build options still need a manual check. A tlshd older than 1.5.0 lets a stale PSK shadow the rotated one (1.5.0 looks up the process keyring first), so the check fails closed. If the `4421` port cannot be created, the node reports the port as not ready and the control plane **refuses the boot** — there is no plaintext fallback.

### Boot network boundary

The first hop of the boot chain (DHCP → TFTP `snponly.efi` → TFTP `boot.ipxe`) has no cryptographic anchor: the device firmware does not verify it, and iPXE has not yet obtained the controller certificate. The current model treats the **boot segment itself as the trust boundary**, keeping device-identity TOFU (NVRAM key + registration window + binding) and server TOFU on top. A deployment must satisfy:

- **Physical/logical isolation**: the PXE segment is a dedicated VLAN or physical port, not shared with the office network or the storage data plane; the controller is the only host serving DHCP and TFTP on it.
- **Layer-2 controls**: enable DHCP snooping (only the controller uplink is trusted), Dynamic ARP Inspection, RA guard, and port security/MAC limits; use 802.1X or MAB to restrict which ports can attach where available.
- **Admission control**: open the registration window only when needed and set a quota; once closed, unknown MACs cannot enter the pool.
- **Revocation closed loop**: `DELETE /devices/{mac}` writes `MAC,ignore` into the dnsmasq binding file, so a revoked device gets no lease; re-registration (`/devices/report`) or purging (`DELETE /devices/revoked`) removes that line.
- **Minimal TFTP surface**: the TFTP root mounts only `snponly.efi` and `boot.ipxe`; configuration and menu are fetched over HTTPS.

The full threat model, residual risks and deployment checklist are in [Security Model](../architecture/security.md).

## Step 1: Controller Server

### 1.1 Declare the PXE network

The controller declaration `control_plane/kurrent.yaml` is the only input. Generate the annotated template, then fill the five `spec.networking` keys:

```bash
kurrent config print init-defaults > control_plane/kurrent.yaml
vim control_plane/kurrent.yaml
```

The minimum bootable content is below; leave the other blocks at their template defaults:

```yaml
apiVersion: kurrent.io/v1
kind: ControlPlaneConfiguration
metadata:
  name: kurrent-cp
spec:
  networking:
    interface: enp3s0
    subnet: 192.168.80.0/24
    dhcpRange: 192.168.80.50,192.168.80.100
    gateway: 192.168.80.2
    dns: 223.5.5.5
```

- `interface` is the **PXE-facing interface**, not the management NIC. This is the most common mistake on multi-NIC hosts.
- `dhcpRange` is one `start,end` string, not two list items.
- The server certificate SAN defaults to `127.0.0.1` and `localhost` only. To open the WebUI by management IP or verify curl certificates, add that IP before the first `kurrent init`:

```yaml
  serverCert:
    san: "IP:192.168.1.10,IP:192.168.80.1,IP:127.0.0.1,DNS:localhost"
```

Validate against the runtime schema before writing, then inspect the effective declaration with defaults injected:

```bash
# Unknown fields, cross-field semantics and policy caps fail before writing
kurrent config validate
kurrent config show                # effective declaration
```

### 1.2 Initialize and bring up the control plane

```bash
kurrent init
```

`kurrent init` builds the validator image, fully validates, applies the declaration, starts/restarts the services, waits for `/healthz`, and verifies that dnsmasq stays running. On startup the controller renders `dnsmasq/dnsmasq.conf` from `spec.networking` (the yml is authoritative, the conf is derived — do not hand-edit it) and generates the TOFU server certificate and component PKI.

The command is idempotent: re-run it after editing the declaration. **Failure semantics**: an invalid declaration is never written; if the controller does not become healthy (including startup-derived artifact failures) or dnsmasq does not stay running, the command exits non-zero and automatically rolls back to the previous declaration and running state.

### 1.3 Verify the service

```bash
curl -k https://127.0.0.1/healthz           # {"status":"ok"}
head -5 dnsmasq/dnsmasq.conf                # interface=... matches the declaration
kurrent config status                       # configHash + derived artifacts
docker ps                                   # control plane / WebUI / dnsmasq
```

Open `https://<controller-management-address>/` in a browser (WebUI; the only entry on 443 — plain HTTP is 301-redirected). For token sign-in, issue `kurrent token create --usage admin` and submit it on the login page to obtain an HttpOnly session cookie (12-hour TTL by default, sliding renewal only in the second half). For automatic browser mTLS, convert the admin certificate to PKCS#12 and import it into the system trust store:

```bash
openssl pkcs12 -export -out kurrent-admin.p12 \
  -inkey control_plane/state/pki/users/admin/client.key \
  -in control_plane/state/pki/users/admin/client.crt
# Double-click kurrent-admin.p12 to import (Windows/macOS system store;
# Firefox keeps its own store and needs a separate import)
```

**CLI credentials**: setup has already bootstrapped `control_plane/state/admin.conf` (entry URL + admin client certificate and trust anchor, 0600). Discovery order is `--credentials` > `KURRENT_CREDENTIALS` > walking up from the current directory to `control_plane/state/admin.conf` → the repo layout next to the executable → the user-level snapshot `~/.kurrent/admin.conf`. Inside the repository no extra configuration is needed; outside it, run `kurrent credentials install` once (re-run after credential rotation).

### 1.4 Firewall and host baseline

```bash
# Controller server
sudo ufw allow 67,69/udp && sudo ufw allow 80,443/tcp && sudo ufw reload

# Host precondition for the auth model: read-only checks of listeners,
# Docker exposure and credential-file permissions
bash control_plane/host-baseline.sh
```

- A controller host should expose only 22 (ops), 53/67/69 (dnsmasq) and 80/443 (nginx); storage nodes add 4840/4421.
- ufw does not filter Docker-published ports: constrain them in the `DOCKER-USER` chain.
- The controller container does not mount `docker.sock`.
- CA private keys, `users.yml`, `sessions.yml`, `admin.conf` and `audit.jsonl` should be mode 600.
- Treat FAIL items as blockers: host root or a compromised nginx process is equivalent to a compromised management plane.

### 1.5 Issue the storage-node bootstrap credential

```bash
kurrent token create --agent-id 3f2a9c1d8b7e --cp-url https://192.168.1.10
# omitting --agent-id generates a 12-lowercase-hex node identity
```

`--cp-url` must be the **HTTPS entry of the control plane as reachable from the storage node** (port 443 `/api/cp` reverse proxy — Agents enroll/renew through it; derived from the `--server` host otherwise). `--agent-id` is the node identity (= the declaration `metadata.name`) and accepts only 12 lowercase hex characters; the token is bound to it. Descriptive names go in `metadata.remark`. The output contains a join command that can run directly on the storage node:

```
bootstrap token: <token> (expires ...; bound to node 3f2a9c1d8b7e; 0/1)

# Run on the bound node (--agent-id is written into the declaration metadata.name):
kurrent join https://192.168.1.10 --token <token> --agent-id 3f2a9c1d8b7e
```

> The token is **bound to the node identity and single-use by default**: enroll only accepts that `agent_id`; an allowance is reserved atomically before issuance and released if issuance fails. TTL defaults to 60 minutes (`spec.pki.bootstrapTokenTtlMinutes`) with a 24-hour cap (`spec.pki.bootstrapTokenMaxTtlMinutes`); for bulk onboarding use `kurrent token create --agent-id ... --uses N` (1..1000, no higher than `spec.pki.bootstrapTokenMaxUses`, default 100). Re-issue after expiry or revocation. **No nvmet-host credential needs issuing**: it is derived by the control plane when the agent enrolls with `backend=nvmet`.

### 1.6 Update the declaration later

```bash
vim control_plane/kurrent.yaml
# Full validation before writing; nothing changes on failure
kurrent config validate
# Apply and converge; automatic rollback on failure
kurrent init
# Effective declaration, configHash, derived artifacts and certificate drift
kurrent config status
```

SAN/days changes are **reported as drift only** by default (`derived.serverCert.drift`) and do not change the TOFU fingerprint. To deliberately make devices trust a new certificate, set `spec.serverCert.rotateOnChange: true` and re-run `kurrent init`. See [Configuration Reference](../reference/configuration.md) and [Operations](./operations.md) for policy blocks and temporary overrides.

Component certificate revocation, CRL timing and node recovery are outside the deployment mainline; see [Operations](./operations.md#component-certificates).

## Step 2: Storage Node

### 2.1 Declare the node configuration (before join)

The node declaration `storager/kurrent.yaml` decides the data-plane address, disk directory and NQN namespace. **Declare it before join**, rather than editing defaults afterwards: on split hosts the default `advertiseUrl` points at the controller, so the controller cannot reach the Agent.

```bash
kurrent config print node-defaults > storager/kurrent.yaml
vim storager/kurrent.yaml
```

Keys to fill in:

```yaml
apiVersion: kurrent.io/v1
kind: NodeConfiguration
metadata:
  name: ""
  remark: Production storage
spec:
  agent:
    backend: nvmet
    advertiseUrl: https://192.168.1.20:4840
    diskDir: /pool1/storager_img
    nqnBase: nqn.2026-07.com.kurrent
  controlPlane:
    url: https://192.168.1.10
```

- `advertiseUrl` is the **controller → Agent 4840** address. Split hosts must use the storage management address; co-located hosts can keep the default, or use `https://host.docker.internal:4840` when the controller runs on a bridge network and cannot reach the host.
- `diskDir` must be an absolute host path on a mounted data disk. join syncs it into `.env` as `KURRENT_DISK_DIR`, the compose mount source. Create the directory first if it does not exist.
- `metadata.name` is the machine identity (12 lowercase hex), written by join from `--agent-id`; descriptive names go in `metadata.remark`. When the template leaves it empty, do not run `kurrent config validate --node` first: validation fails before the identity is written. join merges the identity and validates the result with the same schema; use `--node` for later edits.
- `nqnBase` is the single namespace for host and disk NQNs; keep it consistent across the cluster.

### 2.2 Join the cluster

Run the command from the token output inside the repository on the storage node:

```bash
kurrent join https://192.168.1.10 --token <token> --agent-id 3f2a9c1d8b7e
```

`kurrent join` performs three operations (idempotent and safe to re-run):

1. Generates/updates `storager/kurrent.yaml`: creates it from the template when missing, merges it when present (hand edits are kept), writes `metadata.name`, and syncs `spec.controlPlane.url`. A declared identity that differs from `--agent-id` is rejected.
2. Writes the node-bound bootstrap credential `storager/bootstrap/agent.token`. `nvmet-host.token` is derived by the agent's enroll — nothing to provide by hand.
3. Syncs `.env` interpolation keys and starts/restarts the agent container; the backend picks the orchestration directory (nvmet → `storager/nvmeof`, stgt/lio → `storager/iscsi`).

Before writing anything, join validates the merged declaration inside the agent image against the same schema, so unknown fields, non-absolute paths and invalid NQNs fail immediately rather than after container startup.

### 2.3 Verify the storage node

```bash
docker ps    # storager-agent / kurrent-nvmet-host

docker logs storager-agent 2>&1 | grep pki
docker logs kurrent-nvmet-host 2>&1 | grep -i "数据面端口就绪"
docker exec kurrent-nvmet-host python /app/healthcheck.py; echo $?
```

Expected: the agent prints `pki: client cert ok`, nvmet-host prints `4421 tsas=tls1.3`, and the health check returns 0 (UDS + mTLS + configfs). For stgt/lio backends, inspect the corresponding containers.

Back on the controller, confirm registration and health:

```bash
kurrent agents list
# ID             REMARK       HEALTH  BASE_URL
# 0d26b6f33a89   storage-01   ok      https://192.168.1.20:4840
```

The output also includes ROLE, ENABLED and TAGS; `HEALTH=ok` is a live probe from the controller to the Agent.

**Keep the two addresses distinct**: `advertiseUrl` is the controller-to-Agent 4840 address; `storager_ip` is the data-plane address written into `/boot-vars` for booting clients to reach 4421. When the data plane uses a separate segment, set `storager_ip` in the WebUI Agent detail so clients can reach it.

**Firewall** (storage node):

```bash
sudo ufw allow 4840,4421/tcp && sudo ufw reload
```

> nvmet-host's management plane uses a shared-volume UDS (`/run/kurrent/nvmet/nvmet-host.sock`, directory 0700): no TCP port, unreachable from the LAN. `4421` is the only data-plane port — restrict its sources as needed, but the booting clients' segment must be able to reach it.

### 2.4 Master images and system disks

A storage node does not network-boot itself; it stores and exports disks for Workers. `diskDir` holds two kinds of files:

- **Master images** are prepared once and carry a `_tpl_` marker in the filename (for example `_tpl_ubuntu2404.img`). They must be whole-disk images with a 4K-native GPT. After being placed in `diskDir`, the agent scans them into the master list; a master is only a clone source and is never exported to a device.
- **System disks** are cloned from a master by the controller per Worker, each with its own `os_tag`, disk NQN and authentication credential. Creation and first boot are covered in [First Delivery](./usage.md).

See [System Disk Customization](./system-disk.md) for building and validating a master image. A new storage node can join without any master image; before the first disk creation, at least one healthy node must hold the target master, because the placement policy only selects nodes that hold it.

## Step 3: Firmware and Device Admission

### 3.1 Install the firmware artifact

The platform repository does not contain firmware binaries. Device firmware is built by the companion repository [kurrent-firmware](https://github.com/dutyc/kurrent-firmware); use a release artifact matching the current upstream baseline, or build it per [Firmware Build and Deployment](../firmware/build-deploy.md).

```bash
mkdir -p /tmp/kurrent-fw/pxe-uefi && cd /tmp/kurrent-fw
BASE=https://github.com/dutyc/kurrent-firmware/releases/latest/download
curl -sLO "$BASE/SHA256SUMS"
curl -sL -o pxe-uefi/snponly.efi "$BASE/pxe-uefi-snponly.efi"
sha256sum -c SHA256SUMS --ignore-missing

# Place it in the platform repository's TFTP root.
# Restart dnsmasq so the file-level mount points at the new inode.
sudo install -m 0644 pxe-uefi/snponly.efi \
  /path/to/kurrent/tftp/snponly.efi
cd /path/to/kurrent
docker compose restart kurrent-dnsmasq
```

Mainline currently fixes the PXE first hop to `snponly.efi` and does not switch artifacts per device. Machines that need `pxe-uefi/ipxe.efi` cannot share the same PXE segment with the default first hop, so the first-hop artifact must be unified before deployment; see [Firmware Build and Deployment](../firmware/build-deploy.md#_2-select-artifacts).

Confirm firmware status and the six capabilities (HTTPS boot, NVS persistence, device key generation, public-key export, challenge signing, TOFU anchoring) in the WebUI **Boot Chain** page, or query the API from the controller repository with the admin certificate:

```bash
curl -sS \
  --cert control_plane/state/pki/users/admin/client.crt \
  --key control_plane/state/pki/users/admin/client.key \
  --cacert control_plane/state/certs/server.crt \
  https://<controller-management-address>/api/cp/boot-assets | jq '.firmware'
```

### 3.2 First boot and admission

Open the registration window in WebUI → **Devices** first (TTL is bounded by `spec.registration.windowMaxTtlMinutes`, default 60 minutes; optional admission quota `max_devices`), then connect the bare-metal device to the PXE segment and enable network boot. The window must be open before the device reports; after it closes, reports only refresh the fingerprint and never admit the device.

```bash
# On the controller: watch the DHCP/boot flow
docker logs -f kurrent-dnsmasq 2>&1 | grep -E "DHCPACK|pxe|ipxe"
```

Expected chain: the device gets an address via DHCP → TFTP pulls iPXE and the `boot.ipxe` pointer → iPXE fetches `boot.ipxe.cfg` over HTTPS → `keygen` creates an ECDSA P-256 private key on first use (kept in EFI NVRAM) → `/devices/report` sends the fingerprint and public key → **the device appears under Devices and completes key claim**.

Close the registration window after the device is admitted. Once every device has claimed its key, enable **signature enforcement** on the Devices page: `/boot-vars` then serves only bound devices that pass challenge-response verification.

Admission only proves that the platform side and the registration path work; the device has no bootable system yet. A real boot takes three steps: a master image for the target distribution on a storage node → clone a system disk for the Worker and make it the default → cold boot to a login prompt. See [System Disk Customization](./system-disk.md) for the master image and [First Delivery](./usage.md) for the last two.
