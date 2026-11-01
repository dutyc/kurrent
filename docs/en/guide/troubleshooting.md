# Troubleshooting

This page is organized by symptom: first identify the boundary where the failure occurs, then inspect the responsible component. Task pages keep their local failure tables; this page covers cross-component isolation, evidence collection, and recovery paths.

## Locate the failure boundary

- **Browser or CLI cannot sign in** — management plane 443 / nginx / control plane; `curl -k https://<control-plane-ip>/healthz`; nginx logs.
- **Signed in, but an Agent is not `ok`** — control plane → Agent 4840; `kurrent agents list`; node `spec.agent.advertiseUrl`.
- **Device gets no DHCP response** — dnsmasq / PXE segment; device segment, `interface=`, firewall 67/69.
- **DHCP works but TFTP download fails** — TFTP root / 69; TFTP root files and permissions.
- **iPXE stalls at HTTPS** — 443 / certificate / TOFU; server certificate; NVRAM fingerprint.
- **Device not pooled** — registration window / public key / revocation; window state on Devices; device report.
- **Empty menu or boot loop** — binding / system disk / default disk; Worker details; `/boot-vars` projection.
- **Data plane fails after the menu** — 4421 / tlshd / credentials; `host-prepare.sh --check`; credential push state.
- **Authentication fails after the system boots** — credential rotation / reconciliation; `kurrent workers credential show`.

## Control plane not ready

```bash
docker logs kurrent-control-plane
kurrent config status
curl -k https://<control-plane-ip>/healthz
```

- `/healthz` returns 503: a startup-derived artifact failed; `config status` lists the startup error. Fix the declaration and rerun `kurrent init`.
- `init` exits non-zero and reports a rollback: the new declaration failed startup checks. Rollback restores only the declaration file; use `config status` for derived state such as certificates.
- `configHash` differs from `config show`: the runtime has not loaded the current declaration; rerun `kurrent init`.

## Agent not ok

```bash
kurrent agents list
docker logs storager-agent
docker logs kurrent-nvmet-host
```

| Check | Meaning |
| --- | --- |
| Can the control plane reach `spec.agent.advertiseUrl` | Defaults to `https://<node-ip>:4840`; check firewall and address declaration |
| Does the Agent restart repeatedly | Usually the control plane on 443 is unreachable, or `bootstrap/agent.token` is missing |
| Is the certificate revoked | A failed startup self-check blocks service; run `join` again with a new bootstrap token |

Recovering a revoked node:

```bash
kurrent token create --agent-id <agent_id>
# On the storage node
kurrent join https://<control-plane-url> --token <token> --agent-id <agent_id>
```

Old certificates do not need manual deletion; the Agent startup self-check re-enrolls.

## Device not pooled

1. Confirm the registration window is open on the Devices page and has not exceeded its TTL or device allowance.
2. Confirm the report carries a valid public key: first boot must run `keygen` and `pubkey`.
3. Confirm the device is not revoked; a `revoked` entry does not recover through a new report.
4. Check control plane logs and audit for `/devices/report`.

After the window closes, reports update fingerprints only and do not pool devices. There is no registration path outside the window.

## Boot refused

| Check | Entry |
| --- | --- |
| Is the device bound to a Worker | Devices page; the device ledger is authoritative |
| Does the Worker have a system disk and a default disk | Worker details; without a default disk the control plane returns `menu-default=reboot` |
| Is signature enforcement enabled | Settings page; once enabled, only bound devices passing challenge-response are accepted |
| Does `/boot-vars` return complete connection data | `GET /boot-vars`; check `disk-nqn`, `storager-ip`, port, and PSK |
| Does the NVRAM fingerprint match the control plane certificate | Re-provision `server-fingerprint` after certificate rotation |

## Data plane connection failure

```bash
bash storager/nvmeof/host-prepare.sh --check
docker logs kurrent-nvmet-host
ss -tlnp | grep 4421
```

- The host kernel needs `CONFIG_NVME_TCP_TLS`, `CONFIG_NVME_TARGET_TCP_TLS`, and `CONFIG_NVME_KEYRING`, with `tlshd >= 1.5.0` running.
- `/capabilities` must report `port.ready=true`; if `4421` is not listening, check the nvmet-host port-creation log.
- When boot is refused without a plaintext fallback, check whether credentials reached the Agent that holds the disk.

## Credentials not delivered

```bash
kurrent workers credential show worker-01
```

The output includes the key hash prefix and per-Agent push state. The control plane retries failed pushes on a reconciliation interval; check `spec.credentials.reconcileIntervalSeconds`, Agent connectivity, and whether the node holds the disk.

Plaintext should exist only in the 0600 state files on the control plane and Agent, in configfs on the storage node, and in the kernel `.nvme` keyring. Use hash prefixes and push state for diagnosis; do not print keys.

## Certificate revoked

```bash
kurrent cert components
kurrent cert list
```

- User certificates: `kurrent cert revoke <name> [--serial HEX]`; effective on the next TLS handshake.
- Component certificates: `kurrent cert revoke-component <agent_id>`; writes the nodes CRL and removes the node bootstrap token.
- Recover a node by issuing a new token and running `join` again, as described under "Agent not ok".

## Firmware identity and TOFU

- **`keygen: device key already exists`** — a key already exists; normal idempotent path; clear NVRAM only for an intentional reset.
- **`pubkey: no device key` / `sign: no device key`** — no key generated, or NVRAM was cleared; run `keygen` first.
- **HTTPS download rejected** — server certificate does not match the pinned fingerprint; confirm whether the certificate rotated; clear `server-fingerprint` after rotation.
- **A second HTTPS host is rejected** — TOFU is a global single anchor; consolidate HTTPS endpoints and certificates; use TFTP/HTTP for other traffic.
- **TLS handshake fails** — server TLS is above 1.2 or uses incompatible suites; limit the server to TLS 1.2.
- **`${pubkey}` / `${sig}` is empty** — the command failed or the setting was not written; check command output; both are valid only for the current boot.
- **The key remains after a bare `clear device-key`** — the `nvo/` prefix was missing; use `clear nvo/device-key` or `nvclear`.

## Collect evidence

| Evidence | Location |
| --- | --- |
| Control plane startup and derived artifacts | `docker logs kurrent-control-plane`; `kurrent config status` |
| Management request decisions | `control_plane/state/audit.jsonl` |
| Operation history | WebUI Operations / `kurrent ops list` |
| Agent and data plane | `docker logs storager-agent`, `docker logs kurrent-nvmet-host` |
| Boot chain status | WebUI "Boot chain" / `GET /boot-assets` |
| Deployment probe | `bash tests/deploy/probe_edge.sh` |

Audit write failures return 503 (fail-closed). When reporting an issue, include the time window, device MAC, and Worker id; do not attach private keys, tokens, PSKs, or `admin.conf`.

## Related pages

- [Deployment](../guide/deployment.md)
- [Operations](./operations.md)
- [Boot chain](../concepts/boot-chain.md)
- [Storage and data plane](../concepts/storage.md)
- [Firmware build and deployment](../firmware/build-deploy.md)
