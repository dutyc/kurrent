# CLI Reference

`kurrent` is the control-plane command-line client and deployment tool. It ships as a single binary and covers declaration templates, control-plane initialization, node join, credential issuance, and daily queries. Syntax, full arguments, and behavior are authoritative in the repository's `cli/README.md`; this page summarizes the command surface and common flows.

## Install and credentials

```bash
cd cli
go build -o kurrent .
sudo install -m 0755 kurrent /usr/local/bin/kurrent
kurrent credentials install    # snapshot management credentials into ~/.kurrent
```

`credentials install` copies `control_plane/state/admin.conf` and certificate copies into `~/.kurrent` (directory 0700, files 0600), so a PATH-installed binary works outside the repository over 443 mTLS. Re-run `install` after credential rotation.

Credential discovery order:

1. The file given by `--credentials`
2. The `KURRENT_CREDENTIALS` environment variable
3. Walking up from the current directory for `control_plane/state/admin.conf`
4. `<exe>/../control_plane/state/admin.conf` next to the binary
5. The user-level snapshot `~/.kurrent/admin.conf`

Without management credentials, `--server` and `--token` must be given explicitly for the Bearer channel.

## Command surface

```text
kurrent [--credentials PATH] [--server URL] [--token T] <group> <verb> [args]
```

| Group | Subcommands | Purpose |
| --- | --- | --- |
| `config` | `print init-defaults` / `print node-defaults` | Print control-plane or node declaration templates |
| | `validate [--node] [--config PATH]` | Validate against the runtime schema inside the component image |
| | `show [--node] [--config PATH]` | Print the effective declaration with defaults injected |
| | `status` | Runtime declaration, configHash, dnsmasq and certificate drift |
| `init` | — | Control-plane initialization: validate, apply, start, health-check, roll back on failure |
| `join` | — | Node join: generate or merge the declaration, converge the Agent |
| `token` | `create` / `list` / `revoke` | Issue, list, and revoke bootstrap and management tokens |
| `sessions` | `list` / `revoke` / `revoke-all` | Management session ledger and one/all revocation |
| `cert` | `keygen` / `issue` / `list` / `revoke` | User certificates: local CSR, issuance, listing, revocation |
| | `components` / `revoke-component` | Storage-node component certificates: listing and revocation |
| `credentials` | `install` / `show` / `remove` | Management credential user snapshot |
| `agents` | `list` / `get` / `add` / `edit` / `remove` / `probe` | Storage Agent ledger and capability probing |
| `workers` | `list` / `get` | Worker ledger (read-only) |
| | `credential show` / `set` / `revoke` | View, rotate, and revoke NVMe-oF authentication keys |
| `ops` | `list` | Operation audit log |
| `version` | — | Print the version |

## Control-plane initialization

```bash
kurrent config print init-defaults > control_plane/kurrent.yaml
$EDITOR control_plane/kurrent.yaml
kurrent config validate
kurrent init
kurrent config status
```

`init` does the following:

- builds the validation image and fully validates the declaration;
- writes nothing on validation failure and exits non-zero;
- applies the declaration, restarts the control plane, and waits for `/healthz`;
- rolls back to the previous declaration and restores service on startup failure;
- restarts dnsmasq and confirms it stays up;
- is idempotent, so rerunning after an edit applies the change.

`--config` can point at another declaration file and apply it as the control-plane configuration.

## Node join

```bash
# Control plane: issue a bootstrap token bound to the node identity and print the full join command
kurrent token create --agent-id storage-nvmet-01 --cp-url https://192.168.80.1

# Node: paste the command from the previous step
kurrent join https://192.168.80.1 --token <token> --agent-id storage-nvmet-01
```

`join` does the following:

- generates `storager/kurrent.yaml` from the template when missing, or reads it and syncs the control-plane address when present;
- writes `bootstrap/agent.token` (0600) and the `.env` interpolation keys;
- converges and starts the Agent with compose;
- on the nvmet backend, checks kernel modules, configfs, and `tlshd >= 1.5.0`, and points to `storager/nvmeof/host-prepare.sh` when not ready;
- waits for the derived `nvmet-host` credential to be written, then restarts that component;
- is idempotent and safe to rerun.

Outside the repository root, pass `--dir` to point at the `storager` directory.

## Credential issuance

### Bootstrap token

```bash
kurrent token create --agent-id storage-nvmet-01
kurrent token create --agent-id storage-nvmet-01 --uses 20 --ttl 2h
```

- Single-use by default, 60 minutes;
- bound to `agent_id`; enroll accepts only the same identity;
- `--uses` opens up the use count (maximum 100);
- prints a join command carrying the control-plane address and node id.

### Management token

```bash
kurrent token create --usage admin
kurrent token create --usage admin --scope readonly --ttl 24h
```

- For scripts and WebUI sign-in;
- `--scope` is `admin` / `readonly` / `enroll`;
- 24 hours by default, up to 720 hours;
- only a SHA-256 digest is stored; plaintext appears once in the issuance response.

`token list` lists unexpired entries; `token revoke <token_id>` deletes the entry and invalidates it immediately.

## Certificates

```bash
kurrent cert keygen alice --out ./certs
kurrent cert issue alice --csr ./certs/alice.csr --days 365
kurrent cert list
kurrent cert revoke alice --serial <hex>
kurrent cert components
kurrent cert revoke-component storage-nvmet-01
```

The private key is generated locally and never uploaded; only the CSR is submitted. `issue` requires the CN to be registered in `control_plane/state/users.yml`. Revocation re-signs the CRL, reloads nginx, and invalidates sessions bound to that certificate. Revoking a component also deletes that node's bootstrap tokens; recovery means issuing a new token and rerunning `join`.

## NVMe-oF credentials

The control plane issues a DHHC-1 key automatically when an nvmet system disk is created. The commands below are for explicit rotation and revocation:

```bash
kurrent workers credential show storage-worker-01
kurrent workers credential set storage-worker-01 --generate
kurrent workers credential revoke storage-worker-01
```

`--generate` creates a 32-byte key in place without printing plaintext. `set` reports per-Agent delivery status; the reconciliation thread retries failed deliveries. After revocation, the device's next boot is refused.

## Output and exit codes

| Item | Convention |
| --- | --- |
| Default output | Table |
| `-o json` | Raw JSON for jq pipelines |
| Empty fields | Shown as `-` |
| Success | Exit code 0 |
| Failure | Non-zero exit code, error on stderr |

Positional arguments can be mixed freely with `--key value`, `--key=value`, and `--flag`.

## Common errors

| Symptom | Triage |
| --- | --- |
| `no management credentials` | Run inside the repository, run `credentials install`, or pass `--server` + `--token` |
| mTLS certificate error | Credential file does not match the server; rerun `credentials install` |
| `config validate` fails | Fix the declaration per the error; unknown fields are rejected |
| `join` stops at host preflight | Run `host-prepare.sh` as instructed and confirm kernel modules, configfs, and `tlshd` |
| Token invalid or expired | Run `token create` again; confirm `--agent-id` matches the node declaration |

## Related pages

- [Quickstart](../guide/quickstart.md)
- [Deployment Guide](../guide/deployment.md)
- [Daily Operations](../guide/operations.md)
- [Configuration Reference](./configuration.md)
- [API Reference](./api.md)
