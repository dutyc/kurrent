# CLI Reference

`kurrent` is the control-plane command-line client and deployment tool. It ships as a single binary and covers declaration templates, control-plane initialization, node join, credential issuance, and daily queries. Syntax, full arguments, and behavior are authoritative in the repository's `cli/README.md`; this page summarizes the command surface and common flows.

## Install and credentials

```bash
mkdir -p bin
cd cli
go build -o ../bin/kurrent .
sudo install -m 0755 ../bin/kurrent /usr/local/bin/kurrent
kurrent init                   # first deployment: also installs management credentials into ~/.kurrent
kurrent credentials install    # refresh the snapshot after rotation
```

`kurrent init` installs `~/.kurrent/admin.conf` and certificate copies through the one-shot exchange (directory 0700, files 0600), so a PATH-installed binary works outside the repository over 443 mTLS. Re-run `kurrent init` or `kurrent credentials install` after credential rotation.

Credential discovery order:

1. The file given by `--credentials`
2. The `KURRENT_CREDENTIALS` environment variable
3. The record's `<state>/control-plane/state/admin.conf`
4. Walking up from the current directory for the legacy `control_plane/state/admin.conf`
5. `<exe>/../control_plane/state/admin.conf` next to the binary
6. The user-level snapshot `~/.kurrent/admin.conf`

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
| `init` | — | First-deployment transaction: validate, prepare state, start, exchange credentials, self-test; roll back or resume |
| `join` | — | Node join: generate or merge the declaration, converge the Agent |
| `deployment` | `show [-o yaml\|env]` | Read-only deployment record, repository root, state root, and manual compose variables |
| `host` | `check` | Read-only host baseline: listeners, Docker exposure, firewall, credentials, state root, storage kernel prerequisites |
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

- preflights Docker/Compose v2, the WebUI build output, and ownership/writability of the state root and `~/.kurrent`;
- builds the validation image and fully validates the declaration, writing nothing on failure;
- creates the state root (default `~/.local/state/kurrent`), seeds the compose mount points, and writes the deployment record as `bootstrapping`;
- starts the stack under the pinned project `kurrent`, restarts the control plane, and waits for `/healthz`; rolls back to the previous declaration and restores service on startup failure;
- waits for the one-shot credential exchange, verifies it, installs it into `~/.kurrent`, and runs an mTLS `GET /whoami` self-test with the installed copy;
- consumes the bundle, restarts dnsmasq, confirms it stays up, and moves the record to `ready`;
- is idempotent: rerunning after an edit applies the change; after moving the checkout it reuses the recorded state root and recreates the containers.

`--config` can point at another declaration file and apply it as the control-plane configuration. `--state-dir` selects the state root; `--adopt-state` migrates legacy in-repository state (stops the old stack, copies and verifies every file, then removes the old copies after success).

## Node join

```bash
# Control plane: issue a bootstrap token bound to the node identity and print the full join command
kurrent token create --agent-id 3f2a9c1d8b7e --cp-url https://192.168.80.1

# Node: run the command output in the previous step
kurrent join https://192.168.80.1 --token <token> --agent-id 3f2a9c1d8b7e
```

`--agent-id` is the node identity and accepts only 12 lowercase hex characters; when omitted, the control plane allocates it at bootstrap-token issuance (an explicit value binds an existing identity for recovery), and both `kurrent token create` and the WebUI only consume the returned `agent_id`. The first join writes it to `storager/state/agent.id` under the node state root; reruns must match the state file. Nicknames belong to the control-plane ledger and are set with `kurrent agents edit <id> --remark <text>`. The control-plane address is an origin (a reverse-proxy path prefix is allowed); a mistakenly pasted `/api/cp` suffix is removed before the declaration is written.

`join` does the following:

- generates `storager/kurrent.yaml` from the template when missing, or reads it and syncs the control-plane address when present;
- creates `storager/{state,bootstrap,pki,logs,certs}` under the node state root (default `~/.local/state/kurrent`) and writes the identity `state/agent.id` and `bootstrap/agent.token` (0600);
- resolves the controller trust anchor into `certs/cp-ca.crt`: `--cp-ca` > the co-located controller's `control-plane/state/certs/server.crt` > an existing node copy;
- injects `diskDir`, the node state root and the trust anchor as compose variables and converges the Agent under the pinned project `kurrent-storage`;
- on the nvmet backend, checks kernel modules, configfs, and `tlshd >= 1.5.0`, and points to `storager/nvmeof/host-prepare.sh` when not ready;
- waits for the derived `nvmet-host` credential to be written, then restarts that component;
- is idempotent and safe to rerun.

`--dir` may point at the repository root or the `storager` directory; `--state-dir` selects the node state root; `--cp-ca` supplies the controller trust anchor on split hosts; `--adopt-state` migrates legacy in-repository node state. When the default `diskDir` sits inside the checkout, join warns that moving the repository moves the disk images; a fixed location needs an absolute path outside the repository.

## Host check

```bash
sudo kurrent host check                        # role inferred from running compose services
sudo kurrent host check --role all             # co-located hosts, or before containers start
kurrent host check --role storage              # kernel nvmet prerequisites / configfs / tlshd
sudo kurrent host check --role control -o json --strict
```

