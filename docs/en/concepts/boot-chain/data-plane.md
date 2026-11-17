# Data Plane

The data plane answers "which target, with which credentials". It starts only after the identity stage returns a positive result: the control plane projects the current binding into boot variables, and firmware builds one encrypted connection from them. Block I/O never traverses the control plane.

## Admission

`/boot-vars` is the most important decision on the boot chain. It checks, in order:

1. **Resolution**: the request MAC is resolved through the device ledger to its bound Worker; without a MAC, or on a ledger miss, it falls back to hostname; the dnsmasq projection is consulted only as a last resort for pre-existing setups.
2. **Binding is authentication**: a request carrying a MAC must come from that Worker's bound device; another device cannot claim the Worker's boot identity.
3. **TPM attestation**: requests that failed the identity stage are rejected here; credentials go only to devices that passed attestation.
4. **Credential conditions**: the Worker is bound and a key entry exists; the nvmet backend also requires the storage node's data-plane port to be ready.

## Variable Projection

Once the checks pass, the control plane projects the model into boot variables:

| Variable | Content |
| --- | --- |
| `disk-nqn` | Full NQN of the default disk |
| `storager-ip` | Storage node holding the disk |
| `hostnqn` | Initiator Host NQN for the Worker |
| `nbft-secret` | DH-HMAC-CHAP authentication key |
| `nbft-tls-psk` / `nbft-tls-port` | TLS PSK derived from the key, and the data-plane port |
| `menu-default` | Boot entry selected after the menu timeout |

Non-credential variables are served only when attestation enforcement is explicitly disabled; non-nvmet backends project no NVMe-oF key or PSK. No path assembles a plaintext URI.

## Menu

`menu.ipxe` is loaded over HTTPS and shows the OS label of the current default disk:

- the default disk boots after the timeout;
- to switch system disks, set another disk as default in the Worker detail page in the WebUI; it takes effect on the next boot;
- the boot entry comes from the ledger projection, not from configuration inside the disk.

## Connection

The `sanboot` target looks like:

```text
nvme://<storager>:<tls_port>/<disk_nqn>?secret=<dhhc1>&tls_psk=<psk>&tls_trsvcid=<tls_port>
```

It completes in two layers: DH-HMAC-CHAP answers who may connect, and TLS 1.3 protects content on the same port `4421`. Firmware performs the TLS handshake itself; there is no plaintext fallback port.

## Failures

- the Worker cannot be resolved (MAC unbound, hostname has no match, no dnsmasq projection): empty script;
- a pooled but unbound device: `menu-default=reboot`, rebooting on a short timeout until bound; a Worker without a default disk also returns `reboot`;
- the nvmet backend lacks an authentication key or a ready port: the payload is withheld with reason `no_credential` / `port_not_ready`;
- with attestation enforcement explicitly disabled, the nvmet backend still refuses credentials as `unverified`;
- the device-side handshake fails: the PSK never reached the storage node keyring, `tlshd` is below 1.5.0, or the kernel lacks NVMe/TCP TLS support.

## Related Pages

- Back to the [Boot Chain](../boot-chain.md)
- Where the attestation result comes from: [Device Identity](./device-identity.md)
- Disks, PSK and the keyring: [Storage and Data Plane](../storage.md)
- What happens to the connection next: [Hand-over to the Kernel](./handover.md)
