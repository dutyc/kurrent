# Drift and Overrides

The declaration is the only input; runtime state is its projection. `configHash` confirms which declaration the runtime loaded, and derived artifacts are reported when they disagree with the declaration rather than rewritten automatically.

## configHash

`kurrent config status` returns the canonical SHA-256 of the effective declaration:

```bash
kurrent config status    # configHash
kurrent config show      # Effective form with defaults injected
```

Applying the same declaration twice yields the same configHash; file timestamps and hand edits play no part.

## Derived Artifacts

| Artifact | Authority | On drift |
| --- | --- | --- |
| `dnsmasq.conf` | `spec.networking` | `config status` reports consistency; `init` regenerates it |
| Server certificate | `spec.serverCert` plus first generation | Reports SAN/day drift by default; only `rotateOnChange` re-signs |
| Component PKI | `spec.pki` | Idempotently filled at startup; deleting the PKI directory invalidates existing certificates, equivalent to rebuilding the trust root |
| Credential rotation | `spec.credentials` | Read by the background periodic task |
| Registration window | `spec.registration` | Caps the window; never opens it automatically |

Re-signing a leaf certificate does not change the boot CA devices trust, so re-pooling is unnecessary; drift is reported by default and rotation must be enabled explicitly.

## Startup Errors

Failures deriving artifacts at startup (dnsmasq generation, certificates, PKI) are recorded in `startup.errors`:

- `/healthz` returns 503;
- `kurrent init` treats this as a startup failure and rolls back;
- the `startup` field of `kurrent config status` shows the error and warnings.

## Runtime Overrides

| Variable | Overrides |
| --- | --- |
| `KURRENT_CP_CREDENTIAL_RECONCILE_INTERVAL` | Credential push reconciliation period (seconds) |
| `KURRENT_CP_CREDENTIAL_ROTATION_HOURS` | DHHC-1 periodic rotation (hours) |
| `KURRENT_CP_CRL_REFRESH_INTERVAL` | CRL re-sign period (seconds) |

The declaration is the default source; environment variables serve testing and troubleshooting. In production, write stable values into the declaration so runtime and declaration do not diverge.

## Change Flow

```bash
vim control_plane/kurrent.yaml
kurrent config validate
kurrent init
kurrent config status
```

On a node:

```bash
vim storager/kurrent.yaml
kurrent config validate --node
kurrent join <cp-url>
```

## Related Pages

- Back to [Declarative Configuration](../configuration.md)
- Validation and application: [Validation and Application](./validation.md)
- Operational details, rollback and backup: [Operations](../../guide/operations.md)
