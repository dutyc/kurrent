# Architecture

![Architecture](../../assets/architecture.svg)

## Control Plane / Data Plane Separation

The system is split along one clean line: the **control plane** handles identity, scheduling and configuration over HTTPS; the **data plane** carries NVMe-oF block I/O and never touches the control plane. A Worker's disk traffic flows directly between the device and its Storager host (TCP `4421`, TLS 1.3), so a control-plane outage degrades provisioning but never in-flight I/O.

## The Three Roles

### Controller

The brain. A containerized node running:

- the **Control Plane HTTP service** (container `:8080`, host `127.0.0.1:4839` only) — device ledger & pool, device↔Worker binding, worker lifecycle, storage scheduling (system disk create / delete), boot-variable projection, audit log
- **nginx** (`:443`, the only external entry) — WebUI SPA, management API (`/api/cp/`), boot-chain bare paths (`/devices/…`, `/boot-vars`), iPXE assets (`/file/`) and menus (`/tftp/`); `:80` only redirects with 301
- **dnsmasq** (host network) — DHCP / TFTP and boot-file distribution
- the **Web UI** — one client of the REST API, not a separate system; it authenticates with a session cookie
- the **CLI** (`kurrent`) — `config print` emits the declaration → `init` converges the control plane → `token create` issues bootstrap credentials → `join` brings a node in

State is persisted as plain files (YAML / JSONL) under `control_plane/state/` and `control_plane/config/`; no database has been introduced so far. The declarative configuration in `control_plane/kurrent.yaml` is authoritative and its artifacts (such as `dnsmasq.conf`) are rendered from it at control-plane startup. Every capability is exposed as REST — the Web UI and the CLI are just clients of it.

### Storager

Block storage. Each node runs two components:

- **API Agent** (`:4840`, mTLS) — disk-file lifecycle (master scan, reflink clone, delete) and the storage-scheduling client, driving the local backend over HTTP
- **nvmet-host** (`:4841`, host-network host service, mTLS) — translates control-plane commands into kernel configfs operations (subsystem / namespace / port / `allowed_hosts` and `dhchap_key`) and injects TLS pre-shared keys into the kernel's global `.nvme` keyring; the data-plane port is **`4421` (TLS 1.3), shared by the firmware and kernel boot segments**

The backend of choice is **nvmet**; the iSCSI backends (stgt / lio) keep their code but have no live boot path on the main branch (the historical implementation lives in the `iscsi-classic` branch). Backend differences are encapsulated inside the Agent: the control plane only ever speaks HTTP, and the data-plane address and Host NQN are delivered to devices through boot variables.

### Devices

The stateless compute side. A physical device holds no persistent state: on PXE boot it reports its fingerprint (MAC / UUID / SMBIOS / CPU / memory / NIC — 11 fields) and is admitted into the **device pool**; after being bound to a Worker (its compute identity) and given a system disk, it boots with NVMe-oF as its root device (`nvme://<storager>:4421/<disk NQN>?tls_psk=…`). Block I/O travels the data plane, never the control plane.

## Three Entities: Device, Worker, System Disk

The role layer maps onto an entity layer of three objects:

| Entity | Identity | Notes |
|---|---|---|
| **Device** | MAC (unique) | Physical machine, ledger entry; the authoritative side of the binding |
| **Worker** | `worker_id` (== hostname) | Compute identity; a Device bound to a Worker becomes that Worker's booting node |
| **System Disk** | **NQN** (`<base>:<hostname>.<os>.<os_tag>`) | Storage volume (master-clone or empty); multiple disks per Worker |

The disk is identified by its **NQN**; the IQN is derived from it (consumed by the iSCSI backend and installers). `os_tag` is 12 random hex digits — at once the disk-filename suffix and the last NQN segment.

The binding relationship is authoritative on the **device side** (`bound_worker_id`); the Worker only holds a projection. Disks are decoupled from machines: unbinding or rebinding returns the device to the pool while its system disks stay on the Worker.

- Device lifecycle: `pooled` (in pool, awaiting binding) → `bound` → `revoked`
- Rebinding with `force=true` is atomic: new binding persisted, old binding cleared, ledger snapshot restored on failure
- One Worker can carry multiple system disks (different systems) and switch the default boot system online
- `readiness` = bound device + system disk (`ready` / `partial` / `idle`)

## Boot Chain

