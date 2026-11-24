# Provision Target Trust Stores

**Platform key enrollment** writes the Secure Boot platform keys (PK / KEK / db) into the machine firmware on the first network boot. The signed first hop only executes after UEFI verification, so the site signing certificate must enter the machine's `db` first; the platform enrollment window gives the fleet a network write channel, without per-machine import.

## Before you begin

- You built a signed image with `sbenroll` enrollment data ([Build Signed Firmware](./build-firmware.md)) and delivered it to the TFTP root or local media ([Deliver Signed Firmware](./deploy-firmware.md)).
- The machine is in Setup Mode or has Secure Boot off; machines locked to vendor keys with Secure Boot enabled cannot be written and use the vendor management flow.
- Enrollment runs on a controlled network: in Setup Mode any host on the same segment can write keys, so do not attach unmanaged devices while the window is open.

## What is written

`sbenroll` writes PK → KEK → db in order, with the variables and signed descriptors provided at image build time (`SB_PK_*` / `SB_KEK_*` / `SB_MS_UEFI_CA_CERT`):

- `db` contains the artifact signing certificate and Microsoft UEFI CA 2011.
- Private keys are kept offline; only public certificates and signed descriptors are embedded in the image.

## Steps

1. Open the platform enrollment window in the WebUI devices page (or `POST /api/cp/settings/platform-enrollment`).
2. Network-boot the machine from the signed image: `sbenroll` writes the keys, reports the result to `/devices/enroll-report`, and reboots automatically on the first `ok`.
3. After the reboot the firmware verifies and executes the same image; the MAC appears in the WebUI devices page platform enrollment list, which means enrollment is complete.
4. Close the window once all machines are enrolled: new machines no longer run enrollment, and enrolled devices and the list are kept.

> **Note:** Writing PK / KEK / db only makes the firmware accept the signed first hop. Joining the pool still requires a TPM AK, `sb=1` and the registration window; enrollment does not grant access.

## Result semantics

| `sbenroll` result | Behavior |
| --- | --- |
| `ok` (first) | Keys written; reboots after reporting |
| `ok` (already enrolled) | PK exists and is identical; variables untouched, boot continues |
| `foreign` | A different PK is installed; variables untouched, boot continues |
| `failed` | Write failed (or the image has no enrollment data); boot continues |

## Acceptance

- The machine reboots into the signed boot and reports `sb=1`.
- The MAC appears in the WebUI devices page platform enrollment list.
- Exporting the machine variables confirms the PK / KEK / db contents and write order.
- An already-enrolled machine re-runs without touching the variables; a machine with a foreign PK reports `foreign` and keeps its variables.

## Next steps

- Build the signed image: [Build Signed Firmware](./build-firmware.md)
- Join the pool and first boot: [Deployment](../setup/deployment.md)
- Trust anchors: [Boot Trust](../concepts/boot-chain/trust.md)
- Interface details: [Boot and Device Admission API](../reference/api/boot.md)
