# Declarative Configuration

Kurrent's runtime parameters are described by two YAML declarations: the control-plane declaration and the node declaration. The declarations are the only input; the CLI validates and applies them, and container orchestration is an implementation detail. Node identity is allocated by the control plane when it issues a bootstrap token and lives in a node state file, not in a declaration.

```text
control_plane/kurrent.yaml   → control plane, derived dnsmasq config, PKI and policy
storager/kurrent.yaml        → data directory, backend and NQN namespace
<state>/storager/state/agent.id → node identity (written by join, read-only in the container)
```

`<state>` is the state root outside the repository (default `~/.local/state/kurrent`), fixed by the deployment record `~/.kurrent/deployment.yaml`; declarations move with the repository, runtime state does not.

## Layers

| Layer | Content | Location |
| --- | --- | --- |
| Declaration | Business policy, networking, paths and namespaces a human chooses | `kurrent.yaml` |
| Node state | Machine identity (`agent_id`) | `<state>/storager/state/agent.id` |
| Orchestration | Container names, images, mount targets, port mappings, internal addresses | `docker-compose.yml` |
| Runtime state | Registration window, sessions, devices, Workers, disks, credentials, audit | `<state>/control-plane/state/` |
| One-time credentials | Bootstrap tokens, derived tokens | `<state>/storager/bootstrap/` |
| First-deployment hand-over | Management credential exchange (`request` → `ready` → `consumed`) | `<state>/control-plane/bootstrap/` |

In-container paths, listen addresses and internal communication addresses belong to the orchestration layer and do not enter declarations; the schema rejects them as unknown fields. Keys and tokens never enter YAML.

## Control-Plane Declaration

The control-plane declaration defines networking, PKI, the server certificate, boot policy, authentication policy, the registration window and credential rotation. Except for `spec.networking` and `metadata.name`, every block has defaults; the full key table is in the [Configuration Reference](../reference/configuration.md).

Each block is described under [Control-Plane Declaration](./configuration/control-plane.md).

## Node Declaration

The node declaration defines the data directory, backend, advertised address and NQN namespace. Node identity is not part of it: the control plane allocates it when issuing a bootstrap token, and join writes it to the node state root.

The declaration structure and fields are described under [Node Declaration](./configuration/node.md).

## Validation and Application

The CLI keeps no local field allowlist; it runs the same schema inside the component images. Validation completes before anything is written, and a failure changes no files and starts no containers. `kurrent init` and `kurrent join` apply declarations to the control plane and storage nodes respectively.

How the CLI applies a declaration is described under [Validation and Application](./configuration/validation.md).

## Drift and Overrides

`configHash` confirms which declaration the runtime loaded. Derived artifacts such as dnsmasq configuration, server certificates and PKI are generated one-way from the declaration and drift is reported rather than rewritten. A few environment variables can override background parameters for testing and troubleshooting.

Drift detection and overrides are described under [Drift and Overrides](./configuration/drift.md).

## What's next

- Full key table and defaults: [Configuration Reference](../reference/configuration.md)
- Node identity and the three entities: [Devices, Workers and System Disks](./entities.md)
- Server certificates and boot trust: [Boot Chain](./boot-chain.md)
- Implementation boundaries for applying configuration: [Architecture](../concepts/architecture/index.md)
