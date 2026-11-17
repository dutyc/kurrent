# Node Declaration

The node declaration (`NodeConfiguration`) defines a storage node's data directory, backend, advertised address and NQN namespace. Node identity is not part of it: the control plane allocates it when issuing a bootstrap token, and join writes it to the node state root.

```yaml
apiVersion: kurrent.io/v1
kind: NodeConfiguration
spec:
  agent:
    backend: nvmet
    advertiseUrl: https://192.168.1.20:4840
    diskDir: /pool1/disk_dir
    nqnBase: nqn.2026-07.com.kurrent
  controlPlane:
    url: https://192.168.1.10
```

- `agent_id` (12 lowercase hex characters) is allocated by the control plane when issuing a bootstrap token; the CLI and WebUI only consume the returned identity. Join writes it to `<state>/storager/state/agent.id`. It is the component certificate CN and the control-plane registration key; nicknames belong to the control-plane ledger.
- `diskDir` must be an absolute host path; join injects it as a compose mount source when starting the container and stores no second copy of the configuration.
- `nqnBase` is the namespace for host NQNs and disk NQNs; the control plane declares no separate NQN domain.
- `advertiseUrl` is the address the control plane uses to reach the Agent and must be reachable from the control plane.

## Related Pages

- Back to [Declarative Configuration](../configuration.md)
- Control-plane-side declaration: [Control-Plane Declaration](./control-plane.md)
- Node join flow: [Validation and Application](./validation.md)
- Disks and the backend: [Storage and Data Plane](../storage.md)
