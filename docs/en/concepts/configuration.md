# Declarative Configuration

Kurrent's runtime parameters are described by two YAML declarations: one for the control plane and one for each node. The declaration is the only input; the CLI validates and converges, and container orchestration is an implementation detail.

```text
control_plane/kurrent.yaml   → control plane, derived dnsmasq configuration, PKI and policy
storager/kurrent.yaml        → node identity, data directory, backend and NQN namespace
```

## Layering

| Layer | Content | Location |
| --- | --- | --- |
| Declaration | Business policy, networking, paths, identity, NQN namespace | `kurrent.yaml` |
| Orchestration | Container names, images, mount targets, port mappings, internal addresses | `docker-compose.yml` |
| Runtime state | Registration window, sessions, devices, Workers, disks, credentials, audit | `control_plane/state/` |
| One-time credentials | Bootstrap token, derived tokens | `storager/bootstrap/` |

In-container paths, listen addresses and internal endpoints belong to orchestration and are not part of the declaration; putting them in YAML is rejected as unknown fields. Keys and tokens never enter YAML.

## Control-Plane Declaration

```yaml
apiVersion: kurrent.io/v1
kind: ControlPlaneConfiguration
metadata:
  name: kurrent-cp
spec:
  networking:        # required: five deployment facts
    interface: enp3s0
    subnet: 192.168.80.0/24
    dhcpRange: 192.168.80.50,192.168.80.100
    gateway: 192.168.80.2
    dns: 223.5.5.5
  pki: {}            # bootstrap tokens, component certificates, renew threshold, CRL period
  serverCert: {}     # TOFU server certificate SAN/days/rotation-on-change
  boot: {}           # architecture, menu timeout, auto-boot timeout
  auth: {}           # session/certificate/token policy
  registration: {}   # registration window TTL and admission cap
  credentials: {}    # DHHC-1 rotation and push reconciliation period
  agentTimeoutSec: 10
```

Apart from `spec.networking` and `metadata.name`, every block has defaults. The full key table is in the [Configuration Reference](../reference/configuration.md).

### Networking Declaration

`spec.networking` is the sole authority for dnsmasq configuration:

- `interface` becomes `interface=`;
- `subnet` determines the netmask;
- `dhcpRange` becomes the address pool;
- `gateway` / `dns` become DHCP options 3/6.

The control plane renders `dnsmasq/dnsmasq.conf` from the declaration at startup. **Do not edit the generated file**: the next startup overwrites it. `kurrent config status` reports whether the generated file still matches the declaration.

## Node Declaration

```yaml
apiVersion: kurrent.io/v1
kind: NodeConfiguration
metadata:
  name: 3f2a9c1d8b7e      # node identity (agent_id): 12 lowercase hex, written by join
  remark: Production storage node
spec:
  agent:
    backend: nvmet
    advertiseUrl: https://192.168.1.20:4840
    diskDir: /pool1/disk_dir
    nqnBase: nqn.2026-07.com.kurrent
  controlPlane:
    url: https://192.168.1.10
```

- `metadata.name` is the machine identity, the component certificate CN and the control-plane registry key; only 12 lowercase hex characters are accepted.
- `metadata.remark` is the only place for a descriptive name; it is recorded in the control-plane ledger on first enroll.
- `diskDir` must be an absolute host path; it is also synchronized as the compose mount source.
- `nqnBase` is the namespace for Host NQNs and disk NQNs; the control plane does not declare a separate NQN domain.

## Validation

The CLI keeps no local field list; it runs the same schema inside the component image:

```bash
kurrent config validate              # control-plane declaration
kurrent config validate --node       # node declaration
kurrent config show                  # effective declaration with defaults injected
```

Semantics:

| Rule | Behaviour |
| --- | --- |
| `extra="forbid"` | Unknown fields, typos and obsolete keys are rejected |
| Default injection | Missing policy blocks use schema defaults |
| Cross-field validation | Subnet/gateway/pool consistency, token TTL caps, certificate day caps |
| Identity/path/NQN | Node name, absolute path and NQN namespace are validated at declaration time |
| Version lock | `apiVersion` and `kind` must match |

Validation completes before anything is written. On failure, no file is modified and no container starts.

## Applying

### Control Plane

```bash
kurrent init
```

Steps:

1. Build the validation image (internal step).
2. Validate the declaration with the runtime schema inside the image.
3. Apply the declaration to `control_plane/kurrent.yaml`.
4. Start/recreate containers and restart the control plane.
5. Wait for `/healthz`; a startup artifact failure returns 503.
6. Confirm dnsmasq stays running.

On any failure:

- validation failure: nothing is written;
- startup failure: the previous declaration is restored, the control plane and dnsmasq are restarted, and the command exits non-zero.

### Storage Node

```bash
kurrent join <cp-url> [--token <token>]
```

Steps:

1. Read or generate the node declaration.
2. Merge defaults and validate with the same schema used by the agent image.
3. Write the declaration, bootstrap credential and `.env` interpolation keys.
4. Start the agent and confirm it stays running.
5. On nvmet, preflight host prerequisites (kernel modules, configfs, `tlshd >= 1.5.0`), wait for the derived credential, then restart nvmet-host.

A validation failure writes nothing.

## Runtime State and Drift

### configHash

`kurrent config status` returns the canonical SHA-256 of the effective declaration. It identifies which declaration the runtime loaded:

```bash
kurrent config status    # configHash
kurrent config show      # effective form after defaults
```

File timestamps and manual edits are not part of the judgement.

### Derived Artifacts

| Artifact | Authority | Drift handling |
| --- | --- | --- |
| `dnsmasq.conf` | `spec.networking` | `config status` reports consistency; `init` regenerates |
| Server certificate | `spec.serverCert` + first generation | SAN/day drift is reported by default; `rotateOnChange` reissues |
| Component PKI | `spec.pki` | Filled in idempotently at startup; deleting the directory invalidates existing certificates and recreates the trust roots |
| Credential rotation | `spec.credentials` | Read by background cycles |
| Registration window | `spec.registration` | Bounds the window; does not open one |

Reissuing the server certificate changes the TOFU fingerprint, and enrolled devices must re-trust it. Drift is therefore reported only by default; rotation is explicit.

### Startup Errors

Failures in startup artifacts (dnsmasq render, certificate, PKI) are recorded in `startup.errors`:

- `/healthz` returns 503;
- `kurrent init` treats this as a startup failure and rolls back;
- the `startup` field of `kurrent config status` lists errors and warnings.

## Runtime Overrides

These environment variables temporarily override background parameters from the declaration:

| Variable | Override |
| --- | --- |
| `KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL` | Credential push reconciliation period (seconds) |
| `KURRENT_CP_CREDENTIAL_ROTATION_DAYS` | DHHC-1 scheduled rotation days |
| `KURRENT_CP_CRL_REFRESH_INTERVAL` | CRL re-sign period (seconds) |

The declaration is the default source; environment variables are for testing and troubleshooting. Production should keep stable values in the declaration so runtime state does not diverge from it.

## Change Workflow

```bash
vim control_plane/kurrent.yaml
kurrent config validate
kurrent init
kurrent config status
```

Node:

```bash
vim storager/kurrent.yaml
kurrent config validate --node
kurrent join <cp-url>
```

Operational details, rollback and backup are in [Operations](../guide/operations.md).

## Related Pages

- Full key table and defaults: [Configuration Reference](../reference/configuration.md)
- Node identity and the three entities: [Devices, Workers and System Disks](./entities.md)
- Server certificate and TOFU: [Boot Chain](./boot-chain.md)
- Implementation boundaries: [Architecture](../architecture/index.md)
