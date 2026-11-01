# Firmware Overview

Kurrent's device firmware is built by the separate [kurrent-firmware](https://github.com/dutyc/kurrent-firmware) repository. The repository does not carry iPXE source; it maintains difference patches and build assets on top of the pinned upstream baseline `e6e51ccb`.

Firmware handles the device side of the boot path: NVMe/TCP SAN boot, DH-HMAC-CHAP authentication, NBCT credential handoff, device identity keys, and TOFU. Registration windows, Worker binding, system disk selection, and credential issuance belong to the control plane.

```text
firmware repo dist/ artifacts
        │
        ▼ deploy
platform repository tftp/snponly.efi
        │
        ▼ device network boot
control plane /boot-vars + data plane
```

## Responsibility split

| Repository | Responsibility | Deliverable |
| --- | --- | --- |
| kurrent-firmware | Protocol drivers, authentication, device information collection, NIC adaptation, firmware build | `dist/` artifacts, patch set, test tooling |
| kurrent (platform) | Control plane, boot scripts, registration window, identity and credential projection, system disks | Control-plane service, `tftp/` scripts, API |

Firmware is unaware of business policy: registration windows, Worker binding, disk selection, and credential issuance live in the platform repository. Firmware provides commands and protocol capabilities; boot scripts decide the call order.

## Capabilities

- **NVMe/TCP SAN boot** — Patch 0005; native `nvmetcp` driver covering ICReq negotiation, Connect, Property Set, Identify, I/O queues, and R2T reads/writes.
- **DH-HMAC-CHAP authentication** — Patch 0005; one-way authentication; hashes SHA-256/384/512, DH groups 0/2048/3072/4096.
- **NBCT credential table** — Patch 0009; hands the session key, Host NQN, and TLS handoff parameters to the kernel, avoiding a second credential fetch.
- **TLS 1.3 PSK data plane** — Patch 0010; firmware-segment `sanboot` establishes TLS 1.3 with the PSK; together with the kernel segment this covers the full path.
- **Host NQN setting** — Patch 0005; injects `hostnqn` per MAC, supporting nvmet `allow_any_host=0` strict mode.
- **Device information collection** — Patch 0004; reports SMBIOS identity and memory, CPUID, and NIC fields over HTTP.
- **EFI NVS persistence** — Patch 0006; stores `device-key` and `server-fingerprint` in NVRAM across reboots.
- **TOFU trust** — Patch 0007; pins the server leaf certificate SHA-256 fingerprint on the first HTTPS handshake and rejects mismatches.
- **Device identity commands** — Patch 0008; `keygen` / `pubkey` / `sign` / `nvclear`.
- **NIC adaptation** — Patches 0001-0003; native RTL8125/8126 drivers and a `snponly` local-boot fallback.
- **Local-media boot** — `embed/auto.ipxe`; the `direct-uefi` artifact embeds a script that enters the network boot chain immediately.

## Patch set

Patches are applied in filename order; if any patch fails against the pinned baseline, the build fails. The current chain has ten patches:

1. **0001 `realtek-adaptation`** — Native driver adaptation for the RTL8125 family and RTL8126
2. **0002 `makefile-ipxe-debug`** — Debug targets inherit the full NIC driver set
3. **0003 `snponly-local-boot`** — Local-media boot falls back to SNP/NII/MNP devices
4. **0004 `device-info-collection`** — SMBIOS memory fields and PCI NIC chip names
5. **0005 `nvmeof-adaptation`** — NVMe/TCP SAN driver, DH-HMAC-CHAP, authentication timing fixes, and `hostnqn`
6. **0006 `efi-nvs-backend`** — EFI variable NVS backend, including empty-block deletion semantics
7. **0007 `tofu-fingerprint`** — TOFU fingerprint pinning and verification
8. **0008 `devicekey-commands`** — ECDSA P-256 device identity commands
9. **0009 `nvmetcp-nbct-acpi-table`** — NBCT ACPI table and kernel credential handoff
10. **0010 `tls13-psk`** — TLS 1.3 PSK client and record layer

Besides the patches, `embed/auto.ipxe` is a build-level EMBED asset compiled into the `direct-uefi` artifacts via `EMBED=`; it is not part of the patch sequence. Licensing differs across the set: the RTL8125/8126 adaptation is GPL-2.0 only and may not be redistributed under UBDL; the other patches follow their file-header terms. Read `patches/README` before redistribution.

## Device information

Firmware collects four groups of settings and reports them over HTTP GET:

