# Firmware Contract

This page describes the interface between the firmware and the main repository: device identity commands, NVRAM settings, TOFU behavior, challenge-response formats, control-plane reporting, and boot variables. Field-level authority lives in the firmware repository's `docs/device-trust-usage.md`, `docs/capability-reference.md`, and `patches/README`; control-plane endpoint details are authoritative in `api/control-plane-api.en.md`.

## Device identity commands

The firmware provides three commands; all business logic (registration, challenge, admission) lives in the main repository's boot scripts and control plane.

| Command | Precondition | Output | Persistence |
| --- | --- | --- | --- |
| `keygen` | none | Generates a 32-byte ECDSA P-256 private key and writes `device-key`; refuses to overwrite an existing key and exits non-zero | NVRAM |
| `pubkey` | `device-key` exists | Prints 130 hex characters (65-byte uncompressed point `04‖X‖Y`) and writes the temporary setting `pubkey` | Current boot only |
| `sign <data...>` | `device-key` exists | Concatenates arguments in order without separators, SHA-256 → ECDSA P-256 → DER → base64, and writes the temporary setting `sig` | Current boot only |

Notes:

- `keygen` is the idempotent entry point: boot scripts call it unconditionally once; it generates on first boot and fails harmlessly afterwards.
- The private key is used only inside the firmware; it is never written to disk or transmitted over the network.
- `pubkey` and `sig` are command output buffers that disappear on reboot; they cannot be read across boots.

## NVRAM settings

| Setting | Type | DHCP tag | Persistence | Writer |
| --- | --- | --- | --- | --- |
| `device-key` | hexraw | 0x5e | NVRAM | `keygen` |
| `server-fingerprint` | hexraw | 0x5f | NVRAM | TOFU |
| `trust` | hexraw | 0x5a | NVRAM (root certificate override) | TOFU |
| `pubkey` | hexraw | 0x60 | Memory | `pubkey` |
| `sig` | string | 0x61 | Memory | `sign` |

Clearing persistent settings requires the `nvo/` block prefix:

```text
clear nvo/device-key          # delete the device key; regenerated on next boot
clear nvo/server-fingerprint  # remove the TOFU pin; repinned on next HTTPS connection
```

A bare `clear device-key` or an empty `set device-key` has no effect on the NVS backend.

Deleting `device-key` is equivalent to changing the device identity and requires re-registration and rebinding; deleting `server-fingerprint` makes the next HTTPS connection enter first-trust state again. Both are sensitive operations and should run through a management flow or controlled provisioning.

## TOFU

TOFU applies automatically in the TLS handshake path; there is no command or configuration item.

| State | Condition | Behavior |
| --- | --- | --- |
| First contact | No `server-fingerprint`, certificate validation fails | Accept the handshake, store the leaf certificate SHA-256 fingerprint in `server-fingerprint`, and mirror it into `trust` |
| Pinned and matching | Fingerprint exists and matches | Validation passes; downloads proceed |
| Pinned and mismatching | Fingerprint exists but differs | Reject the handshake; no HTTP request is sent and the existing pin is not overwritten |

Constraints:

- The fingerprint is the **SHA-256 of the leaf certificate DER**; verify independently with `openssl x509 -outform DER | sha256sum`.
- The pin is a single global anchor: the firmware trusts only the certificate pinned on the first HTTPS connection. Every HTTPS target must share that certificate or chain to it; other traffic can use TFTP/HTTP.
- HTTPS uses the native iPXE TLS implementation, limited to TLS 1.2.
- After control-plane certificate rotation, devices must clear `server-fingerprint` and pin again, or boot will be refused.

## Challenge-response

The control plane issues a one-time nonce:

```text
GET /devices/challenge?mac=<MAC>
→ #!ipxe
  set nonce <64 hex characters>
```

The device signs:

```text
sign ${nonce}${mac}
```

The signed data is `nonce‖mac` with no separator. Control-plane verification:

| Item | Value |
| --- | --- |
| Digest | SHA-256 |
| Algorithm | ECDSA P-256 |
| Signature encoding | ASN.1 DER |
| Transport encoding | base64 (standard alphabet, no line breaks) |
| Public key | Uncompressed point `04‖X‖Y`, 130 hex |

The nonce is single-use, short-lived, and bound to the MAC. The challenge endpoint returns 404 for unknown, unclaimed, or revoked devices. The signature does not include the hostname: identity is constrained by the device ledger entry for the MAC and the one-time nonce.

Online rotation uses a nonce from the same challenge endpoint and signs:

```text
rotate‖mac‖new_pubkey‖nonce
```

with the old private key. On success the device public key is replaced immediately, and the old key stops working on the boot verification path.

## Device reporting

```text
GET /devices/report?mac=<MAC>&pubkey=<130 hex>&uuid=...&manufacturer=...
```

| Field | Meaning |
| --- | --- |
| `mac` | Required; the device NIC MAC |
| `pubkey` | ECDSA P-256 public key, required for registration and key claiming |
| `uuid` | SMBIOS UUID; an existing value is never overwritten by a self-reported one |
| `manufacturer` / `product` / `serial` | Vendor, model, serial number |
| `cpumodel` | CPU model |
| `mem-total` / `mem-type` / `mem-speed` | Memory size, type, speed |
| `chip` / `busid` | NIC chip and PCI address |

Behavior:

- Existing device: merge the fingerprint and update `last_seen`; within the registration window a valid public key can claim the key, and a mismatching key is rejected and audited.
- Unknown MAC: admitted only when the registration window is open, admission quota remains, and a valid public key is present; otherwise ignored.
- Revoked device: reports are ignored; the device is neither updated nor revived.
- The response is an empty `#!ipxe` script with no script side effects.

