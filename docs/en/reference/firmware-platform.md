# Firmware Platform Reference

This page is the platform-side firmware reference: patch set, device information fields, NIC support matrix, target configuration and server-side errors. The interface between firmware and the platform (commands, NVRAM, TOFU, challenge-response and boot variables) is covered in the [Firmware Contract](./firmware-contract.md); building, artifact selection and deployment are covered in [Firmware Build and Deployment](../firmware/build-deploy.md).

## Patch set

Patches are applied in filename order; if any patch fails against the pinned baseline, the build fails. The current chain has ten patches:

| No. | Name | Content |
| --- | --- | --- |
| 0001 | `realtek-adaptation` | Native driver adaptation for the RTL8125 family and RTL8126 |
| 0002 | `makefile-ipxe-debug` | Debug targets inherit the full NIC driver set |
| 0003 | `snponly-local-boot` | Local-media boot falls back to SNP/NII/MNP devices |
| 0004 | `device-info-collection` | SMBIOS memory fields and PCI NIC chip names |
| 0005 | `nvmeof-adaptation` | NVMe/TCP SAN driver, DH-HMAC-CHAP, authentication timing fixes, and `hostnqn` |
| 0006 | `efi-nvs-backend` | EFI variable NVS backend, including empty-block deletion semantics |
| 0007 | `tofu-fingerprint` | TOFU fingerprint pinning and verification |
| 0008 | `devicekey-commands` | ECDSA P-256 device identity commands |
| 0009 | `nvmetcp-nbct-acpi-table` | NBCT ACPI table and kernel credential handoff |
| 0010 | `tls13-psk` | TLS 1.3 PSK client and record layer |

Besides the patches, `embed/auto.ipxe` is a build-level EMBED asset compiled into the `direct-uefi` artifacts via `EMBED=`; it is not part of the patch sequence. Licensing differs across the set: the RTL8125/8126 adaptation is GPL-2.0 only and may not be redistributed under UBDL; the other patches follow their file-header terms. Read `patches/README` before redistribution.

## Device information fields

Firmware collects four groups of settings and reports them over HTTP GET:

| Group | Settings | Source |
| --- | --- | --- |
| Identity | `uuid`, `manufacturer`, `product`, `serial`, `board-serial`, `asset` | SMBIOS types 1/2/3 |
| CPU | `cpuvendor`, `cpumodel` | CPUID |
| Memory | `mem-total`, `mem-type`, `mem-speed` | SMBIOS type 17 (patch 0004) |
| NIC | `netX/mac`, `netX/chip`, `netX/busid`, `netX/bustype`, `netX/linktype` | netdev settings; `chip` enabled by patch 0004 |

Numeric settings follow the iPXE convention and are expanded as hexadecimal; the server must accept both hexadecimal and decimal parsing. Strings may contain spaces and special characters, so confirm URL encoding before reporting. Environments without SMBIOS type 17 do not provide memory fields; under SNP boot `${netX/chip}` reports `snpnet`, and only native drivers report the chip model. See the [firmware contract](./firmware-contract.md#device-reporting) for field-level details.

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

## Target configuration

The NQN and address below are for local validation and use the production format `nqn.2026-07.com.kurrent:worker-01.ubuntu.0123456789ab`; production disk NQNs are generated by the control plane from `spec.agent.nqnBase`, default prefix `nqn.2026-07.com.kurrent`. The NQN in the `sanboot` URI must match the actual target subsystem.

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

### Server-side error reference

| Symptom | Cause | Action |
| --- | --- | --- |
| `base64 key decoding error -1` | The `DHHC-1:XX:` prefix is shorter than 10 bytes | Keep the two-digit prefix |
| `dhchap status 2` | The key CRC32 is not the final value | Regenerate with `zlib.crc32` |
| Unauthenticated connection succeeds | `attr_allow_any_host=1` | Set it to 0 and recreate the subsystem |
| `Can't set allow_any_host when explicit hosts are set!` | Stale `allowed_hosts/` link | Remove the old link and retry |

### Host NQN

Firmware's default Host NQN comes from the SMBIOS UUID; when QEMU/OVMF provides no UUID, it falls back to the fixed `nqn.2014-08.org.ipxe:ipxe`. The control plane issues `hostnqn` per Worker and overrides the default through the `hostnqn` setting. When the target sets `allow_any_host=0`, the `hosts/` directory name must match the value the device actually sends.

## sanboot and data-plane TLS

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

When the `sanboot` URI port equals `tls_trsvcid`, firmware performs a TLS 1.3 PSK handshake for that connection:

- The PSK identity is `NVMe0R01 <hostnqn> <subsysnqn>`.
- Patch 0010 implements the handshake and record layer with `TLS_AES_128_GCM_SHA256` and `psk_dhe_ke`.
- The same parameters travel with NBCT to initramfs; the kernel injects the PSK into the `.nvme` keyring and reconnects to the data port with `--tls`.

The default path encrypts both the firmware segment and the kernel segment. HTTPS boot assets still use iPXE native TLS (up to 1.2), independent of data-plane TLS 1.3.

## Related pages

- [Firmware Overview](../firmware/index.md)
- [Firmware Contract](./firmware-contract.md)
- [Firmware Build and Deployment](../firmware/build-deploy.md)
- [Distribution Matrix](./distributions.md)
- [Boot Chain](../concepts/boot-chain.md)
- [Security Model](../architecture/security.md)
- Firmware repository source, patches and research notes: <https://github.com/dutyc/kurrent-firmware>