| Group | Settings | Source |
| --- | --- | --- |
| Identity | `uuid`, `manufacturer`, `product`, `serial`, `board-serial`, `asset` | SMBIOS types 1/2/3 |
| CPU | `cpuvendor`, `cpumodel` | CPUID |
| Memory | `mem-total`, `mem-type`, `mem-speed` | SMBIOS type 17 (patch 0004) |
| NIC | `netX/mac`, `netX/chip`, `netX/busid`, `netX/bustype`, `netX/linktype` | netdev settings; `chip` enabled by patch 0004 |

Numeric settings follow the iPXE convention and are expanded as hexadecimal; the server must accept both hexadecimal and decimal parsing. Strings may contain spaces and special characters, so confirm URL encoding before reporting. Environments without SMBIOS type 17 do not provide memory fields; under SNP boot `${netX/chip}` reports `snpnet`, and only native drivers report the chip model. See the [firmware contract](../reference/firmware-contract.md) for field-level details.

## NIC support

Adaptation focuses on 2.5G/5G and faster NICs; gigabit and Fast Ethernet remain at upstream baseline support and receive no new adaptation. The following reflects the driver device tables and measurements of the pinned baseline plus custom patches; the range may change after an upstream upgrade.

### Native drivers

- **Intel e1000e family (82540 through the latest I219 stepping)** — `intel.c`; verified on physical hardware (`direct-uefi`).
- **Intel I210 / I211 / I350 / 82576** — `intel.c`; includes the common server I350.
- **Intel I225 / I226 (2.5G)** — `intel.c`; all steppings covered.
- **Intel 82599 / X540 / X550 / X553 / X552 (10G)** — `intelx.c`; includes X553-AT.
- **Intel X710 / XL710 (10G / 40G)** — `intelxl.c`; common on servers.
- **Intel E810 / E823 (25G / 100G)** — `ice.c`; newer driver.
- **Realtek RTL8136 / 8139 / 8129 / 8167 / 8168 / 8169** — `realtek.c`; upstream baseline.
- **Realtek RTL8125 family (2.5G)** — `realtek.c` (0001); physical DHCP verified.
- **Realtek RTL8126 (5G)** — `realtek.c` (0001); compiles; hardware validation pending.
- **Marvell/Aquantia AQC100-114 (10G / 5G / 2.5G)** — `aqc1xx.c`; full device-table coverage.
- **Broadcom BCM57xx gigabit** — `tg3`; broad coverage.
- **Broadcom BCM5706/5708 and similar** — `bnx2`; older 1G/10G family.
- **Broadcom NetXtreme-E (BCM957xxx)** — `bnxt`; newer server family.
- **virtio / vmxnet3 / ENA / GVE / netvsc** — virtualized environments.
- **AX88179, LAN7800, SMSC95xx/75xx, DM96xx, CDC ECM/NCM** — the only supported USB NIC classes.

### Protocol layer

When a server NIC ships a UEFI SNP stack, no in-firmware driver is required; use `snponly.efi` directly. This path covers UEFI only; legacy BIOS/UNDI artifacts are not in the current build manifest.

| NIC | Path | Notes |
| --- | --- | --- |
| Mellanox ConnectX-4 / 5 / 6 / 7 (25G-100G) | `pxe-uefi/snponly.efi` or `direct-uefi/snponly.efi` | Zero adaptation; subject to the SNP iSCSI hang defect |
| Broadcom bnx2x family (BCM57710/57711/57712/57800/57810/57840) | Same | Card carries an official boot ROM; subject to the SNP iSCSI hang defect |

### Unsupported

| NIC | Scenario | Notes |
| --- | --- | --- |
| Realtek USB RTL8152 / 8153 / 8156 | USB-C to RJ45 | No iPXE driver |
| Modern WiFi such as Intel AX and Realtek 88 series | Laptop wireless | Only legacy prism2/rtl818x |

### Known issues

- **Some Realtek RTL8168 revisions**: native driver initialization hangs; fall back to `snponly`.
- **SNP iSCSI hang defect**: affects every NIC that can only use the firmware SNP driver; the `snponly` path carries the same risk.
- **Intel I219**: requires `INTEL_PBSIZE_RST` / `NO_PHY_RST` handling, a historical defect fix.
- **Marvell/Aquantia AQC**: official lightweight implementation; link depends on hardware autonegotiation. AQC113 dynamic firmware loading is not implemented and returns `-ENOTSUP` when the firmware is blank.
- **82599 / X550 (SFP+)**: compatibility depends on optical module quality.

### Measurements

