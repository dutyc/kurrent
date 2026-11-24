# Repositories and Branches

Kurrent's code lives in two repositories: the main repository [kurrent](https://github.com/dutyc/kurrent) carries the control plane, storage nodes, CLI, WebUI and boot scripts; the firmware repository [kurrent-firmware](https://github.com/dutyc/kurrent-firmware) builds device firmware. Default deployment uses the `main` branch of both; devices without a TPM use the `legacy/no-tpm` branch of both. The other branches are research or archival lines and are not deployment paths.

## Main repository

| Branch | Contents | Status |
| --- | --- | --- |
| `main` | Deployment line. Device identity is a TPM 2.0 AK; each boot submits a quote over PCR 0/2/4/7 that is checked against the platform baseline. Data plane is NVMe/TCP with TLS 1.3 on a single port `4421` | Actively updated |
| `legacy/no-tpm` | The line without a TPM. Device identity is an ECDSA P-256 public key registered at enrollment, and boot verifies a one-shot nonce signature; no platform attestation and no Secure Boot signed first hop | Deployment option |
| `iscsi-classic` | The iSCSI prototype from 2026-08: iPXE + iSCSI boot, control plane on `4838`, API on `4839`, stgt/lio backend | Archived; historical reference |

## Firmware repository

| Branch | Contents | Status |
| --- | --- | --- |
| `main` | Deployment line. Provides TPM 2.0 attestation and the Secure Boot signed first hop; `build-sb.sh` produces `dist/secureboot/snponly-sb.efi` | Actively updated |
| `legacy/no-tpm` | Firmware paired with the same-named main-repository branch: software device keys and TOFU, unsigned | Optional |
| `research/nvme-of` | Early research on NVMe-oF boot | Research |
| `legacy-iscsi` | Firmware from the iSCSI era | Archived |

Same-named branches are used together: `main` admits devices by TPM AK and PCR baseline, and boot does not complete when the firmware side lacks an AK, a quote or the signed first hop; `legacy/no-tpm` verifies the registered device key and uses neither a TPM nor a signed first hop. See [Boot Trust](../concepts/boot-chain/trust.md) and [Security Model](../concepts/architecture/security.md) for the trust chain.
