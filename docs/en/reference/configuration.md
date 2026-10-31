# Configuration Reference

Kurrent has two authoritative declarations: control-plane configuration and node configuration. Both use `kurrent.io/v1` and are validated by Pydantic models inside the components; the validation image the CLI invokes uses the same schema as runtime loading.

- Control plane: `control_plane/kurrent.yaml`, `kind: ControlPlaneConfiguration`
- Node: `storager/kurrent.yaml`, `kind: NodeConfiguration`

Templates come from `kurrent config print init-defaults` and `kurrent config print node-defaults`, generated from the same source as the `.example` files in the repository. Runtime files are not committed.

## Validation semantics

| Semantics | Behavior |
| --- | --- |
| Unknown fields | Rejected; typos are never silently ignored |
| Required fields | Missing means failure |
| Defaults | Injected at validation time; `config show` displays the effective form |
| Cross-field constraints | Subnet, TTL bounds, and certificate days are checked in the same model |
| Protocol constants | nonce TTL, timeouts, and similar values stay in code, not in the declaration |
| Deployment facts | Container paths, listen addresses, and credential files are not part of the declaration |

Validation happens before anything is written. Declaration errors surface at `kurrent init` / `kurrent join`, not when a container starts.

## Control-plane configuration

### Root fields

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `apiVersion` | string | yes | Fixed `kurrent.io/v1` |
| `kind` | string | yes | Fixed `ControlPlaneConfiguration` |
| `metadata.name` | string | yes | Control-plane name, 1–63 chars, lowercase letters, digits, dot or dash |
| `spec` | object | yes | See below |

### `spec.networking` (required)

PXE deployment network declaration and the sole authority for `dnsmasq.conf`.

| Field | Type | Required | Constraint | Meaning |
| --- | --- | --- | --- | --- |
| `interface` | string | yes | 1–15 chars, no whitespace | NIC dnsmasq binds to |
| `subnet` | string | yes | IPv4 CIDR | Service subnet |
| `dhcpRange` | string | yes | `start,end`, both inside the subnet, excluding network/broadcast | DHCP pool |
| `gateway` | string | yes | IPv4 inside the subnet | DHCP option 3 |
| `dns` | string | yes | IPv4 | DHCP option 6 |

### `spec.pki`

| Field | Default | Range | Meaning |
| --- | --- | --- | --- |
| `bootstrapTokenTtlMinutes` | `60` | 1–1440 | Default bootstrap token TTL |
| `bootstrapTokenMaxTtlMinutes` | `1440` | 1–1440 | Maximum issuable TTL; not below the default |
| `bootstrapTokenMaxUses` | `100` | 1–1000 | Maximum uses per token |
| `componentCertDays` | `90` | 1–3650 | Component certificate lifetime |
| `renewThreshold` | `0.2` | (0, 1] | Renewal trigger as a fraction of remaining lifetime |
| `crlRefreshHours` | `12` | 1–167 | Component CRL re-sign interval; keep below the CRL `nextUpdate` |

### `spec.serverCert`

The nginx 443 self-signed server certificate used by the TOFU boot chain.

| Field | Default | Range | Meaning |
| --- | --- | --- | --- |
| `san` | `IP:127.0.0.1,DNS:localhost` | At least one entry, no duplicates | Comma-separated `IP:` / `DNS:` entries |
| `days` | `3650` | 1–36500 | Certificate lifetime |
| `rotateOnChange` | `false` | — | Re-sign automatically when the declaration changes; changes the TOFU fingerprint |

SAN or lifetime changes are reported as drift only; re-signing requires `rotateOnChange: true`. Devices already in the pool must pin the new server fingerprint.

### `spec.boot`

| Field | Default | Range | Meaning |
| --- | --- | --- | --- |
| `defaultArch` | `x86_64` | Letters, digits, underscore | Default boot architecture |
| `menuTimeoutMs` | `5000` | ≥ 0 | Menu auto-select timeout in milliseconds |
| `autoBootTimeoutMs` | `1` | ≥ 0 | Reboot wait timeout when no default disk exists, in milliseconds |

### `spec.auth`

| Field | Default | Range | Meaning |
| --- | --- | --- | --- |
| `sessionTtlHours` | `12` | 1–168 | Management session lifetime |
| `adminCertDays` | `365` | 1–3650 | Default admin certificate lifetime; not above `userCertDaysMax` |
| `userCertDaysMax` | `3650` | 1–36500 | Maximum user certificate lifetime |
| `tokenTtlHours` | `24` | 1–8760 | Default management token TTL; not above `tokenMaxTtlHours` |
| `tokenMaxTtlHours` | `720` | 1–8760 | Maximum management token TTL |

### `spec.registration`

| Field | Default | Range | Meaning |
| --- | --- | --- | --- |
| `windowMaxTtlMinutes` | `60` | 1–1440 | Maximum duration of one registration window |
| `windowMaxDevices` | `1000` | 1–100000 | Maximum devices admitted in one window |

The window itself is runtime state, opened only briefly during deployment; the declaration only bounds it.

### `spec.credentials`

| Field | Default | Range | Meaning |
| --- | --- | --- | --- |
| `rotationDays` | `90` | 0–3650 | DHHC-1 rotation period; `0` disables |
| `reconcileIntervalSeconds` | `60` | 0–86400 | Credential push reconciliation period; `0` disables |

### `spec.agentTimeoutSec`

| Field | Default | Range | Meaning |
| --- | --- | --- | --- |
| `agentTimeoutSec` | `10` | (0, 600] | Control-plane call timeout to storage Agents, in seconds |

