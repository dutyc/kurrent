# Boot Chain

The boot chain answers three questions at power-on: under which identity, to which target, and with which disk. Kurrent does not write the answers into the machine or the disk; the control plane projects them at every boot according to the current binding.

This page describes the main path (NVMe/TCP with TLS 1.3). Firmware build and provisioning are covered under [Firmware](../firmware/index.md); trust boundaries are covered in the [Security Model](../architecture/security.md).

## Overview

```text
power on
  → DHCP (dnsmasq)       address, next-server
  → TFTP                 snponly.efi + boot.ipxe pointer
  → HTTPS + TOFU         boot.ipxe.cfg (all later scripts over HTTPS)
  → /devices/report      fingerprint + device public key
  → /devices/challenge   one-shot nonce
  → sign(nonce‖mac)      device private key
  → /boot-vars           verify and admit, then project disk NQN, host NQN, credentials
  → menu.ipxe            default disk selected on timeout
  → sanboot nvme://...   NVMe/TCP 4421, DH-HMAC-CHAP + TLS 1.3
  → NBFT / NBCT          firmware hands connection data and keys to initramfs
  → kernel reconnect → rootfs → login
```

The split is explicit: firmware provides protocol drivers, device-key commands, NVRAM and TOFU; the platform repository owns boot scripts and admission decisions.

## 1. Network Boot

The device gets an address and `next-server` over DHCP, then downloads two things over TFTP:

- `snponly.efi`: the first-stage firmware.
- `boot.ipxe`: a pointer that performs one HTTPS redirect.

From `boot.ipxe.cfg` onward, every script, menu and device endpoint uses HTTPS. The server certificate is self-signed and generated at control-plane startup; the device pins its fingerprint on the first handshake (TOFU).

The TFTP root exposes only these two files. Restart the dnsmasq container after replacing `snponly.efi`: the file-level mount pins the inode.

## 2. Device Identity

On first boot, firmware runs `keygen`, generating an ECDSA P-256 private key in EFI NVRAM; the key never leaves the machine. Later boots refuse to overwrite an existing key.

`pubkey` exports the public key, and firmware reports to `/devices/report`:

- the 11-field fingerprint (MAC, UUID, SMBIOS, CPU, memory, NIC and others);
- the device public key.

The control plane records the device in the pool by MAC. **Reporting is not authorization**:

- an unknown MAC is admitted only while the registration window is open and a valid public key is present;
- a pooled but unbound device receives no boot credentials;
- once the window closes, reports only update fingerprints and add no entries.

The device key can be rotated online: the old key signs `rotate‖mac‖new_pubkey‖nonce`, and is invalidated immediately on success.

## 3. Challenge-Response

With binding and signature enforcement in place, knowing a MAC is not enough to obtain credentials. The device requests a one-shot nonce from `/devices/challenge` and signs:

```text
nonce ‖ mac          (no separator; ECDSA P-256 / SHA-256)
```

The nonce is single-use and bound to the MAC; `report`, `challenge` and `boot-vars` are rate-limited per source IP. The signature is submitted with `/boot-vars`, and the control plane verifies it against the device public key.

## 4. Admission and Variable Projection

`/boot-vars` is the most important decision on the boot chain. The control plane checks, in order:

1. **Resolution**: the request MAC is resolved to a Worker through the device ledger; hostname is only a fallback when no MAC is supplied or the ledger has no hit.
2. **Binding is authentication**: the requesting MAC must belong to the matched Worker's bound device; another device cannot claim the Worker's boot identity.
3. **Signature verification**: with enforcement on, no key, no signature or a bad signature is rejected.
4. **Credential conditions**: the Worker is bound and a key entry exists; the nvmet backend also requires the storage node's data-plane port to be ready. If an nvmet condition fails, the payload is withheld; non-nvmet backends project no NVMe-oF credentials.

After these checks, projection depends on whether the device completed signature verification: credentials go only to verified devices, while non-credential variables may be served in transitional mode.

