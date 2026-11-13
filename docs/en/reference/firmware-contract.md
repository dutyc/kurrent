# Firmware Contract

This page describes the interface between the firmware and the main repository: TPM identity commands, NV settings, TOFU behavior, challenge-attestation formats, control-plane reporting, and boot variables. Field-level authority lives in the firmware repository's `docs/device-trust-usage.md`, `docs/capability-reference.md`, and `patches/README`; control-plane endpoint details are authoritative in `api/control-plane-api.en.md`.

## Device identity commands

Firmware provides device identity through TPM 2.0; registration, challenge and admission logic live in the main repository's boot scripts and control plane.

| Command | Precondition | Output | Persistence |
| --- | --- | --- | --- |
| `akgen` | none | Creates a restricted signing key (ECC P-256 or RSA-2048) inside the TPM and persists it in TPM NV; reuses an existing AK and never overwrites it | TPM NV |
| `akpub` | AK exists | Emits the AK public key SPKI DER hex, TPM2B_NAME (68 hex), algorithm type and the firmware's Secure Boot state into the temporary settings `ak-public`, `ak-name`, `ak-type`, `sb` | Current boot only |
| `quote <nonce>` | AK exists | Quotes PCR 0/2/4/7 with SHA-256 and copies the nonce verbatim into `extraData`; emits `quote` (raw TPMS_ATTEST bytes as hex, without the TPM2B length prefix) and `quote-sig` (signature over the quote bytes, hex) | Current boot only |

Notes:

- `akgen` is the idempotent entry point: boot scripts call it unconditionally once; it creates on first boot and skips when an AK already exists.
- The AK private key is generated and held by the TPM, cannot be exported, and firmware never reads the private key in plaintext.
- PCR reads and the quote must happen in one TPM call so measurements cannot change between reads.
- The quote's `extraData` must be the ASCII bytes of the `nonce` string itself (64 hex characters, i.e. 64 bytes), not the 32-byte hex-decoded value; no other encoding transformation.

## NV settings

| Setting | Type | DHCP tag | Persistence | Writer |
| --- | --- | --- | --- | --- |
| `server-fingerprint` | hexraw | 0x5f | NVRAM | TOFU |
| `trust` | hexraw | 0x5a | NVRAM (root certificate override) | TOFU |
| `ak-public` | hexraw | 0x60 | Memory | `akpub` |
| `ak-name` | hexraw | 0x63 | Memory | `akpub` |
| `ak-type` | string | 0x64 | Memory | `akpub` |
| `sb` | string | 0x65 | Memory | `akpub` (`1` = Secure Boot enabled, `0` = disabled) |
| `quote` | hexraw | 0x61 | Memory | `quote` |
| `quote-sig` | hexraw | 0x62 | Memory | `quote` |

Clearing persistent settings requires the `nvo/` block prefix:

```text
clear nvo/server-fingerprint  # remove the TOFU pin; repinned on next HTTPS connection
```

A bare `clear server-fingerprint` or an empty `set server-fingerprint` has no effect on the NVS backend. The AK is not persisted through iPXE settings; clearing the TPM or replacing the mainboard loses the original AK, and the device must be deleted from the ledger and re-admitted.

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

## Challenge-attestation

The control plane issues a one-time nonce:

```text
GET /devices/challenge?mac=<MAC>
→ #!ipxe
  set nonce <64 hex characters>
```

The device runs:

```text
quote ${nonce}
```

Control-plane verification:

| Item | Value |
| --- | --- |
| Quote body | TPMS_ATTEST: magic `0xFF544347`, type `TPM_ST_ATTEST_QUOTE` |
| `extraData` | The ASCII bytes of the nonce string (64 hex characters) |
| PCR selection | Fixed 0/2/4/7, SHA-256 only |
| PCR digest | 32-byte SHA-256 |
| AK signature | ASN.1 DER for ECC P-256; PKCS#1 v1.5 with SHA-256 for RSA-2048 |
| Transport encoding | hex (both quote and signature) |

The nonce is single-use, short-lived, and bound to the MAC. The challenge endpoint returns 404 for unknown, unclaimed, or revoked devices. Attestation does not include the hostname: identity is constrained by the device ledger entry, the AK, and the one-time nonce.

## Device reporting

```text
GET /devices/report?mac=<MAC>&ak_public=<hex>&ak_name=<hex>&ak_type=ecc_p256&sb=1&uuid=...&manufacturer=...
```

| Field | Meaning |
| --- | --- |
| `mac` | Required; the device NIC MAC |
| `ak_public` | AK public key SPKI DER hex; paired with `ak_name` and required for registration and AK claiming |
| `ak_name` | TPM2B_NAME hex (68 hex: algorithm prefix + public-key digest) |
| `ak_type` | `ecc_p256` (default) or `rsa2048`; a mismatch with the actual key algorithm means no identity |
| `sb` | Secure Boot state, `1` = enabled; must be `1` during registration |
| `uuid` | SMBIOS UUID; an existing value is never overwritten by a self-reported one |
| `manufacturer` / `product` / `serial` | Vendor, model, serial number |
| `cpumodel` | CPU model |
| `mem-total` / `mem-type` / `mem-speed` | Memory size, type, speed |
| `chip` / `busid` | NIC chip and PCI address |

