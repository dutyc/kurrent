# Operations

This page addresses environments that have completed deployment and entered system delivery. It covers declaration changes, identity and credentials, device and node lifecycle, upgrades and rollback, backup and observability. For initial deployment, see [Deployment](./deployment.md); for cross-component fault isolation, see [Troubleshooting](./troubleshooting.md).

## Changing Configuration

### Control plane

```bash
vim control_plane/kurrent.yaml
kurrent config validate     # full validation before writing; nothing changes on failure
kurrent init                # apply and converge; roll back the previous declaration on failure
kurrent config status       # effective declaration, configHash, dnsmasq consistency, certificate drift
```

`configHash` is the canonical SHA-256 of the effective declaration. To confirm which declaration the runtime loaded, compare `configHash` from `config status` with the output of `config show`; do not rely on file timestamps.

Common changes:

| Change | Apply with | Notes |
| --- | --- | --- |
| `spec.networking` | `kurrent init` | Regenerates `dnsmasq.conf` and restarts dnsmasq |
| `spec.auth` / `spec.registration` / `spec.credentials` | `kurrent init` | New values are read on the next request or background cycle |
| `spec.pki` | `kurrent init` | CRL refresh period and token caps change accordingly |
| `spec.serverCert.san` / `days` | `kurrent init` | Reports drift by default; does not change the TOFU fingerprint |
| `spec.serverCert.rotateOnChange: true` | `kurrent init` | Reissues the certificate; enrolled devices must re-trust it |
| `spec.boot` | `kurrent init` | Affects menu timeout and default `/boot-vars` behaviour |

Do not edit `$STATE/control-plane/dnsmasq/dnsmasq.conf` by hand: it is derived from `spec.networking` and regenerated at control-plane startup. `$STATE` is the state root from the deployment record (default `~/.local/state/kurrent`); `kurrent deployment show` prints it.

### Storage node

```bash
vim storager/kurrent.yaml
kurrent config validate --node
kurrent join https://<controller-address>    # --token can be omitted when bootstrap/agent.token exists
```

`kurrent join` revalidates the declaration, injects `diskDir` and the PKI directories as compose variables, and restarts the agent (on the nvmet backend it also restarts nvmet-host). Changing `spec.agent.diskDir` changes the disk directory; changing `spec.agent.advertiseUrl` changes the address the control plane uses to reach the Agent; changing `spec.agent.backend` switches the compose directory (nvmet / iscsi). Confirm how existing disk files will be moved before changing directories.

The node identity lives in `<state>/storager/state/agent.id`; component certificates, bootstrap credentials and logs sit under `<state>/storager/`, so the whole node state root needs backup. A lost identity cannot be restored from the declaration: delete `agent.id`, issue a new token with `kurrent token create`, and re-run join with the new identity. The controller trust anchor is `<state>/storager/certs/cp-ca.crt`; after a server-certificate rotation, update that file (or pass `--cp-ca` to join) and restart the node components.

### Server certificate drift

`derived.serverCert.drift` in `config status` lists SAN or validity differences from the declaration. The default behaviour is **report only**: the certificate and the pinned TOFU fingerprint are unchanged. To rotate deliberately:

1. Confirm that every device can accept a new trust anchor (or is scheduled for reprovisioning).
2. Set `spec.serverCert.rotateOnChange: true` and run `kurrent init`.
3. The new fingerprint differs; enrolled devices reject the old certificate on the next HTTPS handshake and need their NVRAM fingerprint reprovisioned.
4. Set the flag back to `false` when done.

## Identity and Credentials

### Management entry points

```bash
kurrent token create --usage admin                 # admin token (24h default; --scope/--ttl)
kurrent token list                                 # unexpired token ledger
kurrent token revoke <token_id>                    # invalid immediately

kurrent sessions list                              # session ledger
kurrent sessions revoke <session_id>               # revoke one session
kurrent sessions revoke-all                        # sign out all sessions
```

Rotating the management certificate: note the current serial (`kurrent cert list`) → stop the control plane and delete `<state>/control-plane/state/pki/users/admin/client.crt` and `client.key` → start the control plane (it re-issues the certificate and rewrites `admin.conf`) → revoke the old serial with the new credential: `kurrent cert revoke admin --serial <old serial> --reason superseded` (effective on the next handshake) → run `kurrent credentials install` to refresh the `~/.kurrent` snapshot. `kurrent credentials show` reports the active `admin.conf`.

### User certificates

```bash
kurrent cert keygen alice --out ./certs            # generate the private key and CSR locally
kurrent cert issue alice --csr ./certs/alice.csr   # the user must exist in state/users.yml
kurrent cert list
kurrent cert revoke alice [--serial HEX]           # re-sign CRL + reload nginx + revoke sessions
```

User certificates are issued by the users CA; revocation takes effect on the next TLS handshake. The WebUI **Identity & Access** page exposes the same operations.

