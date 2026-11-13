#!/usr/bin/env bash
#
# Read-only Secure Boot preflight for the target machine.
#   check-host.sh [artifact-signing.crt]
#
# Reports firmware mode, Secure Boot state, and whether the given signing
# certificate appears in db. Requires no root. Exit status is non-zero when a
# hard requirement fails.

set -uo pipefail

CERT="${1:-}"
FAIL=0
WARN=0

ok()   { echo "  ok    $*"; }
warn() { echo "  warn  $*"; WARN=$((WARN + 1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL + 1)); }

echo "kurrent Secure Boot preflight"

if [[ -d /sys/firmware/efi ]]; then
  ok "booted in UEFI mode"
else
  fail "not booted in UEFI mode (no /sys/firmware/efi)"
fi

efi_var_byte() {
  local name="$1" path
  path="$(find /sys/firmware/efi/efivars -maxdepth 1 -name "${name}-*" -print -quit 2>/dev/null)"
  [[ -n "$path" && -r "$path" ]] || return 1
  # First 4 bytes are attributes; the value starts at byte 5.
  od -An -tu1 -j4 -N1 "$path" 2>/dev/null | tr -d ' '
}

SB_STATE="$(efi_var_byte SecureBoot || true)"
SETUP_STATE="$(efi_var_byte SetupMode || true)"
case "$SB_STATE" in
  1) ok "SecureBoot=1 (enforcing)" ;;
  0) fail "SecureBoot=0 (disabled or Setup Mode)" ;;
  *) warn "SecureBoot variable unavailable; check firmware settings manually" ;;
esac
case "$SETUP_STATE" in
  1) warn "SetupMode=1: db can be modified; enable User Mode after enrolment" ;;
  0) ok "SetupMode=0 (User Mode)" ;;
  *) : ;;
esac

if command -v mokutil >/dev/null; then
  mokutil --sb-state 2>&1 | sed 's/^/  info  /'
else
  warn "mokutil not installed; cannot print a summary"
fi

if command -v efi-readvar >/dev/null; then
  if [[ -n "$CERT" ]]; then
    [[ -f "$CERT" ]] || { fail "certificate not found: $CERT"; CERT=""; }
  fi
  if [[ -n "$CERT" ]]; then
    FP="$(openssl x509 -in "$CERT" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)"
    if efi-readvar -v db 2>/dev/null | grep -qi "${FP//:/}"; then
      ok "signing certificate present in db"
    else
      fail "signing certificate not found in db (fingerprint ${FP})"
    fi
  else
    echo "  info  db entries:"
    efi-readvar -v db 2>/dev/null | sed -n '1,20p' | sed 's/^/        /'
  fi
elif [[ -n "$CERT" ]]; then
  warn "efi-readvar not installed; cannot verify db membership automatically"
fi

echo
if [[ $FAIL -gt 0 ]]; then
  echo "preflight failed ($FAIL requirement(s), $WARN warning(s))"
  exit 1
fi
echo "preflight passed ($WARN warning(s))"
