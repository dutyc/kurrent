# Devices

A device represents a physical machine and is the authoritative side of binding. It is identified by MAC and records the hardware fingerprint, TPM identity and attestation state; the machine itself carries no system, and the system disk and Worker identity survive machine replacement.

## Identity and Record

On first network boot, firmware reports an 11-field fingerprint (MAC, UUID, SMBIOS, CPU, memory, NIC and others) and the TPM AK public key; the control plane records it by MAC as the unique key.

| Field | Meaning |
| --- | --- |
| `mac` | Unique device identifier; the authority for binding |
| `uuid` / `fingerprint` | Self-reported hardware information used for identification and audit |
| `state` | `pooled` / `bound` / `revoked` |
| `bound_worker_id` | Bound Worker; empty when unbound |
| `identity` | TPM 2.0 AK public key, name and algorithm; claimed during the registration window and not replaceable online |
| `pcr_baseline` | Platform baseline digest over PCR 0/2/4/7 and its recording time; recorded on first successful attestation |
| `attestation` | `state` (`unproven` / `proven` / `mismatch`), last attestation time and reason code |
| `source` | `ipxe` (automatic report) or `manual` (manual registration/import) |

## Lifecycle

```text
pooled ──bind──► bound ──revoke──► revoked
  ▲                │
  └───unbind───────┘
```

- Pooling only means "this machine has been here" and grants no boot privilege; a device becomes a Worker's boot node only after binding.
- Automatic admission happens only while the registration window is open and the report carries a valid AK; once the window closes, reports from known devices still update the fingerprint, while unknown devices are neither pooled nor written to the ledger. Manual registration and revocation cleanup are not limited by the window.
- A `revoked` device no longer accepts reports; the dnsmasq binding file writes `MAC,ignore`, so a revoked device gets no lease.

## What's next

- Back to the [Entity Model](../entities.md)
- Binding and rebinding: [Binding and Placement](./binding.md)
- How the device proves itself: [Device Identity](../boot-chain/device-identity.md)