### Component certificates

Component channels (443 enroll, 4840 inbound, controller outbound, agent → nvmet-host over the UDS) perform application-level revocation checks on top of TLS chain validation.

```bash
kurrent cert components
# Write nodes CRL and delete the node's bootstrap token
kurrent cert revoke-component <agent_id>
```

- The control plane generates two component CRLs: `state/pki/nginx/nodes.crl` (nodes CA, for peer components) and `control.crl` (control CA, for the controller client); nginx separately uses `clients.crl` for management handshakes.
- Components fetch these with their own client cert from `/api/cp/enroll/crl` and refresh every 60 seconds by default (`KURRENT_COMPONENT_CRL_REFRESH_SECONDS`); controller outbound and agent → nvmet-host load `nodes.crl` on every connection and check `nextUpdate`.
- **Fail-closed**: a missing or stale CRL refuses the connection outright — no degraded allow. `nodes.crl` / `control.crl` carry a 30-day `nextUpdate` to tolerate a long controller outage.
- Revocation entry points: `DELETE /api/cp/pki/components/{agent_id}` (nodes domain) and `DELETE /api/cp/pki/control/{cn}` (control domain), both accepting `serial` to target a single certificate.
- **Upgrade order**: upgrade and restart the controller first (it generates the CRLs and opens the distribution endpoint), then rebuild the component containers. Older components that do not know `/enroll/crl` keep their previous chain-only behavior and do not refuse to start.
- Revocation takes effect at the component's next refresh after the controller writes the CRL (≤60 s), with no component restart.
- **Startup self-check**: a revoked certificate cannot complete the `/enroll/crl` mTLS handshake, so components fetch the public read-only `GET /crl/nodes.crl` (unauthenticated, GET/HEAD only) at startup, verify its signature against the local nodes CA, and compare the client / serving serials; a hit re-enrolls with the bootstrap credential (`KURRENT_PKI_SELF_CHECK=0` disables the check).
- **Management UI**: the WebUI **Identity & Access** page shows expiry and status across the users / nodes / control domains and can revoke user and node certificates; an abnormal node row also offers **Recover**.

Recovering a revoked node:

1. Controller: WebUI **Identity & Access** → **Storage nodes** → **Recover**, or
   `kurrent token create --agent-id <id> --cp-url https://<cp-host>`. The WebUI pre-fills
   the controller URL and shows a single-use token plus the join command after issuance
   (plaintext visible once).
2. Node: run the printed `kurrent join <cp-url> --token <token> --agent-id <id>`.
3. Startup self-check: the agent finds the local client / serving serials in the nodes CRL,
   re-enrolls with the new token, and overwrites the local certificates; join then waits
   for the derived credential and restarts nvmet-host automatically.

Verification:

```bash
# Includes component CRL distribution, public nodes CRL, control CA ledger,
# and human-cert 403 assertions
bash tests/deploy/probe_edge.sh
docker logs storager-agent 2>&1 | grep 'crl:'
docker exec kurrent-nvmet-host python /app/healthcheck.py; echo $?
```

`docker logs` should show initial fetch ok (revoked counts and next_update), and the health check returns 0.

### NVMe-oF credentials

```bash
kurrent workers credential show worker-01          # key hash prefix + per-Agent push status
kurrent workers credential set worker-01 --generate  # rotate without printing plaintext
kurrent workers credential revoke worker-01        # revoke: the next boot is refused
```

The nvmet backend issues the credential at disk creation; these commands are for explicit rotation and revocation. Failed pushes are retried by background reconciliation; `spec.credentials.reconcileIntervalSeconds` controls the period and `rotationHours` controls scheduled rotation (default 24 hours; revocation is the primary remedy).

## Device and Worker Lifecycle

- **Register a device** — WebUI → Devices → open registration window; only devices with a valid TPM AK are admitted while the window is open.
- **Bind** — Devices / Workers page; one-to-one; an already-bound device needs an explicit rebind.
- **Rebind (cold migration)** — Devices page with `force`; new binding is written before the old one is cleared; system disks stay on the original Worker.
- **Unbind** — Devices page; the device returns to the pool; the Worker keeps its system disks.
- **Drain a storage node** — Agents page → disable; no new disks; existing disks keep serving.
- **Delete a Worker** — Workers page; unbinds devices, removes credential entries and push records; disk files handled by option.
- **Purge revoked devices** — Devices page / `DELETE /devices/revoked`; removes `revoked` entries only.

Before rebinding, confirm that the target Worker's system disks already have usable credentials, and that the original device is powered off or isolated from the data plane. The control plane does not validate target credentials during a rebind and does not fence the original device; Kurrent provides no live migration or data-plane fencing, so the same system disk must not be attached to two devices at once.

## Upgrade and Rollback

