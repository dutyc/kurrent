#!/usr/bin/env bash
#
# Prepare a USB (or BMC virtual-media) directory for Secure Boot provisioning:
#   db-cert.cer        artifact signing certificate in DER (UEFI db import)
#   snponly-sb.efi     signed firmware
#   manifest.json      build manifest (hash, signer, boot CA fingerprint)
#   boot-ca.crt        boot HTTPS CA (for the firmware build / audit)
#   README.txt         operator instructions
#
# Only public material is copied. Private keys are never read or written.

set -euo pipefail

usage() {
  echo "usage: $0 <usb-root> <artifact-signing.crt> <signed-efi> <boot-ca.crt> [manifest.json]" >&2
  exit 2
}

[[ $# -ge 4 && $# -le 5 ]] || usage
USB_ROOT="$1"
ARTIFACT_CERT="$2"
SIGNED="$3"
BOOT_CA="$4"
MANIFEST="${5:-$(dirname "$SIGNED")/manifest.json}"

command -v openssl >/dev/null || { echo "ERROR: openssl not found" >&2; exit 1; }
command -v sbverify >/dev/null || { echo "ERROR: sbverify not found (sbsigntool)" >&2; exit 1; }

[[ -d "$USB_ROOT" ]] || { echo "ERROR: $USB_ROOT is not a directory" >&2; exit 1; }
[[ -f "$SIGNED" ]] || { echo "ERROR: signed artifact not found: $SIGNED" >&2; exit 1; }
[[ -f "$ARTIFACT_CERT" ]] || { echo "ERROR: signing certificate not found: $ARTIFACT_CERT" >&2; exit 1; }
[[ -f "$BOOT_CA" ]] || { echo "ERROR: boot CA not found: $BOOT_CA" >&2; exit 1; }
[[ -f "$MANIFEST" ]] || { echo "ERROR: manifest not found: $MANIFEST" >&2; exit 1; }

sbverify --cert "$ARTIFACT_CERT" "$SIGNED" >/dev/null \
  || { echo "ERROR: artifact is not signed by $ARTIFACT_CERT" >&2; exit 1; }

DEST="$USB_ROOT/kurrent-secureboot"
mkdir -p "$DEST"

cp "$SIGNED" "$DEST/snponly-sb.efi"
cp "$MANIFEST" "$DEST/manifest.json"
cp "$BOOT_CA" "$DEST/boot-ca.crt"
openssl x509 -in "$ARTIFACT_CERT" -outform DER -out "$DEST/db-cert.cer"

cat > "$DEST/README.txt" <<'EOF'
kurrent Secure Boot provisioning
===============================

Public material only. No private key is included.

1. Firmware setup: disable CSM, enable Secure Boot, enter Setup Mode.
2. Key Management: import db-cert.cer into the db (or use BMC/Redfish
   SecureBoot database upload). Keep the platform vendor KEK/PK policy as is.
3. Boot snponly-sb.efi from this media (or the firmware HTTP Boot path).
   The image talks to the controller over HTTPS on the boot CA port; it
   refuses any certificate not issued by boot-ca.crt.
4. Re-enrol after any firmware update that clears db.

Recovery: to retire this signing key, clear db and boot a newly signed image.
EOF

echo "exported to $DEST"
( cd "$DEST" && sha256sum * )
