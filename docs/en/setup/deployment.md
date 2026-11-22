# Deployment

> From blank hosts to a device cold-booting to a login prompt. The deployment units are a **controller server + storage nodes**, separate or co-located; one Agent per storage server.

**Done when** the cold boot reaches a login prompt.

References: [Configuration](../reference/configuration.md) (declarations), [CLI](../reference/cli.md) (commands), [Architecture](../concepts/architecture/index.md) (ports), [Security Model](../concepts/architecture/security.md) (trust boundaries).

## Preparation

- Hosts and devices meet the [deployment prerequisites](../setup/host-prerequisites.md): x86_64 UEFI, TPM 2.0, Secure Boot, SNP NIC;
- the PXE segment is dedicated and the controller is its only DHCP/TFTP/4443 provider;
- the repository, `kurrent` CLI and WebUI build dependencies are ready (installation in the [CLI Reference](../reference/cli.md#install-and-credentials));
- both repositories use `main`; devices without a TPM use `legacy/no-tpm` on both sides (see [Repositories and Branches](../reference/branches.md)).

Run the commands below from the main repository root; to fetch it:

```bash
git clone https://github.com/dutyc/kurrent && cd kurrent
```

A co-located deployment shares one state root; it defaults to `~/.local/state/kurrent`, and backup is in [Operations](../tasks/operations.md#backup-and-restore).

### Network

| Path | Content | Ports |
| --- | --- | --- |
| Management | Browsers, storage nodes | 443 |
| Boot | DHCP, TFTP, boot HTTPS (`spec.networking.interface` binds this side) | 67, 69, 4443 |
| Data | Devices to storage nodes | 4421 |

Example: PXE `192.168.80.0/24`, controller `192.168.80.1`, pool `.50–.100`, gateway `.2`, DNS `223.5.5.5`; management `192.168.1.10`, storage `192.168.1.20`.

The full port table is in [Architecture](../concepts/architecture/index.md#ports).

### Host dependencies

Both hosts: Docker, Compose v2, `git`, `curl`, `jq` and `openssl`, plus Go to build the CLI (1.27+, or allow the `toolchain` download); the controller additionally needs Node.js 22.23.4+, 24.22.1+ or 26.12.1+ and npm.

```bash
sudo apt install -y git curl jq openssl
docker --version && docker compose version && go version && node --version && npm --version
```

Docker/Compose and Node.js are not covered by the apt command above; install them beforehand through the distribution's method.

Storage nodes prepare the data-plane host and mount a data disk (`mkfs` targets an empty disk only):

```bash
sudo bash storager/nvmeof/host-prepare.sh
kurrent host check --role storage

sudo mkfs.btrfs /dev/sdb
sudo mkdir -p /pool1 && sudo mount /dev/sdb /pool1
```

Persist the mount in `/etc/fstab` (UUID) or a systemd mount, or `diskDir` lands on the root filesystem.

## Step 1: Controller Server

### 1.1 Declare

```bash
kurrent config print init-defaults > control_plane/kurrent.yaml
```

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

- `interface`: the PXE-facing interface, not the management NIC.
- `dhcpRange`: one `start,end` string.

To open the WebUI by management IP, add the SAN first:

```yaml
  serverCert:
    san: "IP:192.168.1.10,IP:192.168.80.1,IP:127.0.0.1,DNS:localhost"
```

```bash
kurrent config validate && kurrent config show
```

### 1.2 Start

```bash
(cd webui/app && npm ci && npm run build && test -f dist/index.html)
kurrent init
```

A failure rolls back to the previous declaration; a failed credential exchange resumes when re-run. The full flow is in the [CLI Reference](../reference/cli.md#control-plane-initialization).

### 1.3 Verify

```bash
STATE=${KURRENT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/kurrent}
curl -k https://127.0.0.1/healthz
curl -ks https://127.0.0.1/ | grep -q '<div id="root">'
kurrent config status
```

Open `https://<controller-management-address>/` and sign in with a token from `kurrent token create --usage admin`; browser mTLS import is in [Operations](../tasks/operations.md#management-entry-points).

The CLI credential is installed by `kurrent init`; after rotation re-run `kurrent init` or `kurrent credentials install`.

Later steps use:

```bash
STATE=${KURRENT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/kurrent}
P=$STATE/control-plane/state
api() { curl -sS --cert "$P/pki/users/admin/client.crt" --key "$P/pki/users/admin/client.key" \
  --cacert "$STATE/control-plane/state/certs/server.crt" "https://<controller-management-address>/api/cp$1" "${@:2}"; }
```

### 1.4 Firewall and host baseline

```bash
sudo ufw allow 67,69/udp && sudo ufw allow 80,443/tcp && sudo ufw reload
sudo ufw allow 53 && sudo ufw reload    # only when spec.networking.dns points at the controller
sudo kurrent host check
```

4443 is a Docker-published port and bypasses ufw; restrict it to the boot segment at the network layer per the [Security Checklist](#security-checklist).

### 1.5 Issue the node token

```bash
kurrent token create --agent-id 3f2a9c1d8b7e --cp-url https://192.168.1.10
```

- `--agent-id`: 12 lowercase hex; allocated by the control plane when omitted.
- The token is single-use with a 60-minute TTL; use `--uses N` for bulk onboarding.

The nvmet-host credential is derived at enroll. The output is the join command for the next step.

## Step 2: Storage Node

### 2.1 Declare

```bash
kurrent config print node-defaults > storager/kurrent.yaml
```

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

- `advertiseUrl`: controller → Agent 4840 address; must be the storage management address on split hosts.
- `diskDir`: absolute path on a mounted data disk.
- `nqnBase`: consistent across the cluster.

Co-located deployment (controller and storage node on one host): the controller runs in a container, so use `https://host.docker.internal:4840` for `advertiseUrl` (compose already maps it to the host); set `storager_ip` on the WebUI Agents detail page to the PXE-side address (for example `192.168.80.1`) so devices reach 4421; run the host baseline with `kurrent host check --role all`.

The identity is not in the declaration; join writes it to `storager/state/agent.id`.

### 2.2 Join

```bash
kurrent join https://192.168.1.10 --token <token> --agent-id 3f2a9c1d8b7e
```

Split hosts supply the controller trust anchor on the first join. Replace `<state>` with the controller's real state root (default `~/.local/state/kurrent`) and use an absolute remote path:

```bash
scp admin@192.168.1.10:/home/admin/.local/state/kurrent/control-plane/state/certs/server.crt /tmp/cp-ca.crt
kurrent join https://192.168.1.10 --token <token> --agent-id 3f2a9c1d8b7e --cp-ca /tmp/cp-ca.crt
```

Validation failures write nothing; reruns are idempotent. The flow is in the [CLI Reference](../reference/cli.md#node-join).

### 2.3 Verify

```bash
docker logs storager-agent 2>&1 | grep pki
docker exec kurrent-nvmet-host python /app/healthcheck.py; echo $?
```

Expect `pki: client cert ok` and a health check exit of 0. On the controller:

```bash
kurrent agents list    # HEALTH=ok
```

`advertiseUrl` (controller → 4840) and `storager_ip` (devices → 4421) are distinct; set the latter in the WebUI Agent detail when the data plane uses a separate segment.

Firewall (storage node):

```bash
sudo ufw allow 4840,4421/tcp && sudo ufw reload
```

## Step 3: Firmware and Device Admission

Before starting, generate the offline signing key and import the site signing certificate plus the Microsoft UEFI CA into the target's `db` as described in [Secure Boot](../firmware/secure-boot.md#provisioning-the-target-trust-store), then verify with `tools/secureboot/check-host.sh`. The boot CA is created by `kurrent init` (`<state-root>/control-plane/state/pki/ca/boot/ca.crt`).

### 3.1 Firmware

Build the signed first hop in the firmware repository, then place the artifacts in the platform repository's TFTP root:

```bash
git clone https://github.com/dutyc/kurrent-firmware && cd kurrent-firmware
```

```bash
# Firmware repository: the boot CA comes from the controller state root
SB_KEY=/path/to/artifact-signing.key SB_CERT=/path/to/artifact-signing.crt \
BOOT_CA_CERT=<state-root>/control-plane/state/pki/ca/boot/ca.crt \
BOOT_HTTPS_PORT=4443 ./build/build-sb.sh

# Platform repository: artifacts feed /boot-assets and serve as the TFTP first hop
sudo install -d -m 0755 /path/to/kurrent/tftp/secureboot
sudo install -m 0644 dist/secureboot/snponly-sb.efi dist/secureboot/manifest.json /path/to/kurrent/tftp/secureboot/
sudo install -m 0644 /path/to/kurrent/tftp/secureboot/snponly-sb.efi /path/to/kurrent/tftp/snponly.efi
cd /path/to/kurrent
eval "$(kurrent deployment show -o env)"
docker compose restart kurrent-dnsmasq
```

The first hop is fixed to `snponly.efi`; restart dnsmasq after replacing it. The image embeds the boot CA and the 4443 port, so changing either requires a rebuild; the controller address comes from DHCP by default. Full build and provisioning are in [Secure Boot](../firmware/secure-boot.md).

```bash
api /boot-assets | jq '{firmware, secureboot}'   # secureboot.status=ok
```

### 3.2 Admit

WebUI → **Devices** → open the registration window, then connect the device to the PXE segment, enable UEFI network boot and Secure Boot; or:

```bash
api /settings/registration-window -X POST -H 'Content-Type: application/json' \
  -d '{"ttl_minutes": 30, "max_devices": 20}'
docker logs -f kurrent-dnsmasq 2>&1 | grep -E "DHCPACK|pxe|ipxe"
```

Chain: DHCP → TFTP first hop → report and nonce over 4443 → TPM creates the AK → `/devices/report` admits the device and claims the AK; later boots require a PCR quote.

```bash
api /settings/registration-window -X DELETE
```

Disable enforcement only for migration or troubleshooting: `api /settings/enforcement -X PUT -H 'Content-Type: application/json' -d '{"enabled": false}'`, then restore it.

## Step 4: System Disks and the First Boot

Run these on the controller with the `api` helper from 1.3; every step also works in the WebUI.

### 4.1 Master image

The master must be a whole-disk image (GPT, ESP and root partition) with `_tpl_` in the filename, placed in the storage node's `diskDir`; partition images, archives and installation media cannot be used directly. Building it is in [System Disk Customization](../tasks/system-disk.md).

```bash
# Build host: convert qcow2 to a whole-disk raw image (skip if already raw)
qemu-img convert -O raw master.qcow2 _tpl_ubuntu2404.img
scp _tpl_ubuntu2404.img admin@192.168.1.20:/pool1/storager_img/
```

```bash
api /masters | jq '.agents[] | {agent, masters: [.masters[] | {name, gpt}]}'
```

`gpt` must be `4k`, otherwise boot fails with `0x7f22208e`.

### 4.2 Bind a Worker

WebUI Devices page, or:

```bash
api /workers -X POST -H 'Content-Type: application/json' \
  -d '{"worker_id":"worker-01","hostname":"worker-01","mac":"00:0c:29:b9:8b:2d"}'
```

### 4.3 Create a system disk

```bash
api /workers/worker-01/luns/disk -X POST -H 'Content-Type: application/json' \
  -d '{"type":"master","os":"ubuntu","os_version":"24.04","name":"_tpl_ubuntu2404.img"}'
```

Placement is decided by the control plane; the response carries the `os_tag` and disk NQN; the first disk becomes the default.

### 4.4 Switch the default disk (optional)

The first disk already becomes the default; pick another only when a Worker has multiple disks:

```bash
api /workers/worker-01/default-disk -X PUT -H 'Content-Type: application/json' \
  -d '{"disk":"<os_tag>"}'
```

Without a default disk `/boot-vars` returns `menu-default=reboot`.

### 4.5 Cold-boot acceptance

```text
DHCP → iPXE → /boot-vars → NVMe/TCP TLS 1.3 → GRUB → kernel → initramfs (NBFT/NBCT) → rootfs → login
```

For an empty menu, a data-plane connection failure, an authentication failure or `0x7f22208e`, see [Troubleshooting](../tasks/troubleshooting.md); for `0x7f22208e` check that `gpt` is `4k` in `/masters` first.

## Security Checklist

| Item | Requirement |
| --- | --- |
| Network and switching | Dedicated PXE segment with the control plane as the only DHCP/TFTP provider; TFTP root mounts only `snponly.efi` and `boot.ipxe`; switches enable DHCP snooping, DAI, RA guard and port security |
| Exposed ports | Control plane 22, 53/67/69, 80/443/4443 (4443 boot segment only); storage nodes 4840/4421 |
| Host baseline | `sudo kurrent host check --strict` with no FAIL or SKIP; CA keys, `admin.conf`, credentials and audit files at 600 |
| Registration and attestation | Registration window opened on demand with a quota and closed afterwards; TPM attestation on by default; devices without a TPM are not admitted |
| Secure Boot and artifacts | Site signing certificate and the Microsoft UEFI CA in `db`; artifacts from a `build-sb.sh` signed build, with `secureboot.status=ok` in `/boot-assets` |
| Certificates and audit | User certificates issued on demand and revoked on departure or compromise; CRL refresh, credential pushes and audit alarms reviewed regularly |
| Backup and firmware | Encrypted backups with tested recovery; firmware artifacts verified with `SHA256SUMS` |