1. Back up declarations, the state root (`$STATE`) and `~/.kurrent/` (next section).
2. After updating the code, upgrade the control plane first: rebuild the image and run `kurrent init`; confirm `/healthz` and `config status`.
3. Rebuild storage-node components with `kurrent join`; confirm `agents list` shows `ok`.
4. Update WebUI static assets last with `(cd webui/app && npm ci && npm run build)` (`dist` is not committed); update firmware separately if changed.
5. Run the deployment probe: `bash tests/deploy/probe_edge.sh`.

Control-plane migrations run at startup and are idempotent, but a backup is still required before upgrading. To roll back, rebuild with the previous code and rerun `kurrent init`; declarations and the state root remain. If the newer version has written fields unknown to the older one, restore the backup before rolling back.

### Repository moves

Because the state root is decoupled from the repository, moving or renaming the checkout needs no state migration:

1. Stop anything that may drive compose from either location; confirm no `kurrent init` / `kurrent join` is running.
2. Move or rename the repository directory.
3. Run `kurrent init --dir <new repository root>` in the new location; it reuses the recorded state root and pinned projects, recreates the containers with `--force-recreate` and updates `repoRoot` in the record.
4. Run `kurrent config status` and `kurrent host check` to confirm services and mount sources.

Storage nodes follow the same steps with `kurrent join <cp-url> --dir <new repository root>`. Do not hand-edit `~/.kurrent/deployment.yaml`: repository root, state root and project names must agree, or the next init misidentifies the deployment.

## Backup and Recovery

Back up:

| Path | Content | Permissions |
| --- | --- | --- |
| `control_plane/kurrent.yaml` | Control-plane declaration (in the repository) | 644 |
| `storager/kurrent.yaml` | Node declaration (in the repository) | 644 |
| `$STATE/control-plane/state/` | Ledgers, credentials, audit, sessions, certificate records, PKI and TOFU certificate | Directories 700, files 600 |
| `$STATE/control-plane/config/agents.yml` | Agent ledger | 644 |
| `$STATE/control-plane/dnsmasq/` | Rendered configuration and leases (regenerable) | 644 |
| `$STATE/storager/` | Node identity, bootstrap credentials, component PKI, logs and trust anchor | Directories 700, private keys 600 |
| `~/.kurrent/deployment.yaml` | Deployment record (repository root, state root, projects) | 600 |
| `~/.kurrent/admin.conf` and `pki/` | Management credential snapshot (rebuildable with `kurrent init`) | 600 |

The state root contains private keys and credential plaintext; backups must be encrypted and access-controlled. `control-plane/dnsmasq/` and the credentials in `~/.kurrent/` are reproducible, so a complete backup centers on `control-plane/state/` and the node state root. Recovery:

1. Stop the control plane and storage-node components.
2. Restore declarations and the state root to the path in the deployment record, preserving permissions; restore the record as well.
3. Run `kurrent init`; confirm `configHash` matches the backup.
4. Rerun `kurrent join` on storage nodes as needed and confirm `agents list`.
5. If the PKI under `state/` or `certs/` is lost, all components must re-enroll and devices must re-trust the server certificate; this is not a lossless recovery.

## Observability

- **Control-plane health** — `GET /healthz` (via 443); returns 503 when a startup artifact fails.
- **Declaration status** — `kurrent config status`; effective declaration, configHash, dnsmasq consistency, certificate drift, startup errors.
- **Agent health** — `kurrent agents list` / WebUI Agents; control-plane to Agent connectivity.
- **Boot-chain status** — WebUI **Boot Chain** / `GET /boot-assets`; firmware capabilities, script chain and menu completeness.
- **Operation audit** — WebUI Operations / `kurrent ops list`; registration, binding, disk and credential history.
- **Request audit** — `$STATE/control-plane/state/audit.jsonl`; principal, issuer, serial, route, decision and alarms.
- **Alarm delivery** — `KURRENT_CP_ALARM_WEBHOOK`; POSTs `event=alarm` events to an external endpoint.

When the audit write fails the request returns 503 (fail-closed). Alarm delivery uses a background queue and stays outside the request path.

## Security Baseline

```bash
sudo kurrent host check --role all    # co-located hosts; use control / storage when split
```

- The control-plane host exposes only 22, 53/67/69 and 80/443; storage nodes additionally expose 4840/4421.
- Output is `ok` / `warn` / `fail` / `skip`; exit 0 passes, 1 has a FAIL, 2 is a usage error. Firewall checks report `skip` without root, and `--strict` counts `skip` as failure.
- The nvmet-host management plane uses a shared-volume UDS with no TCP listener.
- The control-plane container does not mount `docker.sock`.
- CA private keys, `users.yml`, `sessions.yml`, `admin.conf` and `audit.jsonl` should be 600.
- Docker-published ports bypass ufw; enforce inbound restrictions in the `DOCKER-USER` chain.
- Isolate the PXE network as described in [Deployment](./deployment.md), with DHCP snooping, Dynamic ARP Inspection and port security enabled.