Behavior:

- Existing device: merge the fingerprint and update `last_seen`; within the registration window a valid AK completes the claim, and a mismatching AK is rejected and audited.
- Unknown MAC: admitted only when the registration window is open, admission quota remains, a valid AK is present, and `sb=1`; otherwise ignored.
- Revoked device: reports are ignored; the device is neither updated nor revived.
- The response is an empty `#!ipxe` script with no script side effects.

Reporting is unauthenticated and rate-limited by source IP.

## Boot variables

```text
GET /boot-vars?mac=<MAC>&hostname=<name>&nonce=<nonce>&quote=<hex>&quote_sig=<hex>
```

The response is a `#!ipxe` script by default; `format=json` returns the same fields as JSON (snake_case). The encoding does not change admission.

Admission order:

1. **Identify**: MAC → device ledger → `bound_worker_id`; hostname is only a fallback for legacy records.
2. **Binding check**: a request carrying a MAC must come from the device bound to that Worker, or an empty script is returned.
3. **Attestation**: validate the quote structure, AK signature and `extraData == nonce`, then compare the PCR digest with the platform baseline. Requests that fail attestation never receive keys; with enforcement on by default they are rejected outright.
4. **Data-plane readiness**: the nvmet backend must have both an authentication key and a ready data-plane port, or nothing is projected.
5. **Projection**: emit the variables below, all valid only for this boot.

| Variable | Meaning | Condition |
| --- | --- | --- |
| `disk-nqn` | Full NQN of the system disk, passed through verbatim | A default disk exists |
| `storager-ip` | Data-plane address | A default disk exists and its storage node is registered |
| `nbft-tls-port` | Data-plane TLS port | nvmet backend with a ready port |
| `nbft-tls-psk` | TLS PSK derived from the authentication key | Attestation passed and a key exists |
| `nbft-secret` | DHHC-1 authentication key | Attestation passed and a key exists |
| `hostnqn` | The Worker's Host NQN | Node and disk NQN available |
| `os` / `os-version` | OS label for the menu | Default disk carries OS information |
| `menu-default` / `menu-timeout` | Auto-boot entry and timeout (milliseconds) | Always emitted |
| `base-iqn` | Disk IQN prefix, used to derive `initiator-iqn` | A default disk exists |
| `iscsi-sep` | iSCSI backend separator (not consumed by the current boot scripts) | stgt / lio backends only |

Without a default disk, the control plane returns `menu-default=reboot` with a short timeout, so the device reboots while waiting for a disk or default selection. It never constructs a plaintext fallback.

## Boot script variables

`boot.ipxe.cfg` is maintained by the platform; operators normally do not edit it. It defines:

| Variable | Meaning |
| --- | --- |
| `controller_ip` | Control-plane address, defaulting to DHCP `next-server` |
| `https-port` / `https-url` | Control-plane HTTPS entry |
| `menu-url` | Absolute URL of the menu script |
| `storager-ip` | Default data-plane address (overridable by `/boot-vars`) |
| `base-iqn` | Base IQN, used to derive `initiator-iqn` |
| `iscsi-sep` | Default iSCSI separator (not consumed by the current boot scripts) |
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

- The full chain requires firmware with the NVMe/TCP, authentication, NVS, TOFU, TPM AK and quote, Host NQN, and NBCT patches; firmware without a TPM identity cannot be admitted or obtain credentials.
- The control plane never sends keys to requests that fail attestation; enforcement is on by default.
- Upgrading the control plane does not require a simultaneous firmware upgrade, but new variables (for example a new TLS port) must be consumable by the firmware; check firmware capabilities before upgrading.

## Security boundary

- Keys are injected only after attestation; `/boot-vars` provides no registration channel.
- The AK private key cannot be exported, so a booted system cannot read or copy the device identity; attestation covers PCR 0/2/4/7, not runtime.
- The first hop depends on Secure Boot verifying signed firmware; server TOFU first trust remains a residual risk.
- Clearing the TPM or replacing the mainboard loses the original AK; delete the ledger entry and re-admit the device. Decommissioned devices should clear `server-fingerprint`.

## Related pages

- [Firmware Overview](../firmware/index.md)
- [Firmware Platform Reference](./firmware-platform.md)
- [Build and Artifacts](../firmware/build-deploy.md)
- [Provisioning and Flashing](../firmware/build-deploy.md)
- [Device Identity and Trust](../firmware/index.md#device-identity-and-trust)
- [Boot Chain](../concepts/boot-chain.md)
- [Security Model](../architecture/security.md)
- [API Reference](./api.md)