| Variable | Content |
| --- | --- |
| `disk-nqn` | Full NQN of the default disk |
| `storager-ip` | Storage node holding the disk |
| `hostnqn` | Initiator Host NQN for the Worker |
| `nbft-secret` | DH-HMAC-CHAP authentication key |
| `nbft-tls-psk` / `nbft-tls-port` | TLS PSK derived from the key, and the data-plane port |
| `menu-default` | Boot entry selected after the menu timeout |

With no default disk, `menu-default=reboot` is returned; a pooled but unbound device also loops on reboot until bound. On nvmet, a missing key or an unready port withholds the payload; non-nvmet backends project no NVMe-oF key or PSK. No path assembles a plaintext URI.

## 5. Menu

`menu.ipxe` is loaded over HTTPS and shows the OS label of the current default disk:

- the default disk boots after the timeout;
- to switch system disks, set another disk as default in the Worker detail page in the WebUI; it takes effect on the next boot;
- the boot entry comes from the ledger projection, not from configuration inside the disk.

## 6. Data-Plane Connect

The `sanboot` target looks like:

```text
nvme://<storager>:<tls_port>/<disk_nqn>?secret=<dhhc1>&tls_psk=<psk>&tls_trsvcid=<tls_port>
```

The connection has two layers:

- **Authentication**: DH-HMAC-CHAP completes connection control, answering "who may connect".
- **Encryption**: TLS 1.3 protects content on the same port `4421`.

Firmware performs the TLS handshake itself. Authentication failure, a missing PSK or an unready port fails the connection; there is no plaintext fallback port.

## 7. Hand-off to the Operating System

At `sanboot`, firmware installs an **NBCT** credentials table (an ACPI table) recording for this session:

- target address and port;
- subsystem NQN and host NQN;
- DH-HMAC-CHAP key.

The TLS PSK and data-plane port travel with the `sanboot` URI; NBFT carries network and subsystem discovery.

The initramfs `nbft-connect` consumes **NBFT** (network and subsystem discovery) and **NBCT** (credentials and host NQN relay), reconnects on the same port with `--tls`, and mounts the root filesystem.

No machine identity is written into the disk: identity, credentials and connection data come from this boot's projection.

> Current status: NBCT is produced by firmware; iPXE-side NBFT generation is still pending. The verified whole-machine chain injects an NBFT table to emulate the "firmware pre-generated" stage.

## Failure Paths

| Symptom | Cause |
| --- | --- |
| Device never enters the pool | Registration window closed; report lacks a valid public key; device revoked |
| Device pooled but cannot boot | No Worker binding; or the Worker has no system disk |
| Device reboots repeatedly | The Worker has no default disk; or the pooled device is not yet bound |
| "No key / no signature" after enforcement | Device has not claimed its key; boot script did not obtain a nonce |
| Boot variables withheld | Worker has no credential; backend is not nvmet; data-plane port is not ready |
| Reboot after the menu timeout | No default disk set |
| Data-plane connect fails | PSK not in the storage node keyring; `tlshd` version or kernel TLS support missing |
| HTTPS handshake fails | The control-plane certificate was rotated while the device still pins the old fingerprint |

## Trust Boundary

The first hop (DHCP → TFTP `snponly.efi` → TFTP `boot.ipxe`) has no cryptographic protection: device firmware does not verify it, and iPXE has not yet obtained the server certificate. Deployments treat the PXE network itself as the trust boundary, while retaining:

- device-identity TOFU (NVRAM key + registration window + binding);
- server TOFU;
- challenge-response and binding-as-authentication;
- data-plane encryption with no plaintext fallback.

Attackers on the same Layer 2, a compromised host or physical access are outside this boundary. See the [Security Model](../architecture/security.md) for the full threat model and upgrade paths.

## Firmware Split

| Capability | Provided by |
| --- | --- |
| DHCP/TFTP client, HTTPS, TOFU, NVRAM | Firmware |
| `keygen` / `pubkey` / `sign` | Firmware |
| NVMe/TCP driver, DH-HMAC-CHAP, NBCT | Firmware |
| Boot scripts, registration window, `/boot-vars` admission | Platform control plane |
| System disks, credentials, menu generation | Platform control plane |

Firmware build, artifacts and device provisioning are covered in the [Firmware Overview](../firmware/index.md) and [Provisioning](../firmware/provision.md).
