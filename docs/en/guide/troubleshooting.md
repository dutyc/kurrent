# Troubleshooting

This page is organized by symptom: first identify the boundary where the failure occurs, then inspect the responsible component. Task pages keep their local failure tables; this page covers cross-component isolation, evidence collection, and recovery paths.

## Locate the failure boundary

- **Browser or CLI cannot sign in** — management plane 443 / nginx / control plane; `curl -k https://<control-plane-ip>/healthz`; nginx logs.
- **Signed in, but an Agent is not `ok`** — control plane → Agent 4840; `kurrent agents list`; node `spec.agent.advertiseUrl`.
- **Device gets no DHCP response** — dnsmasq / PXE segment; device segment, `interface=`, firewall 67/69.
- **DHCP works but TFTP download fails** — TFTP root / 69; TFTP root files and permissions.
- **iPXE stalls at 4443 HTTPS** — boot CA / 4443 certificate / port; boot CA used at firmware build time, leaf SAN and port.
- **Device not pooled** — registration window / TPM AK / revocation; window state on Devices; device report.
- **Empty menu or boot loop** — binding / system disk / default disk; Worker details; `/boot-vars` projection.
- **Data plane fails after the menu** — 4421 / tlshd / credentials; `kurrent host check --role storage`; credential push state.
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
- `init` reports a foreign owner for the state root or `~/.kurrent`: run the suggested `sudo chown -R "$(id -u):$(id -g)" <path>`; do not rerun as root.
- `init` reports legacy in-repository state: confirm the migration, then rerun with `--adopt-state`; when a `bootstrapping` record and exchange bundle remain, the rerun resumes at the credential exchange.
- `init` reports a missing state root while the deployment record exists: restore the state root from a backup, or remove `~/.kurrent/deployment.yaml` and deploy again; `kurrent deployment show` prints the recorded values.

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
2. Confirm the report carries a valid TPM AK: first boot must run `akgen` and `akpub`, with Secure Boot enabled.
3. Confirm the device is not revoked; a `revoked` entry does not recover through a new report.
4. Check control plane logs and audit for `/devices/report`.

After the window closes, reports update fingerprints only and do not pool devices. There is no registration path outside the window.

## Boot refused

| Check | Entry |
| --- | --- |
| Is the device bound to a Worker | Devices page; the device ledger is authoritative |
| Does the Worker have a system disk and a default disk | Worker details; without a default disk the control plane returns `menu-default=reboot` |
| Is TPM attestation enforcement disabled | Devices page registration panel; it is on by default, and disabling it lets bound devices without attestation boot (credentials are still never injected) |
| Does `/boot-vars` return complete connection data | `GET /boot-vars`; check `disk-nqn`, `storager-ip`, port, and PSK |
| Does the NVRAM fingerprint match the control plane certificate | Re-provision `server-fingerprint` after certificate rotation |

## Data plane connection failure

```bash
kurrent host check --role storage
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

## Device identity, TPM attestation and boot trust

Device identity is a TPM 2.0 AK, and attestation is submitted with `/boot-vars`. The control plane records rejection reasons in the `boot_vars.credential` audit and in `attestation.last_reason` on the device record; the Attestation column and expanded row on the Devices page show the state and reason code.

| Reason code | Meaning | Action |
| --- | --- | --- |
| `no_tpm` | The report carried no valid AK (no TPM, or firmware lacks TCG2) | Confirm the target has TPM 2.0 and it is enabled; see the next row for Secure Boot |
| `sb_disabled` | The registration-time report says `sb=0` (Secure Boot off) | Enable Secure Boot, enroll the site trust store, and admit the device again |
| `window_closed` | An unknown MAC reported while the window was closed or the quota was exhausted | Open the registration window and report again |
| `ak_unregistered` | The device has no AK identity yet | Report with an AK while the registration window is open |
| `missing_mac` | `/boot-vars` carried no MAC and the hostname could not locate a device | Pass `mac=${net0/mac}` from the boot script |
| `nonce_invalid` | The nonce is missing, used, expired or bound to another MAC | Request `/devices/challenge` again; do not reuse an old nonce |
| `quote_invalid` | Quote structure, extraData or signature does not verify | Confirm firmware quotes PCR 0/2/4/7 with SHA-256 and copies the nonce verbatim into extraData |
| `pcr_mismatch` | The PCR digest does not match the platform baseline (firmware or boot configuration changed) | Verify the change, then click Approve New Baseline in the device detail; the next boot re-attests |
| `baseline_approved` | The baseline was just approved and awaits the next attestation | Reboot the device to complete one attestation |

- **Boot HTTPS rejected** — the server certificate does not chain to the boot CA embedded in firmware; check the `BOOT_CA_CERT` used at build time against `state/pki/ca/boot/ca.crt`, and whether the 4443 leaf SAN covers the address devices use.
- **A second HTTPS host is rejected** — the boot CA is a global single anchor; consolidate boot HTTPS endpoints and certificates; use TFTP/HTTP for other traffic.
- **TLS handshake fails** — server TLS is above 1.2 or uses incompatible suites; limit the server to TLS 1.2.
- **`${quote}` / `${quote-sig}` is empty** — the TPM command failed or the device has no TPM; check firmware logs; devices without a TPM are not admitted.
- **`device.claim rejected reason=ak_mismatch`** — the registered AK differs from this report; the AK cannot be replaced online, so delete the ledger entry and admit the device again during the registration window.

## Collect evidence

| Evidence | Location |
| --- | --- |
| Control plane startup and derived artifacts | `docker logs kurrent-control-plane`; `kurrent config status` |
| Management request decisions | `control-plane/state/audit.jsonl` under the state root (`kurrent deployment show` prints the state root) |
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