A read-only local check that does not call the management API and works before the control plane starts. Public TCP listeners, Docker daemon TCP exposure, and `DOCKER-USER`/ufw are checked for both roles, with role-specific port allowlists (control: 22/53/67/69/80/443; storage: 22/4840/4421). `control` additionally checks control-plane container exposure, credential permissions and the state root (outside the repository, 0700, correct owner, matching the deployment record); `storage` additionally checks the nvmet / nvmet-tcp modules, configfs, and `tlshd`; `all` is the union. Containers are identified by compose service labels, not fixed names. Statuses are `ok` / `warn` / `fail` / `skip`; exit codes are `0` pass, `1` FAIL, `2` usage error, and `--strict` also counts SKIP as failure. Fixing system state still uses `sudo bash storager/nvmeof/host-prepare.sh`.

## Credential issuance

### Bootstrap token

```bash
kurrent token create --agent-id 3f2a9c1d8b7e
kurrent token create --agent-id 3f2a9c1d8b7e --uses 20
```

- Single-use by default, 60 minutes (TTL comes from the control-plane declaration `spec.pki.bootstrapTokenTtlMinutes`);
- bound to `agent_id` (12 lowercase hex); enroll accepts only the same identity;
- `--uses` opens up the use count (1..1000, and no higher than the control-plane `spec.pki.bootstrapTokenMaxUses`, default 100);
- prints a join command carrying the control-plane address and node id.

### Management token

```bash
kurrent token create --usage admin
kurrent token create --usage admin --scope readonly --ttl 24h
```

- For scripts and WebUI sign-in;
- `--scope` is `admin` / `readonly` / `enroll`;
- 24 hours by default; the upper bound comes from the control-plane `spec.auth.tokenMaxTtlHours` (default 720 hours);
- only a SHA-256 digest is stored; plaintext appears once in the issuance response.

`token list` lists unexpired entries; `token revoke <token_id>` deletes the entry and invalidates it immediately. `token_id` is the 6-character prefix before the dot (shown by `token list` or at issuance); pasting the full token is rejected so the secret never reaches server access logs.

## Certificates

```bash
kurrent cert keygen alice --out ./certs
kurrent cert issue alice --csr ./certs/alice.csr --days 365
kurrent cert list
kurrent cert revoke alice --serial <hex>
kurrent cert components
kurrent cert revoke-component 3f2a9c1d8b7e
```

The private key is generated locally and never uploaded; only the CSR is submitted. `issue` requires the CN to be registered in `<state>/control-plane/state/users.yml`. Revocation re-signs the CRL, reloads nginx, and invalidates sessions bound to that certificate. Revoking a component also deletes that node's bootstrap tokens; recovery means issuing a new token and rerunning `join`.

## NVMe-oF credentials

The control plane issues a DHHC-1 key automatically when an nvmet system disk is created. The commands below are for explicit rotation and revocation:

```bash
kurrent workers credential show storage-worker-01
kurrent workers credential set storage-worker-01 --generate
kurrent workers credential set storage-worker-01 --secret-file /secure/dhhc1.key
kurrent workers credential revoke storage-worker-01
```

`--generate` creates a 32-byte key in place without printing plaintext; an existing key is passed via `--secret-file` (mode 0600) or `--secret-stdin`, never as plaintext `--secret` on the command line. `set` reports per-Agent delivery status; the reconciliation thread retries failed deliveries. After revocation, the device's next boot is refused.

## Output and exit codes

| Item | Convention |
| --- | --- |
| Default output | Table |
| `-o json` | Raw JSON for jq pipelines |
| Empty fields | Shown as `—` |
| Success/help | Exit code 0 |
| Runtime failure | Exit code 1, error on stderr |
| Usage error | Exit code 2, command usage printed |

Positional arguments can be mixed freely with `--key value`, `--key=value`, and `--flag`. Every command supports `-h`/`--help`; unknown flags, missing values, duplicate flags, and extra positional arguments are usage errors.

## Common errors

| Symptom | Triage |
| --- | --- |
| `No management credentials found` | Run `kurrent init` first; or run `credentials install` to refresh the snapshot; or pass `--server` + `--token` |
| mTLS certificate error | Credential file does not match the server; rerun `kurrent init` or `credentials install` |
| `config validate` fails | Fix the declaration per the error; unknown fields are rejected |
| `init` reports a foreign state-root owner | Run the suggested `sudo chown -R "$(id -u):$(id -g)" <path>`; do not rerun as root |
| `init` reports legacy state | Confirm the migration, then rerun with `--adopt-state` |
| `join` stops at host preflight | Run `kurrent host check --role storage` first, then `host-prepare.sh` as instructed |
| `join` reports a missing controller trust anchor | Supply the controller `server.crt` with `--cp-ca` (see the split-host step in deployment) |
| Token invalid or expired | Run `token create` again; confirm `--agent-id` matches `<state>/storager/state/agent.id` |

## Related pages

- [Deployment](../guide/deployment.md)
- [Daily Operations](../guide/operations.md)
- [Configuration Reference](./configuration.md)
- [API Reference](./api.md)
