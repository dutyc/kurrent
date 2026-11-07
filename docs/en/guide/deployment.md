# Deployment

> This page starts from blank hosts and walks through preparation, the control plane, storage nodes, firmware and device admission. The deployment units are a **controller server + storage nodes**; they may be separate or run on the same machine, with one Agent per storage server.

Deployment is declaration-driven: `control_plane/kurrent.yaml` and `storager/kurrent.yaml` are the only inputs, and `kurrent init` / `kurrent join` validate and converge them. Container orchestration is an internal detail. Admission means the platform is ready; master images, binding, disk creation and the first system boot belong to system delivery, covered in [System Disk Customization](./system-disk.md) and [First Delivery](./usage.md). Isolate deployment failures by component per [Troubleshooting](./troubleshooting.md); declaration fields and defaults are in the [Configuration Reference](../reference/configuration.md), and host prerequisites are in the [Deployment Prerequisites](../reference/host-prerequisites.md).

## Preparation

### Hosts and roles

The deployment units are a controller server plus one or more storage nodes; they may share a host, but the roles stay separate. Host minimums, kernel and `tlshd` versions, data disks and the host baseline are in [Deployment Prerequisites](../reference/host-prerequisites.md). The controller must have an address on the PXE subnet; browsers, the API and Agent callbacks use the management address. Do not mix the two.

### Network planning

The controller needs two reachable paths:

- **Management plane**: administrator browsers and storage nodes reach 443; the controller uses this address to issue tokens and call Agents.
- **Boot plane**: bare-metal devices reach DHCP (67/UDP) and TFTP (69/UDP); `spec.networking.interface` binds this side, and that interface must hold an address on the PXE subnet.

Prefer two NICs or VLAN subinterfaces. If one NIC carries both planes, the PXE segment must be isolated and must not share the office network. Booting clients must be able to route to the storage node's **4421** data-plane port; add explicit routes and firewall rules when the segments differ.

Replace these with the actual environment:

- PXE subnet `192.168.80.0/24`: `spec.networking.subnet`
- Controller PXE address `192.168.80.1`: dnsmasq bind address, inside the PXE subnet
- DHCP pool `192.168.80.50 – .100`: handed out to bare-metal devices
- Gateway / DNS `192.168.80.2` / `223.5.5.5`: DHCP options 3 / 6
- Controller management address `192.168.1.10`: WebUI / API / `--cp-url`
- Storage management address `192.168.1.20`: how the controller reaches the Agent (4840)
- Storage data-plane address `192.168.1.20` (or a dedicated data IP): how booting clients reach 4421; must be client-reachable
- Storage hostname `storage-01`: arbitrary; the control plane allocates the node identity at bootstrap-token issuance and join stores it in the node state file, independent of the hostname