| Date | NIC | Firmware | Result |
| --- | --- | --- | --- |
| 2026-08-07 | Realtek RTL8125 | SNP firmware | iSCSI mount hangs |
| 2026-08-09 | Realtek RTL8168 | Native driver | Some revisions hang during initialization; SNP fallback |
| 2026-08-10 | Intel e1000 (82540EM and similar) | `direct-uefi` | Passed |
| 2026-08-11 | Realtek RTL8125 | Custom firmware (0001) | Works |

Selection order: prefer NICs covered by native drivers; for server NICs without a native driver but with a UEFI boot ROM, use the `snponly` artifact and assess the SNP iSCSI hang risk; validate RTL8126 on the target motherboard before production; do not use USB NICs or WiFi for diskless boot.

## Device identity and trust

Firmware provides the device identity key, NVRAM persistence, and TOFU trust; registration, challenge-response, and authorization are orchestrated by the control plane boot script.

| Command | Behavior | Failure path |
| --- | --- | --- |
| `keygen` | Generates an ECDSA P-256 private key from the EFI RNG and writes `device-key` | Refuses to overwrite an existing key and exits non-zero |
| `pubkey` | Exports the 130-hex-digit uncompressed public key `04‖X‖Y` and writes the temporary setting `pubkey` | Errors when `device-key` is absent |
| `sign <data...>` | Concatenates arguments without separators, hashes with SHA-256, signs with ECDSA P-256, outputs base64(DER), and writes the temporary setting `sig` | Refuses to sign without `device-key` |
| `nvclear` | Clears `device-key`, `server-fingerprint`, and `trust`, reporting `cleared` / `not present` / `could not clear` per item | Prints `incomplete` on the last line and exits non-zero if any item fails |

`pubkey` and `sig` are valid only for the current boot and are not persisted.

| Setting | Type | Persistence | Source |
| --- | --- | --- | --- |
| `device-key` | hexraw | NVRAM | `keygen` |
| `server-fingerprint` | hexraw | NVRAM | TOFU |
| `trust` | hex | NVRAM | Root-certificate override for the TOFU image |
| `pubkey` | hexraw | Memory | `pubkey` |
| `sig` | string | Memory | `sign` |

Persistent settings are wrapped tags; clearing them requires the `nvo/` prefix, for example `clear nvo/device-key`. A bare `clear device-key` only clears the memory copy. Use `nvclear` to clear all three at once.

Signature data:

```text
data = nonce ‖ mac
signature = base64( DER( ECDSA-P256( SHA-256( data ) ) ) )
```

`nonce` is issued once by the control plane; `mac` is the boot NIC MAC in lowercase colon form. The hostname is not signed; identity binding relies on the MAC-to-device-ledger binding and the one-time nonce. The public key is the uncompressed point `0x04‖X(32B)‖Y(32B)`, the signature is ASN.1 DER, and transport uses standard base64.

TOFU trust:

| Phase | Condition | Behavior |
| --- | --- | --- |
| First contact | No `server-fingerprint` stored | Accept the handshake and store the SHA-256 fingerprint of the leaf certificate DER |
| Pinned and matching | Fingerprint matches | Download normally |
| Pinned and mismatching | Fingerprint differs | Reject the connection and keep the existing fingerprint |

TOFU is a global single anchor: once `server-fingerprint` exists, firmware trusts only the certificate pinned during the first HTTPS connection. Every HTTPS target must share that anchor or chain to that certificate; use TFTP/HTTP for other traffic. After rotating the control plane certificate, clear the NVRAM fingerprint first, otherwise the device cannot connect again.

Lifecycle:

1. First boot runs `keygen`; later boots skip because the key exists.
2. `pubkey` exports the public key, which is reported with device information to register the device.
3. The control plane issues a one-time nonce; the device runs `sign ${nonce}${net0/mac}` and returns the signature.
4. After verification, the control plane projects boot variables and the device proceeds to boot.
5. Replacing hardware creates a new device identity; the system disk and Worker identity remain in the control plane and move through device rebinding.
6. Reset identity with `nvclear` or `clear nvo/device-key`; the device must then register again.

Security boundaries:

- **The private key does not leave the device**: `device-key` is used only inside firmware; the public key and signatures may be exported.
- **A booted system can read the private key**: the key lives in an EFI variable with runtime access, so the running OS can read it. Secure Boot does not close this read path; see the [security model](../architecture/security.md).
- **First contact is unauthenticated**: TOFU accepts the first certificate inside the registration window; PXE segment isolation carries the man-in-the-middle risk.
- **Nonces must be one-time**: replay requires a repeated nonce; the control plane must keep nonces one-time, short-lived, and bound to the MAC.

## NVMe-oF boot