Reporting is unauthenticated and rate-limited by source IP.

## Boot variables

```text
GET /boot-vars?mac=<MAC>&hostname=<name>&nonce=<nonce>&sig=<base64>
```

The response is a `#!ipxe` script by default; `format=json` returns the same fields as JSON (snake_case). The encoding does not change admission.

Admission order:

1. **Identify**: MAC → device ledger → `bound_worker_id`; hostname is only a fallback for legacy records.
2. **Binding check**: a request carrying a MAC must come from the device bound to that Worker, or an empty script is returned.
3. **Signature verification**: verify `nonce‖mac` with the public key in the device ledger. Unverified requests may receive non-credential variables but never any key; once enforcement is on they are rejected outright.
4. **Data-plane readiness**: the nvmet backend must have both an authentication key and a ready data-plane port, or nothing is projected.
5. **Projection**: emit the variables below, all valid only for this boot.

| Variable | Meaning | Condition |
| --- | --- | --- |
| `disk-nqn` | Full NQN of the system disk, passed through verbatim | A default disk exists |
| `storager-ip` | Data-plane address | The Worker is bound to a node |
| `nbft-tls-port` | Data-plane TLS port | nvmet backend with a ready port |
| `nbft-tls-psk` | TLS PSK derived from the authentication key | Signature verified and a key exists |
| `nbft-secret` | DHHC-1 authentication key | Signature verified and a key exists |
| `hostnqn` | The Worker's Host NQN | Node and disk NQN available |
| `os` / `os-version` | OS label for the menu | Default disk carries OS information |
| `menu-default` / `menu-timeout` | Auto-boot entry and timeout (milliseconds) | Always emitted |
| `base-iqn` / `iscsi-sep` | iSCSI installer root-path parameters | stgt / lio backends only |

Without a default disk, the control plane returns `menu-default=reboot` with a short timeout, so the device reboots while waiting for a disk or default selection. It never constructs a plaintext fallback.

## Boot script variables

`boot.ipxe.cfg` is maintained by the platform; operators normally do not edit it. It defines:

| Variable | Meaning |
| --- | --- |
| `controller_ip` | Control-plane address, defaulting to DHCP `next-server` |
| `https-port` / `https-url` | Control-plane HTTPS entry |
| `chain-url` | Root for installer and tool assets |
| `menu-url` | Absolute URL of the menu script |
| `storager-ip` | Default data-plane address (overridable by `/boot-vars`) |
| `base-iqn` | Base iSCSI IQN |
| `iscsi-sep` | iSCSI separator (overridden per backend type) |
| `initiator-iqn` | iSCSI initiator name |

## Data-plane URI

The menu assembles the NVMe/TCP root path as:

```text
nvme://<storager-ip>:<nbft-tls-port>/<disk-nqn>?secret=<nbft-secret>&tls_psk=<nbft-tls-psk>&tls_trsvcid=<nbft-tls-port>
```

- `secret=` is appended when `nbft-secret` exists; legacy deployments without authenticated targets may omit it.
- `tls_psk` and `tls_trsvcid` are always appended for the firmware-side TLS 1.3 handshake.
- If `disk-nqn`, `nbft-tls-port`, or `nbft-tls-psk` is missing, the menu refuses to boot and does not fall back to a plaintext port.
- On success, `keep-san 1` keeps the SAN connection for the kernel and initramfs.

## NBCT and NBFT

**NBCT** (NVMe Boot Credentials Table) is an ACPI table the firmware installs at sanboot time, with one fixed-size record per authenticated session:

| Field | Content |
| --- | --- |
| `traddr` | Data-plane address |
| `trsvcid` | Data-plane port |
| `nqn` | Subsystem NQN |
| `hostnqn` | Host NQN |
| `secret` | DH-HMAC-CHAP key |

NBCT exists only in memory, never in NVRAM. The initramfs `nbft-connect` module reads `/sys/firmware/acpi/tables/NBCT*` and reconnects each entry with `--dhchap-secret` and `--hostnqn`, with no second network fetch for credentials; without NBCT it falls back to `nvme connect-all --nbft`.

**NBFT** (NVMe Boot Firmware Table) is the discovery table describing transport addresses, subsystems, and NIC information. iPXE-side NBFT generation is not in mainline yet; current verification chains inject an NBFT table to simulate the firmware-generated stage. The mainline firmware provides the NBCT relay; NBFT consumption lives in the initramfs.

## Version and compatibility

- The full chain requires firmware with the NVMe/TCP, authentication, NVS, TOFU, device identity command, Host NQN, and NBCT patches; older firmware without the identity commands fails at `keygen`/`sign` and cannot obtain credentials.
- The control plane never sends keys to unverified requests; with enforcement on, older firmware cannot complete boot.
- Upgrading the control plane does not require a simultaneous firmware upgrade, but new variables (for example a new TLS port) must be consumable by the firmware; check firmware capabilities before upgrading.

## Security boundary

- Keys are injected only after signature verification; `/boot-vars` provides no registration channel.
- The device private key lives in NVRAM and is readable by the booted system; current mainline has no Secure Boot/TPM measurement to isolate it.
- TOFU first contact has no cryptographic protection and depends on PXE network isolation; after pinning, integrity is enforced.
- Never copy NVRAM contents to another device; decommissioned devices should clear `device-key` and `server-fingerprint`.

## Related pages

- [Firmware Overview](../firmware/index.md)
- [Build and Artifacts](../firmware/build.md)
- [Provisioning and Flashing](../firmware/provision.md)
- [Boot Chain](../concepts/boot-chain.md)
- [Security Model](../architecture/security.md)
- [API Reference](./api.md)
