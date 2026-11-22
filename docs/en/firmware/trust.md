# Boot Trust

Firmware establishes trust along three independent paths: the first-stage binary is verified by the target's UEFI, boot HTTPS trusts only the boot CA embedded at build time, and device identity is carried by TPM attestation. None replaces another; if any of them fails, the boot is blocked or credentials are withheld.

## Three Trust Anchors

| Object | Trust anchor | Failure consequence |
| --- | --- | --- |
| First-stage binary (iPXE) | Site signing certificate in the target's UEFI `db` | Unsigned or mismatched firmware never runs |
| Boot HTTPS (4443) | Boot CA embedded in the signed firmware | Server certificates outside that CA are rejected; there is no fallback to first-contact trust |
| Device identity | TPM AK and PCR 0/2/4/7 quote | No AK or a failing quote means no credentials |

## First-Stage Verification

UEFI verifies the signature of the first stage against `db` before executing it. The signing certificate is a one-time provisioning item: once written to the target's `db`, it takes no part in each boot and needs no reachable trust service. Unsigned artifacts are refused outright on this path; there is no branch that continues after a failed verification.

Provisioning steps and check commands are in [Secure Boot Provisioning](./secure-boot.md#provisioning-the-target-trust-store).

## Boot HTTPS

Signed firmware pins the boot CA through `TRUST=` and `CERT=` and accepts only certificates issued by that CA during the handshake. There is no first-contact trust and no switch to disable verification; rotating the boot CA is equivalent to rebuilding the firmware.

Unsigned debugging artifacts are the only form that uses TOFU: on first contact the SHA-256 of the leaf certificate DER is written to `server-fingerprint` in NVRAM and mirrored to `trust`; afterwards a mismatching fingerprint rejects the handshake and does not overwrite the pinned value. After the control plane rotates its certificate, debugging artifacts must run `clear nvo/server-fingerprint` before they can boot again. TOFU is a single global anchor, so all HTTPS targets must share one certificate or chain to it. Fields and behavior are in the [Firmware Contract](../reference/firmware-contract.md#tofu-unsigned-debugging-artifacts).

## Device Identity

Device identity is an attestation key (AK) inside the TPM; the private key is non-exportable and firmware never reads its plaintext. Every boot produces a SHA-256 quote over PCR 0/2/4/7 with the nonce written verbatim into `extraData`; the control plane verifies the signature, compares the platform baseline and confirms the nonce binding before injecting credentials into `/boot-vars`.

Platform-side identity semantics and failure reasons are in [Device Identity](../concepts/boot-chain/device-identity.md).

## Effect of Trust Changes

| Change | Action required |
| --- | --- |
| Replace the firmware signing key or certificate | Rebuild the signed artifacts and update `db` on every target |
| Replace the boot CA | Rebuild the signed artifacts; the boot CA fingerprint is embedded in the image |
| Change the boot HTTPS port, static scripts or an explicit control-plane address | Rebuild and sign |
| Update firmware or change Secure Boot policy | PCRs change, attestation is rejected as `pcr_mismatch`; review and approve the new baseline |
| Clear the TPM or replace the mainboard | The original AK is lost; delete the device record and re-admit it within a registration window |

## Boundaries

- Secure Boot covers only the first iPXE segment and boot HTTPS; the loader after `sanboot` is decided by the distribution signing chain on the master image's ESP.
- There is no rollback protection: a previously signed firmware still verifies, so rollback control depends on the release process.
- Some PXE firmware does not verify the downloaded network boot program; prefer local media or controlled HTTP boot where possible.

## What's next

- [Firmware Overview](./index.md)
- [Secure Boot Provisioning](./secure-boot.md)
- [Firmware Contract](../reference/firmware-contract.md)
- [Security Model](../concepts/architecture/security.md)