Ports and listener boundaries are in [Architecture](../architecture/index.md#ports) and [Deployment Prerequisites](../reference/host-prerequisites.md#network-and-ports). The control-plane management plane publishes no TCP port; the boot chain behind 443 proxies only three boot endpoints, and the remaining `/devices/*` management endpoints are not exposed. The interactive control-plane API docs (`/openapi.json`, `/docs`) are off by default and can be enabled for debugging with `KURRENT_CP_OPENAPI=1`.

### Repository and CLI

Both the controller and storage nodes need the repository and the `kurrent` CLI. On split hosts, run this once per host; on a co-located host, run it once.

```bash
git clone https://github.com/dutyc/kurrent
cd kurrent

# Release CLI binary (v0.3.0+; Linux amd64/arm64, Windows)
mkdir -p bin
BASE=https://github.com/dutyc/kurrent/releases/download/v0.3.0
curl -sL -o bin/kurrent "$BASE/kurrent-linux-amd64"
chmod +x bin/kurrent
# Global install; the rest of this guide uses plain `kurrent`
sudo install -m 0755 bin/kurrent /usr/local/bin/kurrent
kurrent version   # verify
```

> Versions move with the Releases page (https://github.com/dutyc/kurrent/releases; Windows: `kurrent-windows-amd64.exe`). Alternatively build from source instead of downloading (`mkdir -p bin && cd cli && go build -o ../bin/kurrent .`, needs Go 1.27+). Build output goes to the repository-level `bin/`, never the source directory.

### State root and deployment record

Runtime state lives outside the repository. The checkout supplies declarations, code and build artifacts only; ledgers, PKI, bootstrap credentials and logs live in the host state root, so moving or renaming the checkout does not change the deployment identity.

- The state root defaults to `$XDG_STATE_HOME/kurrent` (`~/.local/state/kurrent` when unset); `KURRENT_STATE_DIR` or `--state-dir` overrides it.
- The deployment record `~/.kurrent/deployment.yaml` (0600) stores the pinned project names (`kurrent`, `kurrent-storage`), repository root, state root and role status. `kurrent deployment show` prints it; `-o env` renders the variables needed for manual compose runs.
- The management credential snapshot lives in `~/.kurrent/` (0700) and is installed by `kurrent init` on first deployment.

Layout under the state root:

| Path | Content | In-container mount |
|---|---|---|
| `<state>/control-plane/state` | device/Worker ledgers, PKI, certificates and `admin.conf` | `/app/state` |
| `<state>/control-plane/config/agents.yml` | Agent ledger | `/app/config/agents.yml` |
| `<state>/control-plane/dnsmasq` | rendered configuration, DHCP hosts and leases | `/app/dnsmasq` |
| `<state>/control-plane/bootstrap` | one-shot management credential exchange | `/app/bootstrap` |
| `<state>/storager/state` | node identity `agent.id` | `/etc/kurrent/state` |
| `<state>/storager/bootstrap` | `agent.token` and derived credentials | `/etc/kurrent/bootstrap` |
| `<state>/storager/pki/agent-<id>`, `pki/nvmet-<id>` | component certificates | `/etc/kurrent/pki` |
| `<state>/storager/logs` | agent and nvmet-host logs | `/var/log/kurrent` |
| `<state>/storager/certs/cp-ca.crt` | controller TOFU trust anchor | `/etc/kurrent/cp-ca.crt` |

Co-located deployments share one state root; on split hosts each storage node uses its own, and the controller trust anchor is supplied through join's `--cp-ca`. A host supports one deployment: container and compose project names are fixed, and a second deployment needs a separate host.

### Host dependencies

Both hosts need Docker, Compose v2, `git`, `curl`, `jq` and `openssl`; the **control-plane host additionally needs Node.js 18+ and npm** to build the WebUI static assets. Install Docker and Compose from the distribution or the official Docker instructions; the remaining tools on Debian/Ubuntu:

```bash
sudo apt install -y git curl jq openssl
docker --version && docker compose version && node --version && npm --version
```

**Storage nodes** additionally prepare the data-plane host; the full prerequisites and diagnostic semantics are in [Deployment Prerequisites](../reference/host-prerequisites.md#storage-node-kernel-and-tlshd). Confirm the data-disk device name first; the `mkfs` example below is for an empty data disk and must never point at a system disk:

```bash
# 1. nvmet host prerequisites (modules, configfs, tlshd; idempotent)
sudo bash storager/nvmeof/host-prepare.sh
#    Read-only diagnosis (no root, for pre-deployment checks; run at the repo root):
#    kurrent host check --role storage

# 2. Confirm kernel NVMe/TCP TLS support
grep -E 'CONFIG_NVME_(TCP_TLS|TARGET_TCP_TLS|KEYRING)' \
  /boot/config-$(uname -r)
sudo apt install -y ktls-utils
sudo systemctl enable --now tlshd

# 3. Data disk (btrfs/xfs support reflink)
sudo mkfs.btrfs /dev/sdb
sudo mkdir -p /pool1 /pool1/storager_img
sudo mount /dev/sdb /pool1
```

Persist the data-disk mount in `/etc/fstab` (prefer the UUID from `blkid`) or an equivalent systemd mount so it returns after reboot; otherwise `diskDir` lands in an empty directory on the root filesystem.

### Boot network boundary

The first hop of the boot chain (DHCP → TFTP `snponly.efi` → TFTP `boot.ipxe`) has no cryptographic anchor: the device firmware does not verify it, and iPXE has not yet obtained the controller certificate. A deployment must treat the **boot segment itself as the trust boundary**:

- The PXE segment is a dedicated VLAN or physical port; the controller is the only host serving DHCP and TFTP on it.
- Enable DHCP snooping (only the controller uplink is trusted), Dynamic ARP Inspection, RA guard and port security; use 802.1X or MAB to restrict attachable ports where available.
- Open the registration window only when needed, set a quota, and close it afterwards; the TFTP root mounts only `snponly.efi` and `boot.ipxe`.

The full threat model, residual risks and deployment checklist are in the [Security Model](../architecture/security.md#deployment-checklist).

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
# WebUI static assets (dist is not committed; build before the first up)
(cd webui/app && npm ci && npm run build && test -f dist/index.html)

kurrent init
```

`webui/app/dist` is a build artifact bind-mounted read-only into nginx by the control-plane container. Build it before the first `docker compose up`: a missing `dist` is created by compose as a root-owned empty directory, after which a normal user's `npm run build` fails on permissions (run `sudo rm -rf webui/app/dist` and rebuild). `kurrent init` preflights that directory and fails with the same remediation when it is missing or owned by another user.

`kurrent init` is a complete first-deployment transaction:

1. Preflight Docker/Compose v2, the repository layout, the WebUI build output, and ownership/writability of the state root and `~/.kurrent`; legacy in-repository state requires `--adopt-state`.
2. Fully validate the declaration against the runtime schema inside the controller image, then apply it.
3. Create the state directories, seed the compose file mount points (`agents.yml`, dnsmasq configuration and leases), and write the deployment record as `bootstrapping`.
4. Start the stack under the pinned project `kurrent` and wait for `/healthz`.
5. The controller publishes the management credential (entry URL, admin client certificate and trust anchor) into a one-shot exchange directory; the CLI verifies it, installs `~/.kurrent/admin.conf`, and runs an mTLS `GET /whoami` self-test with the installed copy.
6. The exchange bundle is consumed and removed, dnsmasq is restarted and verified to stay running, and the record becomes `ready`.

The command is idempotent: re-run it after editing the declaration; after moving the checkout it reuses the recorded state root, recreates the containers with `--force-recreate` and updates the recorded repository root while ledgers stay unchanged. **Failure semantics**: an invalid declaration is never written; if the controller does not become healthy or dnsmasq does not stay running, the command exits non-zero and automatically rolls back to the previous declaration and running state; if the credential exchange or self-test fails, the bundle and the `bootstrapping` record stay in place and a rerun resumes from that step.

The controller renders `<state>/control-plane/dnsmasq/dnsmasq.conf` from `spec.networking` (the declaration is authoritative, the conf is derived — do not hand-edit it) and generates the TOFU server certificate and component PKI under the state root. Files written by the containers as root are re-owned to the state directory's owner, so host-side backups and migration do not need `sudo`.

### 1.3 Verify the service

```bash
STATE=${KURRENT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/kurrent}
curl -k https://127.0.0.1/healthz           # {"status":"ok"}
curl -ks https://127.0.0.1/ | grep -q '<div id="root">'   # WebUI assets are mounted
head -5 "$STATE/control-plane/dnsmasq/dnsmasq.conf"        # interface=... matches the declaration
kurrent config status                       # configHash + derived artifacts
kurrent deployment show                     # repository root, state root, projects, status
docker ps                                   # control plane / WebUI / dnsmasq
```

Open `https://<controller-management-address>/` in a browser (WebUI; the only entry on 443 — plain HTTP is 301-redirected). For token sign-in, issue `kurrent token create --usage admin` and submit it on the login page to obtain an HttpOnly session cookie (12-hour TTL by default, sliding renewal only in the second half). For automatic browser mTLS, convert the admin certificate to PKCS#12 and import it into the system trust store:

```bash
STATE=${KURRENT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/kurrent}
openssl pkcs12 -export -out kurrent-admin.p12 \
  -inkey "$STATE/control-plane/state/pki/users/admin/client.key" \
  -in "$STATE/control-plane/state/pki/users/admin/client.crt"
# Double-click kurrent-admin.p12 to import (Windows/macOS system store;
# Firefox keeps its own store and needs a separate import)
```

**CLI credentials**: `kurrent init` installed `~/.kurrent/admin.conf` through the one-shot exchange (entry URL, admin client certificate and trust anchor, 0600) and verified it with mTLS before reporting success. Discovery order is `--credentials` > `KURRENT_CREDENTIALS` > the record's `<state>/control-plane/state/admin.conf` > walking up from the current directory to the legacy `control_plane/state/admin.conf` → the repo layout next to the executable → the user-level snapshot `~/.kurrent/admin.conf`. The CLI works from any directory; after credential rotation re-run `kurrent init` or `kurrent credentials install` to refresh the snapshot.

How the exchange works: `<state>/control-plane/bootstrap` (0700, owned by the deploying user) is mounted as `/app/bootstrap`. `init` writes `request`; once PKI and `admin.conf` are ready, the control plane copies `admin.conf` and the server CA, admin certificate and key it references into `pki/`, rewrites the paths to be bundle-relative, writes them 0700/0600 with the exchange directory's owner, then writes `ready` (per-file SHA-256) last and removes `request`. The CLI verifies that paths stay inside the bundle, that hashes match, that certificate and key pair up, and that the server URL is usable, only then installs the snapshot; the bundle is consumed and deleted only after the mTLS self-test passes. The bundle is a copy — the authoritative file remains `<state>/control-plane/state/admin.conf` — and any failure leaves `consumed` unwritten so a rerun resumes from that step. The trust boundary is same-host filesystem ownership: the directory belongs to the deploying user and the container root can write it; credentials never appear in logs, command-line arguments or environment variables.

### 1.4 Firewall and host baseline

```bash
# Controller server
sudo ufw allow 67,69/udp && sudo ufw allow 80,443/tcp && sudo ufw reload

# Host precondition for the auth model: read-only checks of listeners,
# Docker exposure and credential-file permissions
sudo kurrent host check
```

Expected listeners, file permissions and Docker port confinement are in [Deployment Prerequisites](../reference/host-prerequisites.md#host-baseline) and the [Security Model](../architecture/security.md#deployment-checklist). Treat FAIL items as blockers (`--strict` also counts SKIP as failure): host root or a compromised nginx process is equivalent to a compromised management plane.

### 1.5 Issue the storage-node bootstrap credential

```bash
kurrent token create --agent-id 3f2a9c1d8b7e --cp-url https://192.168.1.10
# omitting --agent-id makes the control plane allocate a new 12-lowercase-hex identity
```

`--cp-url` must be the **HTTPS entry of the control plane as reachable from the storage node** (port 443 `/api/cp` reverse proxy — Agents enroll/renew through it; derived from the `--server` host otherwise). `--agent-id` is the node identity and accepts only 12 lowercase hex characters; the token is bound to it. When omitted the control plane allocates a new identity and returns it (an explicit value binds an existing identity, e.g. for recovery). Nicknames belong to the control-plane ledger and are set after join in the WebUI or with `kurrent agents edit`. The output contains a join command that can run directly on the storage node:

```
bootstrap token: <token> (expires ...; bound to node 3f2a9c1d8b7e; 0/1)

# Run on the bound node (--agent-id is written into storager/state/agent.id under the node state root):
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
- `diskDir` must be an absolute host path on a mounted data disk. join injects it as `KURRENT_DISK_DIR` when it starts the containers — no second copy is written to disk. Create the directory first if it does not exist.
- `nqnBase` is the single namespace for host and disk NQNs; keep it consistent across the cluster.

The node identity is not part of the declaration: the control plane allocates it at bootstrap-token issuance and join writes it to `storager/state/agent.id` under the node state root, which both containers mount read-only. The declaration therefore holds only parameters a human chooses, and `kurrent config validate --node storager/kurrent.yaml` works before join.

### 2.2 Join the cluster

Run the command from the token output inside the repository on the storage node:

```bash
kurrent join https://192.168.1.10 --token <token> --agent-id 3f2a9c1d8b7e
```

`kurrent join` performs four operations (idempotent and safe to re-run):

1. Validates `storager/kurrent.yaml`: creates it from the template when missing, merges it when present (hand edits are kept), and syncs `spec.controlPlane.url`.
2. Creates `storager/{state,bootstrap,pki,logs,certs}` under the node state root and persists the identity `state/agent.id` and the node-bound bootstrap credential `bootstrap/agent.token` (0600). The first run requires `--agent-id`; later runs may omit it, but once the state file exists a given `--agent-id` must match it. `nvmet-host.token` is derived by the agent's enroll — nothing to provide by hand.
3. Resolves the controller trust anchor and copies it to `certs/cp-ca.crt`: `--cp-ca` first, then the co-located controller's `control-plane/state/certs/server.crt`, then an existing node copy. When none exists, join fails and prints the command that copies it from the controller.
4. Injects `diskDir`, the node state root and the trust anchor as compose variables and starts/restarts the agent; on the nvmet backend it then restarts nvmet-host to consume the derived credential. The backend picks the orchestration directory (nvmet → `storager/nvmeof`, stgt/lio → `storager/iscsi`) and the compose project is pinned to `kurrent-storage`.

Before writing anything, join validates the merged declaration inside the agent image against the same schema, so unknown fields, non-absolute paths and invalid NQNs fail immediately rather than after container startup. The default `diskDir` (`storager_img`) sits inside the checkout, so moving the repository would move the disk images; join warns about this, and a fixed location needs an absolute path outside the repository in the declaration.

On split hosts, the first join supplies the controller `server.crt` with `--cp-ca`:

```bash
scp admin@192.168.1.10:<state>/control-plane/state/certs/server.crt /tmp/cp-ca.crt
kurrent join https://192.168.1.10 --token <token> --agent-id 3f2a9c1d8b7e --cp-ca /tmp/cp-ca.crt
```

The node state root also defaults to `$XDG_STATE_HOME/kurrent`; `--state-dir` or `KURRENT_STATE_DIR` selects another location such as a dedicated data disk.

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

`diskDir` holds two kinds of files:

- **Master images** are prepared once and carry a `_tpl_` marker in the filename (for example `_tpl_ubuntu2404.img`). They must be whole-disk images with a 4K-native GPT. After being placed in `diskDir`, the agent scans them into the master list; a master is only a clone source and is never exported to a device.
- **System disks** are cloned from a master by the controller per Worker, each with its own `os_tag`, disk NQN and authentication credential. Creation and first boot are covered in [First Delivery](./usage.md).

See [System Disk Customization](./system-disk.md) for building and validating a master image. A new storage node can join without any master image; before the first disk creation, at least one healthy node must hold the target master, because the placement policy only selects nodes that hold it.

## Step 3: Firmware and Device Admission

### 3.1 Install the firmware artifact

The platform repository does not contain firmware binaries. Device firmware is built by the companion repository [kurrent-firmware](https://github.com/dutyc/kurrent-firmware); use a release artifact matching the current upstream baseline, or build it per [Firmware Build and Deployment](../firmware/build-deploy.md).

Devices support x86_64 UEFI boot only; Legacy BIOS (CSM) is no longer supported. See [Deployment Prerequisites](../reference/host-prerequisites.md#host-roles) for the remaining device requirements.

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
eval "$(kurrent deployment show -o env)"   # state root and other interpolation variables
docker compose restart kurrent-dnsmasq
```

Mainline currently fixes the PXE first hop to `snponly.efi` and does not switch artifacts per device. Machines that need `pxe-uefi/ipxe.efi` cannot share the same PXE segment with the default first hop, so the first-hop artifact must be unified before deployment; see [Firmware Build and Deployment](../firmware/build-deploy.md#_2-select-artifacts).

Confirm firmware status and the six capabilities (HTTPS boot, NVS persistence, device key generation, public-key export, challenge signing, TOFU anchoring) in the WebUI **Boot Chain** page, or query the API from the controller repository with the admin certificate:

```bash
STATE=${KURRENT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/kurrent}
curl -sS \
  --cert "$STATE/control-plane/state/pki/users/admin/client.crt" \
  --key "$STATE/control-plane/state/pki/users/admin/client.key" \
  --cacert "$STATE/control-plane/state/certs/server.crt" \
  https://<controller-management-address>/api/cp/boot-assets | jq '.firmware'
```

### 3.2 First boot and admission

Open the registration window in WebUI → **Devices** first (TTL is bounded by `spec.registration.windowMaxTtlMinutes`, default 60 minutes; optional admission quota `max_devices`), then connect the bare-metal device to the PXE segment and enable UEFI network boot. The window must be open before the device reports; after it closes, reports only refresh the fingerprint and never admit the device.

```bash
# On the controller: watch the DHCP/boot flow
docker logs -f kurrent-dnsmasq 2>&1 | grep -E "DHCPACK|pxe|ipxe"
```

Expected chain: the device gets an address via DHCP → TFTP pulls iPXE and the `boot.ipxe` pointer → iPXE fetches `boot.ipxe.cfg` over HTTPS → `keygen` creates an ECDSA P-256 private key on first use (kept in EFI NVRAM) → `/devices/report` sends the fingerprint and public key → **the device appears under Devices and completes key claim**.

Close the registration window after the device is admitted. Once every device has claimed its key, enable **signature enforcement** on the Devices page: `/boot-vars` then serves only bound devices that pass challenge-response verification.

Admission only proves that the platform side and the registration path work; the device has no bootable system yet. A real boot takes three steps: a master image for the target distribution on a storage node → clone a system disk for the Worker and make it the default → cold boot to a login prompt. See [System Disk Customization](./system-disk.md) for the master image and [First Delivery](./usage.md) for the last two.

## Migration and repository moves

With runtime state decoupled from the repository, the checkout location only affects code, declarations and build artifacts:

- **Moving or renaming the checkout**: after the move, run `kurrent init --dir <new repository root>` in the new location. The command reuses the recorded state root and pinned projects, recreates the containers with `--force-recreate` (mount sources remain absolute), and updates the recorded repository root; ledgers, PKI and credentials stay unchanged.
- **Storage nodes**: run `kurrent join <cp-url> --dir <new repository root>` in the new location; the node state root, identity and credentials are unchanged.
- **Legacy layout migration** (early deployments whose state still lives in the repository): the first `kurrent init` detects `control_plane/state`, `storager/state` and similar legacy paths and refuses to continue, pointing at `--adopt-state`. The migration stops the legacy stack, copies and verifies every file into the state root, then starts the stack and completes the credential self-test; the legacy copies are removed only after all of that succeeds, and a failure leaves them intact. Ownership is checked for both the control-plane and co-located node legacy state before any stack is stopped; when files belong to another user (for example, created by Docker as root), the command lists every path to fix in a single `sudo chown` remediation and stops without copying anything.
- **Lost state root**: when the deployment record exists but the state root does not, `kurrent init` refuses to create an empty deployment; restore the state root from a backup, or remove the record and deploy again.

Backup, restore and the downtime requirements before migration are covered in [Operations](./operations.md#backup-and-recovery).