Firmware includes a native `nvmetcp` driver for NVMe over TCP SAN boot. The NQN and address below are for local validation and use the production format `nqn.2026-07.com.kurrent:worker-01.ubuntu.0123456789ab`; production disk NQNs are generated by the control plane from `spec.agent.nqnBase`, default prefix `nqn.2026-07.com.kurrent`. The NQN in the `sanboot` URI must match the actual target subsystem.

- Protocol flow: ICReq/ICResp negotiation → Connect (Admin) → authentication → Property Set (CC.EN=1) → Identify (controller/namespace) → I/O queue Connect → block reads and writes.
- Authentication: one-way DH-HMAC-CHAP, hashes SHA-256/384/512, DH groups 0/2048/3072/4096.
- TCP transport only; RDMA and FC are not supported.
- The boot path uses a single controller and a single namespace; Identify parses both 512 B and 4 KB blocks.

### Target configuration

Without authentication:

```bash
NQN="nqn.2026-07.com.kurrent:worker-01.ubuntu.0123456789ab"
IMG=/path/to/backing.img
SYS=/sys/kernel/config/nvmet/subsystems/$NQN
PORT=/sys/kernel/config/nvmet/ports/1

mkdir -p "$SYS"
echo 1 > "$SYS/attr_allow_any_host"
mkdir -p "$SYS/namespaces/1"
echo -n "$IMG" > "$SYS/namespaces/1/device_path"
echo 1 > "$SYS/namespaces/1/enable"

mkdir -p "$PORT"
echo ipv4 > "$PORT/addr_adrfam"
echo tcp  > "$PORT/addr_trtype"
echo 127.0.0.1 > "$PORT/addr_traddr"
echo 4420 > "$PORT/addr_trsvcid"
ln -sf "$SYS" "$PORT/subsystems/$NQN"
```

Place the backing file in a directory with sufficient permissions and space; some environments restrict `/tmp`.

The DH-HMAC-CHAP key format is `DHHC-1:XX:<base64(key + CRC32)>`:

```bash
python3 -c 'import zlib,base64; k=b"0123456789abcdef0123456789abcdef"; \
print("DHHC-1:01:"+base64.b64encode(k+zlib.crc32(k).to_bytes(4,"little")).decode())'
```

`XX` must be two digits; `01` means SHA-256. CRC32 must use the standard final value (including the final XOR) and is appended little-endian after the key. Incorrect prefix length or CRC makes nvmet reject the key with `base64 key decoding error -1` or `dhchap status 2`.

Linux 7.0+ configfs layout:

```bash
HOSTNQN="nqn.2014-08.org.ipxe:ipxe"
HOST_DIR=/sys/kernel/config/nvmet/hosts/$HOSTNQN

echo 0 > "$SYS/attr_allow_any_host"
mkdir -p "$HOST_DIR"
echo -n "$KEY" > "$HOST_DIR/dhchap_key"
echo ffdhe4096 > "$HOST_DIR/dhchap_dhgroup"
ln -sf "$HOST_DIR" "$SYS/allowed_hosts/$HOSTNQN"
```

`allowed_hosts/` uses symlinks, not directories; `attr_allow_any_host=1` makes the kernel skip authentication. Older kernels expose a different authentication interface; follow the actual kernel documentation.

#### Server-side error reference

| Symptom | Cause | Action |
| --- | --- | --- |
| `base64 key decoding error -1` | The `DHHC-1:XX:` prefix is shorter than 10 bytes | Keep the two-digit prefix |
| `dhchap status 2` | The key CRC32 is not the final value | Regenerate with `zlib.crc32` |
| Unauthenticated connection succeeds | `attr_allow_any_host=1` | Set it to 0 and recreate the subsystem |
| `Can't set allow_any_host when explicit hosts are set!` | Stale `allowed_hosts/` link | Remove the old link and retry |

### Host NQN

Firmware's default Host NQN comes from the SMBIOS UUID; when QEMU/OVMF provides no UUID, it falls back to the fixed `nqn.2014-08.org.ipxe:ipxe`. The control plane issues `hostnqn` per Worker and overrides the default through the `hostnqn` setting. When the target sets `allow_any_host=0`, the `hosts/` directory name must match the value the device actually sends.

### sanboot and credential injection

```text
sanboot nvme://<traddr>:<trsvcid>/<nqn>[?secret=<DHHC-1 key>]
```

Without authentication:

```text
sanboot nvme://10.0.2.2:4420/nqn.2026-07.com.kurrent:worker-01.ubuntu.0123456789ab
```

With authentication:

```text
sanboot nvme://10.0.2.2:4420/nqn.2026-07.com.kurrent:worker-01.ubuntu.0123456789ab?secret=DHHC-1:01:...
```

