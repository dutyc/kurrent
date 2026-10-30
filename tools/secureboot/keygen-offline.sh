#!/usr/bin/env bash
#
# Generate an offline artifact signing key set:
#   artifact-signing.key    EFI signing key (keep offline)
#   artifact-signing.crt    certificate enrolled in UEFI db
#
# The boot HTTPS CA is different: it lives in the control plane
# (state/pki/ca/boot/), issues the 4443 leaf certificate, and is exported from
# there for the firmware build. Keeping the two keys separate means a control
# plane compromise cannot sign firmware.
#
# The output directory must be outside this repository (and outside a
# container state directory). Keys are created mode 0600; nothing secret is
# ever printed. Refuses to overwrite an existing key set.

set -euo pipefail

usage() {
  echo "usage: $0 <output-dir> [artifact-cn]" >&2
  echo "  output-dir must be an offline directory outside the repository" >&2
  exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || usage
OUT_ARG="$1"
ARTIFACT_CN="${2:-kurrent-firmware artifact signing}"

command -v openssl >/dev/null || { echo "ERROR: openssl not found" >&2; exit 1; }

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
OUT="$(realpath -m "$OUT_ARG")"
case "$OUT" in
  "$REPO_ROOT"|"$REPO_ROOT"/*|/app|/app/*)
    echo "ERROR: refusing to write keys inside $REPO_ROOT or /app" >&2
    exit 1
    ;;
esac

umask 077
mkdir -p "$OUT"
chmod 700 "$OUT"

for name in artifact-signing.key artifact-signing.crt; do
  if [[ -e "$OUT/$name" ]]; then
    echo "ERROR: $OUT/$name already exists; rotate by moving the old set aside first" >&2
    exit 1
  fi
done

openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes \
  -keyout "$OUT/artifact-signing.key" -out "$OUT/artifact-signing.crt" \
  -subj "/CN=$ARTIFACT_CN" \
  -addext "basicConstraints=critical,CA:FALSE" \
  -addext "keyUsage=critical,digitalSignature" \
  -addext "extendedKeyUsage=codeSigning" \
  >/dev/null 2>&1

chmod 600 "$OUT"/artifact-signing.key
chmod 644 "$OUT"/artifact-signing.crt

echo "artifact signing key written to $OUT"
echo "  signer SHA-256: $(openssl x509 -in "$OUT/artifact-signing.crt" -noout -fingerprint -sha256 | cut -d= -f2)"
echo
echo "Next:"
echo "  1. start the control plane once and export the boot CA from state/pki/ca/boot/ca.crt"
echo "  2. build firmware with SB_KEY=$OUT/artifact-signing.key SB_CERT=$OUT/artifact-signing.crt BOOT_CA_CERT=<exported boot CA>"
echo "  3. export the signed artifact with tools/secureboot/export-usb.sh"
