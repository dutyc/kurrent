# Device Identity

Device identity answers "is the requester that device" and yields a reviewable conclusion: which ledger entry this machine is, and whether its boot environment still matches its baseline. The trust root lives in hardware, not on a disk: swapping the system disk does not change the device identity.

## Trust Root: TPM AK

On first boot, firmware creates an attestation key inside the local TPM (a restricted signing key; ECC P-256 or RSA-2048); the private key is non-exportable. Later boots reuse the same AK, and an existing AK is never recreated. The control plane records only the AK public key (SPKI DER), the AK name and the key type.

The AK cannot be rotated online: device recovery or a mainboard swap goes through the administrator flow of deleting the entry and re-enrolling during a registration window.

## Registration and Admission

Firmware reports the 11-field fingerprint (MAC, UUID, SMBIOS, CPU, memory, NIC and others), the AK public key, name and type, and the Secure Boot state to `/devices/report`.

The control plane records devices by MAC, but **registration grants no privilege**:

- an unknown device is admitted only while the registration window is open, within the admission quota, with a valid AK and `sb=1`;
- once the window closes, reports from known devices still update the fingerprint; unknown devices are neither pooled nor written to the ledger;
- devices without a TPM, or with Secure Boot off, are not admitted;
- a pooled but unbound device receives no boot credentials.

## Challenge and Attestation

The device requests a one-shot nonce from `/devices/challenge`; a device that is unknown, unclaimed or revoked gets a 404. Firmware then produces an SHA-256 quote over PCR 0/2/4/7 with its AK:

```text
extraData = nonce    (verbatim; no encoding transform)
qualifiedSigner = AK
pcrDigest = SHA-256( PCR 0 / 2 / 4 / 7 )
```

Reading the PCRs and producing the quote happen in one TPM call, so no state can change between the two reads. The quote and signature are submitted with `/boot-vars`, where the control plane checks the structure, the AK signature, the single-use nonce and its binding, and finally the PCR digest. `report`, `challenge` and `boot-vars` are rate-limited per source IP.

## Platform Baseline

The baseline is recorded the first time attestation passes. A later digest change is denied as `pcr_mismatch` and kept as a pending digest; after reviewing the change, an administrator approves it with `POST /devices/{mac}/approve-baseline`.

The first baseline is only as trustworthy as the provisioning environment: if the first attestation happens under attacker control, the baseline is poisoned. Initial enrollment must be verified against the provisioning environment.

## Reason Codes

| Reason code | Meaning | Raised at |
| --- | --- | --- |
| `no_tpm` | The report carries no valid AK | Registration |
| `sb_disabled` | Secure Boot is reported off | Registration |
| `window_closed` | The registration window is closed or the quota is used up | Registration |
| `ak_mismatch` | A known device reports a different AK | Claim |
| `ak_unregistered` | The ledger has no AK to verify against | `/boot-vars` |
| `quote_invalid` | The quote is missing, malformed or has a bad signature | `/boot-vars` |
| `nonce_invalid` | The nonce is expired, already used, or bound to another MAC | `/boot-vars` |
| `pcr_mismatch` | The PCR digest differs from the platform baseline | `/boot-vars` (pending kept) |
| `missing_mac` | A request without a MAC under enforcement | `/boot-vars` |

The device never sees the reason code: a rejected `/boot-vars` returns an empty script and the device has no variables to run. The code is written to the audit log `state/audit.jsonl` and kept as the device's `attestation.last_reason` (returned by `GET /devices`; the WebUI device detail page shows the attestation state and baseline).

## Boundaries

Attestation proves only that "the caller holds this device's AK and can produce a valid quote over the selected PCRs", not that the running system is uncompromised; a compromised runtime can still obtain the current PSK within the rotation window. PCR selection and AK creation also need per-model verification, and AMD fTPM and discrete TPMs behave differently. See the [Security Model](../../concepts/architecture/security.md) for details.

## What's next

- Back to the [Boot Chain](../boot-chain.md)
- How the first hop establishes trust: [First Hop](./first-hop.md)
- After attestation passes: [Data Plane](./data-plane.md)
- AK, quote format and trust anchors: [Firmware Contract](../../reference/firmware-contract.md)
