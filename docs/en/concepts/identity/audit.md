# Audit and Alarms

Every management request appends one `state/audit.jsonl` record with principal class, principal, issuer, serial, fingerprint, route, decision, reason and request_id. Secrets and token plaintext are never recorded.

If the write fails, the request returns 503 (fail-closed).

## Alarm Events

Eight event classes are written as `event=alarm`:

- boot credentials injected but TPM attestation not verified;
- component or control principals on a management route;
- unknown issuer / CRL hit;
- a burst of authentication failures from the same source;
- bootstrap identity or CN mismatch;
- user/admin certificate issuance, CRL update, CA generation;
- `users.yml` changes;
- audit write failure.

## Delivery

With `KURRENT_CP_ALARM_WEBHOOK` set, alarm events are POSTed as JSON by a background queue; delivery stays outside the request path.

## What's next

- Back to [Identity and Access](../identity.md)
- Audit constraints in the threat model: [Security Model](../../concepts/architecture/security.md)
- Alarm troubleshooting: [Troubleshooting](../../tasks/troubleshooting.md)