### Complete example

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
  pki:
    bootstrapTokenTtlMinutes: 60
    bootstrapTokenMaxTtlMinutes: 1440
    bootstrapTokenMaxUses: 100
    componentCertDays: 90
    renewThreshold: 0.2
    crlRefreshHours: 12
  serverCert:
    san: "IP:127.0.0.1,DNS:localhost"
    days: 3650
    rotateOnChange: false
  boot:
    defaultArch: x86_64
    menuTimeoutMs: 5000
    autoBootTimeoutMs: 1
  auth:
    sessionTtlHours: 12
    adminCertDays: 365
    userCertDaysMax: 3650
    tokenTtlHours: 24
    tokenMaxTtlHours: 720
  registration:
    windowMaxTtlMinutes: 60
    windowMaxDevices: 1000
  credentials:
    rotationDays: 90
    reconcileIntervalSeconds: 60
  agentTimeoutSec: 10
```

## Node configuration

### Root fields

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `apiVersion` | string | yes | Fixed `kurrent.io/v1` |
| `kind` | string | yes | Fixed `NodeConfiguration` |
| `metadata.name` | string | yes | Node identity (agent_id), 1–63 chars, lowercase letters, digits, dot or dash |
| `metadata.remark` | string | no | Human-readable note, defaults to empty |
| `spec` | object | yes | See below |

`metadata.name` is both the control-plane registry key and the component certificate CN, bound by the bootstrap token. After `kurrent join` writes it, it should not be edited by hand.

### `spec.agent`

| Field | Type | Required | Constraint | Meaning |
| --- | --- | --- | --- | --- |
| `backend` | string | no | `nvmet` / `stgt` / `lio`, default `nvmet` | Data-plane backend |
| `advertiseUrl` | string | no | http(s) URL, may be empty | Agent address reachable from the control plane, used for registration and renewal |
| `diskDir` | string | yes | Absolute host path | Disk image directory; also the compose mount source |
| `nqnBase` | string | yes | `nqn.<yyyy-mm>.<reverse-domain>` | Namespace for Host NQN and disk NQNs |

### `spec.controlPlane`

| Field | Type | Required | Constraint | Meaning |
| --- | --- | --- | --- | --- |
| `url` | string | yes | http(s) URL | mTLS target for component enroll / renew |

### Complete example

```yaml
apiVersion: kurrent.io/v1
kind: NodeConfiguration
metadata:
  name: storage-nvmet-01
  remark: Production storage
spec:
  agent:
    backend: nvmet
    advertiseUrl: https://192.168.80.10:4840
    diskDir: /storager_img
    nqnBase: nqn.2026-07.com.kurrent
  controlPlane:
    url: https://192.168.80.1
```

## Apply and verify

### Control plane

```bash
kurrent config print init-defaults > control_plane/kurrent.yaml
$EDITOR control_plane/kurrent.yaml
kurrent config validate
kurrent config show          # effective form with defaults injected
kurrent init                 # validate, apply, start, wait for health
kurrent config status        # configHash, derived files, certificate drift
```

`init` is idempotent: rerun it after editing the declaration. A validation failure writes nothing; a startup failure rolls back to the previous declaration and restores service with a non-zero exit.

### Node

```bash
kurrent config print node-defaults > storager/kurrent.yaml
$EDITOR storager/kurrent.yaml
kurrent config validate --node
kurrent join https://<control-plane> --token <token> --agent-id <agent_id>
```

`join` generates the declaration from the template when missing and merges the control-plane address when it exists. Node declaration errors fail before writing, not at container startup.

## configHash and drift

The control-plane `configHash` is the SHA-256 of the effective declaration (defaults injected) serialized as canonical JSON (sorted keys, compact separators). It confirms which declaration is loaded at runtime and excludes runtime settings.

Derived objects and drift:

| Object | Source | Drift handling |
| --- | --- | --- |
| `dnsmasq.conf` | `spec.networking` | Regenerated at startup; `config status` reports differences |
| Server certificate | `spec.serverCert` | Report-only by default; re-signed when `rotateOnChange` is on |
| Component PKI | `spec.pki` | Checked at startup; missing or damaged material fails health |

Runtime settings such as the registration window and signature enforcement live in `state/settings.json`; they are not part of the declaration or `configHash`.

## Environment variables

| Variable | Effect | Scope |
| --- | --- | --- |
| `KURRENT_CONFIG_FILE` | Overrides the declaration path the process reads | Control plane and node; for test injection |
| `KURRENT_CP_INSECURE` | Development mode allowing all requests | Local development only, never production |
| `KURRENT_CP_OPENAPI` | Enables the OpenAPI document endpoints | Off by default |
| `KURRENT_CP_ALARM_WEBHOOK` | Alarm delivery URL | Optional |
| `KURRENT_CP_CREDENTIAL_ROTATION_DAYS` | Temporary override of the rotation period | Operations and tests |
| `KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL` | Temporary override of the reconciliation period | Operations and tests |
| `KURRENT_CP_CRL_REFRESH_INTERVAL` | Temporary override of the CRL re-sign period | Operations and tests |

The declaration is the default source for policy; the two background-task variables are temporary overrides. Container paths and listen addresses are fixed by compose and are not part of the declaration.

## Related pages

- [Declarative Configuration](../concepts/configuration.md)
- [Daily Operations](../guide/operations.md)
- [CLI Reference](./cli.md)
- [API Reference](./api.md)
- [Glossary](./glossary.md)
