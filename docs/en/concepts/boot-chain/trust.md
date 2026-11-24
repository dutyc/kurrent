# Boot Trust

The boot chain establishes trust in three independent stages: the first-hop binary is verified by the machine's UEFI, HTTPS during boot trusts only the boot CA embedded in the signed image, and device identity is carried by TPM attestation. If any stage fails, boot stops at that stage and does not fall back to another path.

| Stage | Trust anchor | Verified when | Failure result |
| --- | --- | --- | --- |
| First-hop binary | The site signing certificate in the machine's `db` | When the firmware loads the TFTP first hop | Unsigned or tampered images do not execute |
| Boot HTTPS | The boot CA embedded in the signed image | The 4443 boot HTTPS handshake | Scripts and variables are refused, with no first-contact-trust fallback |
| Device identity | TPM AK and the PCR 0/2/4/7 baseline | The nonce + quote on every boot | No credentials are issued |

## First-hop verification

UEFI validates the first hop against `db` before executing it. The site signing certificate is provisioned once: on the first network boot the platform enrollment window writes it into `db` (machines locked to vendor keys use the vendor flow), after which it does not participate in each boot and does not depend on the control plane being online. See [Provision Target Trust Stores](../../tasks/enroll-platform-keys.md) for the flow.

The first hop signs the static script, the boot CA and the boot HTTPS port into the binary, and the controller address is not embedded by default. The script, the trust anchor and the port therefore cannot be replaced individually; changing any of them requires a rebuild and re-signing.

## Boot HTTPS

The signed image pins the boot CA with `TRUST=` and `CERT=`, so the handshake only accepts certificates issued by that CA. There is no first-contact trust and no switch to disable verification; rotating the boot CA is equivalent to rebuilding the firmware.

The unsigned debug artifact is the only form that uses first-contact trust (TOFU): on the first connection it stores the SHA-256 of the leaf certificate DER in the NVRAM `server-fingerprint` and mirrors it to `trust`; afterwards a fingerprint mismatch refuses the handshake and never overwrites the stored fingerprint. After a control-plane certificate rotation the debug artifact must run `clear nvo/server-fingerprint` to re-pin, or boot fails. Fields and behavior are in the [Firmware Contract](../../reference/firmware-contract.md#tofu-unsigned-debugging-artifacts).

## Device identity

Device identity is an attestation key (AK) inside the TPM: the private key is created inside the TPM, is not exportable, and the firmware never reads it in the clear. On every boot the firmware produces a SHA-256 quote over PCR 0/2/4/7 with the nonce written verbatim into `extraData`; the control plane verifies the signature, compares the PCR digest with the platform baseline and confirms the nonce binding before injecting credentials in `/boot-vars`. See [Device Identity](./device-identity.md) for the mechanics.

## Control-plane artifacts

The control plane generates the boot-chain certificates idempotently at startup, separate from the three component roots:

- `state/pki/ca/boot/ca.crt|ca.key`: the boot CA, 10 years, signs only boot endpoint certificates.
- `state/certs/boot-server.crt|boot-server.key`: the 4443 leaf certificate, 1 year, reusing `spec.serverCert.san`; it is re-issued automatically when fewer than 30 days remain, without rotating the boot CA.

The nginx 4443 server only reverse-proxies the boot endpoints (`/boot/phase`, `/devices/enroll-report`, `/boot-vars`, `/devices/report`, `/devices/challenge`), does not require a client certificate, and returns 404 for unknown paths; the management plane, the WebUI and `/api/cp/` are not served on that port.

The host port is adjusted with `KURRENT_BOOT_HTTPS_PORT` and must match the firmware build's `BOOT_HTTPS_PORT`: the port is embedded in the signed image, and `/api/cp/boot-assets` checks this with `secureboot.port_matches`.

## Effect of trust changes

| Change | Required action |
| --- | --- |
| Replace the firmware signing key or certificate | Rebuild the signed artifacts and re-enroll `db` on the machines |
| Replace the boot CA | Rebuild the signed artifacts; the boot CA fingerprint is embedded in the image |
| Change the boot HTTPS port, the static script or the explicit controller address | Rebuild and re-sign |
| Update firmware or change the Secure Boot policy | PCRs change, attestation is refused with `pcr_mismatch`, approve the new baseline after review |
| Clear the TPM or replace the motherboard | The original AK is lost; delete the device record and re-enroll within the registration window |

## Boundaries

- Secure Boot covers only the first-hop binary and boot HTTPS; the loader after `sanboot` is decided by the distribution signing chain on the master image's ESP.
- No rollback protection: an older signed firmware still passes verification, and rollback is controlled by the release process.
- Some PXE firmwares do not verify the downloaded network boot program; prefer local media or controlled HTTP boot where available.

## Next steps

- Back to [Boot Chain](../boot-chain.md)
- Build the signed image: [Build Signed Firmware](../../tasks/build-firmware.md)
- Write the target trust store: [Provision Target Trust Stores](../../tasks/enroll-platform-keys.md)
- Interface and variables: [Firmware Contract](../../reference/firmware-contract.md)
- Threat model: [Security Model](../architecture/security.md)
