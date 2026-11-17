# Management Tokens and Sessions

Besides client certificates, the management plane accepts Bearer tokens and cookie sessions. One authenticator chain handles all three and produces a single `Identity` for authorization.

| Credential | Carrier | Lifetime and storage |
| --- | --- | --- |
| mTLS client certificate | 443 mutual TLS | User certificates default to 365 days, capped by declaration; the private key stays with the user |
| Management token | `Authorization: Bearer` | Default 24 hours, capped by declaration; only a sha256 digest is stored |
| Session | `__Host-kurrent_session` cookie | Default 12 hours; the cookie carries only `{jti, exp, epoch}`, identity comes from the server ledger |

The authenticator chain tries certificate → token → session in order and stops at the first success. An invalid credential returns 401; there is no downgrade.

## Management Tokens

```bash
kurrent token create --usage admin [--scope admin,readonly,enroll] [--ttl 24h]
```

- Scope is an upper bound: `enroll` can only issue bootstrap tokens, cannot issue admin tokens, and cannot read management APIs.
- `last_used_at` is written back with throttling, making unused credentials visible.
- Revocation deletes the ledger entry; Bearer verification has no cache.

## Sessions

After exchanging a certificate or token for a session, the client carries only the cookie:

- the server records `jti`, user, groups, certificate fingerprint and scopes;
- every request rechecks that the user still exists and groups are current;
- deleting the entry or incrementing the epoch invalidates sessions immediately;
- write requests require `Origin` and `Host` to match.

## Related Pages

- Back to [Identity and Access](../identity.md)
- User certificates and the three CAs: [People and Certificates](./people.md)
- Component credentials: [Component Identity](./components.md)
