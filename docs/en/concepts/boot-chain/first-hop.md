# First Hop

The first hop spans power-on to an executable bootloader. It determines where every later script comes from, so it must be trusted before anything else; this step is performed by the target firmware itself and relies on nothing the network claims.

## Address and Download

The device gets an address and `next-server` over DHCP, then downloads the first stage `snponly.efi` over TFTP. The TFTP root exposes only this file and the `boot.ipxe` pointer.

Restart the dnsmasq container after replacing `snponly.efi`: the file-level mount pins the inode, so a hot replacement has no effect.

## Verification

UEFI verifies `snponly.efi` against the site certificate in the target's `db`. If the image is unsigned, the signature does not match, or the image is tampered with, firmware refuses to run it and network boot never begins.

The site certificate is provisioned once: writing it into the target's `db` is a deployment step, not something repeated per boot, and it does not depend on the control plane being online. See [Secure Boot Provisioning](../../firmware/secure-boot.md) for the provisioning flow.

## Embedded Scripts and boot CA

The signed image embeds the static scripts, the boot CA and the boot HTTPS port, and leaves the controller address out of the image by default, so scripts after the first hop cannot be replaced on their own. Menus, variables and device endpoints are fetched over 4443 HTTPS, trusting only that boot CA and never first-contact trust.

## Failures

- verification fails: firmware refuses to run the image and the device stays at the UEFI boot menu;
- the 4443 handshake fails: the boot CA used at build time differs from `state/pki/ca/boot/ca.crt`, or the leaf SAN or port differs from the build;
- TFTP or DHCP is unreachable: network boot cannot begin; this only causes denial of service and changes no trust decision.

## Related Pages

- Back to the [Boot Chain](../boot-chain.md)
- How the device proves itself next: [Device Identity](./device-identity.md)
- Trust anchors and provisioning: [Secure Boot Provisioning](../../firmware/secure-boot.md)
- Threat model: [Security Model](../../architecture/security.md)
