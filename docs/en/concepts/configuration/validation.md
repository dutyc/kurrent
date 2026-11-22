# Validation and Application

The CLI runs the same schema inside the component images and keeps no local field allowlist. Validation completes before anything is written, so a failure changes no files and starts no containers.

```bash
kurrent config validate              # Control-plane declaration
kurrent config validate --node       # Node declaration
kurrent config show                  # Effective declaration with defaults injected
```

## Validation Rules

| Rule | Behavior |
| --- | --- |
| `extra="forbid"` | Unknown fields, typos and old keys are rejected |
| Default injection | Missing policy blocks use schema defaults |
| Cross-field checks | Subnet/gateway/pool consistency, token TTL caps, certificate day caps and more |
| Paths/NQN | Absolute paths, NQN namespace and addresses are validated at declaration time; node identity is checked against the state file by join |
| Version lock | `apiVersion` and `kind` must match |

## Applying to the Control Plane

```bash
kurrent init
```

1. build the validation image (internal step);
2. validate the declaration with the runtime schema inside the image;
3. apply the declaration to `control_plane/kurrent.yaml`;
4. start or recreate containers and restart the control plane;
5. wait for `/healthz`; a failed startup derivation returns 503;
6. confirm dnsmasq stays running.

On any failure: validation failures write nothing; startup failures restore the previous declaration, restart the control plane and dnsmasq, and return a non-zero exit code.

## Applying to a Storage Node

```bash
kurrent join <cp-url> [--token <token>]
```

1. read or generate the node declaration;
2. merge defaults and validate with the same schema inside the agent image;
3. write `storager/state/agent.id` and boot credentials under the node state root (first run needs `--agent-id`);
4. inject compose variables from `diskDir`, the node state root and trust anchors, start the agent and confirm it stays running;
5. for the nvmet backend, pre-check host prerequisites (kernel modules, configfs, `tlshd >= 1.5.0`) and restart nvmet-host after derived credentials arrive.

A failed declaration validation writes no files.

## What's next

- Back to [Declarative Configuration](../configuration.md)
- Control-plane declaration: [Control-Plane Declaration](./control-plane.md)
- Node declaration: [Node Declaration](./node.md)
- Runtime state and drift: [Drift and Overrides](./drift.md)
