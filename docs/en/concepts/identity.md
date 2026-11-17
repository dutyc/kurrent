# Identity and Access

Kurrent has two orthogonal identity chains:

- **Management identity**: how humans, automation and components prove themselves when calling management APIs.
- **Device identity**: how a machine proves itself on the boot chain when requesting boot credentials.

The two chains use different trust roots and cannot substitute for each other. A valid device key grants no management access; an administrator certificate cannot impersonate a device.

## Device Identity

Device identity is a TPM 2.0 attestation key (AK). Firmware creates the restricted signing key inside the local TPM, and the private key is non-exportable; on every boot the AK produces a quote over PCR 0/2/4/7, which the control plane compares with the platform baseline, with a one-shot nonce preventing replay. The full registration and attestation flow is in [Device Identity](./boot-chain/device-identity.md).

## People and Certificates

The management plane is reachable only through HTTPS `443`. Human users generate the private key and CSR locally, and the control plane receives only the CSR and issues through the users CA. Revocation can target one certificate (by serial) or all certificates for a user.

Certificate issuance and revocation are covered in [People and Certificates](./identity/people.md).

## Management Tokens and Sessions

Besides client certificates, the management plane accepts Bearer tokens and cookie sessions: the authenticator chain tries certificate → token → session in order and stops at the first success. Tokens are stored as digests only, and the cookie carries only `{jti, exp, epoch}`; identity comes from the server ledger.

Token and session details are covered in [Management Tokens and Sessions](./identity/tokens.md).

## Component Identity

The storage node's Agent and nvmet-host use certificates issued by the nodes CA. On first join they call `/enroll` with a bootstrap token bound to `agent_id`; in steady state they renew before expiry and fetch the nodes CRL periodically, failing closed when it is missing or expired.

Enrollment, renewal and revocation are covered in [Component Identity](./identity/components.md).

## Audit and Alarms

Every management request writes one audit record with the principal class, principal, issuer, serial, fingerprint, route, decision, reason and request_id; if the write fails, the request returns 503. Eight event classes are written as `event=alarm` and can be delivered through a webhook.

Audit fields and alarm delivery are covered in [Audit and Alarms](./identity/audit.md).

## Related Pages

- How the boot chain uses device identity: [Boot Chain](./boot-chain.md)
- Declared users and groups: [Declarative Configuration](./configuration.md)
- Threat model and residual risk: [Security Model](../architecture/security.md)
- Day-to-day commands: [Operations](../guide/operations.md)
