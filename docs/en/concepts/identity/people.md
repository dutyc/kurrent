# People and Certificates

The management plane is reachable only through HTTPS `443`. Human users and admin use client certificates issued by the users CA; the control plane receives only the CSR, never the private key.

## Single Entry

The control-plane service itself has no TCP listener; it listens on an in-container unix socket, and nginx is the only peer. `X-Client-Cert-*` and `X-Client-Class` headers are trusted only when injected by nginx; other proxied locations clear them by default.

## Three CAs

| CA | Issues | Purpose |
| --- | --- | --- |
| users | Human users and admin | Management mTLS; nginx classifies it as `human` |
| nodes | agent / nvmet-host | Component enroll / renew and component mTLS; classified as `component` |
| control | The control plane's own client | Control-plane outbound connections to components; not exposed on 443 |

nginx trusts only the users + nodes bundle and derives the principal class from the issuer. Every control-plane route declares its accepted classes, **denying by default**: component certificates cannot reach management routes, human certificates cannot call component renewal endpoints, and control-domain certificates do not enter 443.

## User Certificates

An administrator or user generates the private key and CSR locally:

```bash
kurrent cert keygen alice --out ./certs
kurrent cert issue alice --csr ./certs/alice.csr
```

The control plane receives only the CSR. The CN must already exist in `state/users.yml`; groups also come from that file.

Revocation can target one certificate (by serial) or all certificates for a user. The CRL is re-signed and nginx reloaded, so the next TLS handshake is rejected. Revocation also removes that user's sessions.

## Authorization

- Reads require authentication only.
- Writes require the `admin` group.
- The `enroll` scope only permits issuing bootstrap tokens.
- Session-authenticated writes additionally require same-origin.

## What's next

- Back to [Identity and Access](../identity.md)
- Tokens and sessions: [Management Tokens and Sessions](./tokens.md)
- Where users and groups are declared: [Declarative Configuration](../configuration.md)
