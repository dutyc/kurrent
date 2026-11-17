# Component Identity

The storage node's Agent and nvmet-host use certificates issued by the nodes CA. Component identity does not substitute for device or human identity: a component certificate can call only component endpoints, never the management plane.

## First Join

1. an administrator issues a bootstrap token bound to `agent_id` (12 lowercase hex characters, allocated by the control plane at issuance and written by join to `storager/state/agent.id` under the node state root; the CLI and WebUI only consume the returned identity; nicknames belong to the control-plane ledger);
2. the node calls `/enroll` with the token and receives a component certificate;
3. the token is single-use by default; TTL and use count are capped by declaration;
4. the nvmet-host credential is derived by the agent from the enroll response and is not issued separately.

## Steady State

- the agent renews before expiry according to the configured threshold;
- components periodically fetch the nodes CRL and fail closed when it is missing or expired;
- the control plane checks the peer serial on every outbound connection;
- a revoked component is rejected on the next handshake or application check, and its bootstrap tokens are deleted.

## Recovery

Recovering a revoked node: issue a new bootstrap token and rerun `kurrent join`. The agent startup self-check discovers the local certificate in the public CRL and re-enrolls with the new token.

## Related Pages

- Back to [Identity and Access](../identity.md)
- User certificates and the three CAs: [People and Certificates](./people.md)
- Node join steps: [Deployment](../../guide/deployment.md)