On success the log shows `sending Property Set (CC)`, `namespace 1: N blocks of 4096 bytes`, and `I/O queue established`, after which control passes to GRUB on the disk.

Keys are never embedded in firmware. The control plane `/boot-vars` endpoint returns an iPXE script per device that sets boot variables:

```ipxe
dhcp || goto failed
chain --autofree https://<control-plane>/devices/challenge?mac=${net0/mac} || goto failed
isset ${nonce} || goto failed
sign ${nonce}${net0/mac} || goto failed
chain --autofree https://<control-plane>/boot-vars?mac=${net0/mac}&hostname=${hostname}&nonce=${nonce}&sig=${sig} \
  || goto failed
sanboot nvme://<traddr>:<trsvcid>/<nqn>?secret=${nbft-secret}
:failed
shell
```

When data-plane TLS is enabled, the control plane also issues `nbft-tls-psk` and `nbft-tls-port`; the script appends `&tls_psk=${nbft-tls-psk}&tls_trsvcid=${nbft-tls-port}` to the URI. If the port or key is missing, the script must enter the failure branch and must not fall back to the plaintext port. `/boot-vars` does not require a login session; credentials are injected only after the one-time nonce and device signature verification. Unverified requests receive only non-credential variables.

### TLS 1.3 dual-segment encryption

When the `sanboot` URI port equals `tls_trsvcid`, firmware performs a TLS 1.3 PSK handshake for that connection:

- The PSK identity is `NVMe0R01 <hostnqn> <subsysnqn>`.
- Patch 0010 implements the handshake and record layer with `TLS_AES_128_GCM_SHA256` and `psk_dhe_ke`.
- The same parameters travel with NBCT to initramfs; the kernel injects the PSK into the `.nvme` keyring and reconnects to the data port with `--tls`.

The default path encrypts both the firmware segment and the kernel segment. HTTPS boot assets still use iPXE native TLS (up to 1.2), independent of data-plane TLS 1.3.

### Validation

QEMU/OVMF validation requires KVM, OVMF firmware, and a working nvmet target. The firmware repository provides scripts:

```bash
sudo AUTH=1 bash test/nvmet-setup.sh
python3 test/cred-server.py &
# Test script: test/nvmeof-auth-test.ipxe
bash test/run-qemu-auth.sh
```

Acceptance: the serial console shows successful authentication, Property Set, I/O queues, and GRUB output; the target shows two controller records, one for the firmware segment and one for the kernel segment; in the packet capture, the first byte of each client TCP stream is `0x16`, plaintext-port packets are zero, and the payload contains no PSK or DH-HMAC-CHAP key plaintext. `test/cred-server.py` is for validation only and must not be used as a production credential endpoint.

### Common failures

| Symptom | Cause | Action |
| --- | --- | --- |
| Identify returns `0x8018` | Property Set was skipped; authentication did not finish | Confirm the firmware contains the full patch chain |
| No `sending Property Set (CC)` in the log | Authentication incomplete or a phase race | Check target authentication configuration and keys |
| Connection resets after authentication | AuthReceive sent twice | Use firmware containing the 0005 authentication fix |
| `authentication required but no secret` | Target requires authentication but the URI has no `secret` | Check `/boot-vars` variable injection |
| Connect fails | NQN, port, or Host NQN mismatch | Compare target configuration with `ss -tln` |
| TLS handshake fails | URI port is not `tls_trsvcid`, or the PSK mismatches | Compare control plane parameters with the data-plane port |

## Runtime boundaries

- Build artifacts are UEFI-only, seven in total.
- NVMe/TCP only; RDMA and FC are not supported.
- The boot path uses a single controller and a single namespace; Identify parses 512 B and 4 KB blocks.
- DH-HMAC-CHAP is one-way; mutual authentication is outside the firmware implementation.
- HTTPS downloads use iPXE native TLS up to 1.2; data-plane `sanboot` uses TLS 1.3 PSK.
- Mainline firmware is unsigned and cannot load with Secure Boot enabled; disable Secure Boot before deployment.
- Firmware provides commands and protocol capabilities only; it does not implement registration windows, Worker binding, disk selection, or credential issuance.
- iPXE-side NBFT generation is still pending; the current validation path uses an injected NBFT table to simulate the firmware pre-generation stage.

## Related pages

- [Firmware build and deployment](./build-deploy.md)
- [Firmware contract](../reference/firmware-contract.md)
- [Boot chain](../concepts/boot-chain.md)
- [System disk customization](../guide/system-disk.md)
- [Distribution matrix](../reference/distributions.md)
- [Security model](../architecture/security.md)
- Firmware repository source, patches, and research notes: <https://github.com/dutyc/kurrent-firmware>