1. Device powers on → DHCP (dnsmasq) → TFTP downloads iPXE → iPXE boots
2. **Device identity**: `keygen` creates an ECDSA P-256 private key once (persisted in EFI NVRAM, never leaves the machine) → `pubkey` → `/devices/report` sends the fingerprint and public key; an unknown MAC is admitted **only while the registration window is open and a valid public key is present**
3. **Challenge-response**: `/devices/challenge` returns a one-shot nonce → `sign <nonce><mac>` → `/boot-vars?…&sig=`
4. **`/boot-vars` admission**: identity match (MAC resolved through the device ledger; hostname is a fallback when no MAC is supplied or the ledger has no hit) → **binding is authentication** (the request MAC must belong to the matched Worker's bound device) → signature verification (with enforcement on, no key / no signature / bad signature are all rejected) → credentials injected only when the key store has an entry (**on nvmet nodes the key is issued automatically when a system disk is created**); only then are root-path, NQN and key projected
5. A pooled-but-unbound device loops on `reboot` until bound
6. **Data-plane connect**: root-path is `nvme://<storager>:4421/<disk NQN>?secret=…&tls_psk=…&tls_trsvcid=4421`, authenticated with **DH-HMAC-CHAP** and encrypted with **TLS 1.3**; the key follows the Worker and lives only for this boot session — it is never written to disk
7. **Hand-off to the OS**: at sanboot the firmware installs the **NBCT** credentials table (a custom ACPI table, registered iBFT-style, one record per session: traddr / trsvcid / nqn / hostnqn / secret, plus the TLS PSK and TLS port when the control plane provisioned them); the initramfs `nbft-connect` consumes **NBFT** (network and subsystem discovery) and **NBCT** (credentials and hostnqn identity relay) to reconnect — with `--tls` on the TLS port, once a PSK is available — and mount the root filesystem
8. The system boots to a login prompt — **not a single byte of machine identity is written into the disk**

> Status: NBCT is produced by the firmware (patch `0009`); **iPXE-side NBFT generation is still pending** — the verified end-to-end chain (QEMU + nvmet, six rings, authenticated) injects an NBFT table to emulate the "firmware pre-generated" stage.

## Security Boundary

Two orthogonal layers of trust.

**① Management-plane authentication** — three credential types tried in order, all producing one `Identity{name, groups, method}`:

- mTLS client certificate (nginx validates the chain and passes the DN through; CN = user, O = group)
- management token (Bearer; only a sha256 digest is stored, valid within its TTL)
- session cookie (HttpOnly signed session, 7-day sliding renewal, browser sign-in state)

Authorization: read requests only need to be authenticated; write requests require the `admin` group. Credentials are self-bootstrapped: at startup the control plane signs an admin certificate with the internal CA and renders `admin.conf`.

**Trust premise**: the `X-Client-Cert-DN` header is only trusted from nginx, so **443 is the only external entry and the control-plane port binds `127.0.0.1` only** — remove either one and an administrator identity can be forged.

**② Device identity trust root** — device authentication along the boot chain:

- the private key is generated and persisted inside device NVRAM (ECDSA P-256); fingerprint reporting is unauthenticated but only feeds the pool and grants no privilege
- challenge-response signing (one-shot nonce, bound to the MAC) proves the machine is what it claims to be
- TOFU single anchor: the self-signed server certificate's fingerprint is pinned in device NVRAM on the first handshake; any later validation failure is fatal
- **binding is authentication**: `/boot-vars` validates that the requesting MAC belongs to the matched Worker's bound device, preventing another device from stealing a Worker's boot identity

**Authentication ≠ encryption — two separate layers**: DH-HMAC-CHAP answers "who may connect" and does not by itself protect data-plane content. Content protection is **TLS 1.3 on the data-plane port** (`4421`, `tsas=tls1.3`, `treq=required`), which both boot segments share: the firmware performs the TLS 1.3 PSK handshake itself, and the kernel/initramfs reconnects on the same port with `--tls` after the NBCT table hands it the PSK. The pre-shared key is a **pure function of the Worker's DHHC-1 key** (HMAC-SHA256, domain-separated): it is never stored as a separate field, so rotating or revoking the key rotates or revokes the PSK atomically. On the storage node the control plane's push lands it in the kernel's global `.nvme` keyring. Encryption is the default with **no plaintext fallback**: there is no second port to fall back to, so when the data port is not ready or the key is missing, the control plane withholds the boot variables (recording an audit event) and `menu.ipxe` drops to `failed` rather than assembling a plaintext URI.

## Protocol Evolution

The data plane has moved from iSCSI to **NVMe-oF (NVMe over TCP)**: the firmware main branch carries the full patch set (`0001`–`0010`, including the nvmetcp driver, TOFU fingerprinting, the device-key commands, NBCT and the firmware-side TLS 1.3 PSK client), and cold boot has passed whole-machine acceptance on several distributions.

The semantics — stateless compute, disk-machine decoupling, one identity chain from MAC to boot — do not depend on the protocol: Storager backends are already abstracted behind the Agent, so swapping the transport does not touch the architecture. iSCSI has no live boot path on the main branch (its historical implementation is kept in the `iscsi-classic` branch); cold boot over NVMe/TCP TLS — the firmware segment (iPXE `sanboot` performing the TLS 1.3 PSK handshake itself) and the kernel segment (`nvme connect --tls`) — has passed end-to-end acceptance for both, and what remains is aligning the NBCT table with distribution/upstream ecosystems.
