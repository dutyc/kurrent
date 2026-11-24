# Deployment Prerequisites

Deployment requires the following host, network and device prerequisites. The procedure is in [Deployment](../setup/deployment.md), the port table is in [Architecture](../concepts/architecture/index.md#ports), and security boundaries are in the [Security Model](../concepts/architecture/security.md).

## Host roles

| Role | Minimum requirements | Responsibility |
| --- | --- | --- |
| Control-plane server (×1) | Linux x86_64; Docker and Compose v2; Node.js 22.23.4+, 24.22.1+ or 26.12.1+ and npm (WebUI build); one PXE-side interface (dedicated NIC or VLAN subinterface) | DHCP/TFTP, 443 WebUI/API, PKI, device ledger and boot variables |
| Storage node (×1 or more) | Linux x86_64; Docker and Compose v2; kernel NVMe/TCP TLS support; `tlshd >= 1.5.0`; one btrfs/xfs data disk | Agent (4840) and nvmet-host (4421), hosting master images and system disks |
| Bare-metal device (×N) | x86_64 UEFI; TPM 2.0; Secure Boot capable; UEFI network boot (SNP), detailed below | Enters the pool over PXE and boots a system disk |

Co-location does not merge the roles; it still runs one Agent.

The control plane must have an address on the PXE segment; WebUI, API and Agent callbacks use management-network addresses.

Bare-metal devices support UEFI boot only; Legacy/CSM is no longer supported (artifacts are UEFI-only).

## Bare-metal device requirements

These requirements apply to the `main` line; devices without a TPM use the `legacy/no-tpm` line described in [Repositories and Branches](../reference/branches.md), which does not require a TPM or Secure Boot. All four requirements must hold on the `main` line:

| Item | Requirement | If missing |
| --- | --- | --- |
| Architecture and firmware | x86_64; UEFI boot only, with CSM/Legacy disabled | No matching artifact exists, so the boot chain cannot start |
| TPM | TPM 2.0 (discrete TPM or Intel PTT / AMD fTPM); can create an ECC P-256 or RSA-2048 AK; SHA-256 PCR 0/2/4/7 readable | Registration is rejected (`no_tpm`); failed attestation withholds credentials |
| Secure Boot | Not locked to vendor keys (Setup Mode or can be disabled); the first network boot writes PK / KEK / db through the platform enrollment window, and db includes the site certificate and the Microsoft UEFI CA | Registration is rejected (`sb_disabled`); an unsigned first hop never runs |
| Network boot | UEFI IPv4 network boot (DHCP + TFTP) with a UEFI SNP stack on the NIC | The first hop cannot be downloaded; NICs without a UEFI SNP must use the all-driver first hop (`boot_firmware=full`) |

- A local disk is not a prerequisite; the device holds no persistent state.
- CPU and memory only need to meet the installed distribution's requirements.
- Device identity (AK) is bound to the mainboard and TPM: clearing the TPM or replacing the mainboard is equivalent to replacing the device and requires re-admission.
- USB and wireless NICs are not used for boot; NIC compatibility is in [Firmware Platform Reference](../reference/firmware-platform.md).
- Platform key enrollment is covered in [Provision Target Trust Stores](../tasks/enroll-platform-keys.md); machines locked to vendor keys use the vendor flow.

Verify on the target:

```bash
ls /sys/firmware/efi                           # UEFI boot
mokutil --sb-state                             # Secure Boot state
tpm2_getcap properties-fixed | grep -i family  # TPM version
tpm2_getcap pcrs | grep -i sha256              # SHA-256 PCR bank
```

Whether a NIC provides an SNP stack can only be confirmed in UEFI setup or from vendor documentation; the commands require `mokutil` and `tpm2-tools`.

## Network and ports

- **Management plane**: browsers and storage nodes reach 443.
- **Boot plane**: devices reach DHCP (67/UDP), TFTP (69/UDP) and boot HTTPS (4443); `spec.networking.interface` binds this side.

Prefer a dedicated NIC or VLAN for each side; with one NIC the PXE segment must be isolated.

Devices must be able to route to the storage node's 4421. The full port table is in [Architecture](../concepts/architecture/index.md#ports).

## Host dependencies

Both hosts need Docker, Compose v2, `git`, `curl`, `jq`, `openssl` and Go 1.27+ (to build the CLI); the control plane additionally needs Node.js 22.23.4+, 24.22.1+ or 26.12.1+ and npm. Installation commands are in [Deployment](../setup/deployment.md#host-dependencies).

## Storage-node kernel and tlshd

- The kernel needs `CONFIG_NVME_TCP_TLS`, `CONFIG_NVME_TARGET_TCP_TLS` and `CONFIG_NVME_KEYRING`.
- `tlshd >= 1.5.0` must be installed and running; older versions let an old PSK shadow the new one.
- If the packaged version is below 1.5.0, build from source per `storager/nvmeof/nvmet-host/README.md`.
- If the 4421 listener cannot be created, the node reports the port as unready and the control plane refuses the boot without falling back to plaintext.

`host-prepare.sh` requires root and idempotently loads modules, mounts configfs and persists both; diagnose with `kurrent host check --role storage`.

## Data disk

- btrfs and xfs support reflink, making clones close to instant; ext4 falls back to a full copy.
- Record it in `/etc/fstab` (UUID) or a systemd mount, or `diskDir` resolves to the root filesystem.

## Host baseline

The control plane should expose only 22, 53/67/69 and 80/443; storage nodes additionally expose 4840/4421.

- The control-plane management plane has no TCP listener; nvmet-host uses a shared-volume UDS.
- Control-plane containers do not mount `docker.sock`.
- CA private keys, `users.yml`, `sessions.yml`, `admin.conf` and `audit.jsonl` are 600.
- ufw does not filter Docker-published ports; restrict inbound traffic in the `DOCKER-USER` chain.

`sudo kurrent host check` (`--role control|storage` when split, `all` when co-located); `--strict` counts SKIP as failure. The full checklist is in the [Security Checklist](../setup/deployment.md#security-checklist) and [Operations](../tasks/operations.md#security-baseline).

## What's next

- [Deployment](../setup/deployment.md)
- [Architecture](../concepts/architecture/index.md#ports)
- [Security Model](../concepts/architecture/security.md)
- [Configuration](../reference/configuration.md)
