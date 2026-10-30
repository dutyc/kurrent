#!/usr/bin/env bash
#
# Enrol the artifact-signing certificate into a target's UEFI db over Redfish.
#
# The tool is a commissioning step, not part of the boot chain:
#   1. discover the system and its Secure Boot databases
#   2. refuse to continue unless a Microsoft UEFI CA is already in db
#      (the golden image's shim is signed by it; override is explicit)
#   3. import the public certificate idempotently (never resets keys)
#   4. read the database back and verify the certificate fingerprint
#   5. optionally enable Secure Boot (takes effect after reboot)
#
# Credentials come from KURRENT_BMC_USER / KURRENT_BMC_PASSWORD or a 0600
# credential file; they are never passed on the command line or logged.
# Private keys are not read. `--dry-run` performs every read and no write.
#
# Exit codes: 0 ok/skipped/dry-run, 1 usage or precondition, 2 imported but
# unverified, 3 Redfish unsupported or transport failure, 4 write failed.
#
# Vendor Redfish implementations differ: this tool is protocol-tested against a
# mock BMC. Confirm certificate import and Setup Mode behaviour on one unit per
# model before batch use; fall back to the USB/BIOS path when unsupported.

set -euo pipefail
exec python3 - "$@" <<'PY'
import argparse
import base64
import hashlib
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class ToolError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def log(message):
    print(f"[secureboot] {message}", file=sys.stderr)


def die(message, code):
    raise ToolError(message, code)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="enroll-redfish.sh",
        description="Import the artifact-signing certificate into UEFI db over Redfish.",
    )
    parser.add_argument("--bmc", required=True, help="BMC hostname or IP")
    parser.add_argument("--port", type=int, help="BMC port (default 443, or 80 for --scheme http)")
    parser.add_argument("--scheme", choices=("https", "http"), default="https")
    parser.add_argument("--system", help="Redfish system id (default: first member)")
    parser.add_argument("--cert", required=True, help="artifact signing certificate (PEM or DER)")
    parser.add_argument("--credential-file", help="0600 file with user=/password=")
    parser.add_argument("--enable-secureboot", action="store_true", help="PATCH SecureBootEnable=true")
    parser.add_argument("--dry-run", action="store_true", help="read and verify only, never write")
    parser.add_argument("--insecure", action="store_true", help="skip BMC TLS verification")
    parser.add_argument("--ca-cert", help="CA bundle used to verify the BMC certificate")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds")
    parser.add_argument("--verify-retries", type=int, default=3, help="read-back attempts")
    parser.add_argument("--report", help="append one JSON line per host to this file")
    parser.add_argument("--allow-no-microsoft-ca", action="store_true",
                        help="continue even when db has no Microsoft UEFI CA")
    parser.add_argument("--allow-unverified-readback", action="store_true",
                        help="exit 0 when the BMC cannot expose a verifiable certificate")
    args = parser.parse_args(argv)
    if args.scheme == "http" and not args.insecure:
        die("--scheme http requires --insecure (plaintext BMC transport)", 1)
    if args.port is None:
        args.port = 443 if args.scheme == "https" else 80
    return args


def load_credentials(args):
    user = os.environ.get("KURRENT_BMC_USER")
    password = os.environ.get("KURRENT_BMC_PASSWORD")
    if args.credential_file:
        path = args.credential_file
        try:
            mode = os.stat(path).st_mode & 0o777
            if mode != 0o600:
                die(f"credential file must be mode 0600 (got {oct(mode)})", 1)
            values = {}
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip()
        except OSError as error:
            die(f"cannot read credential file {path}: {error}", 1)
        user = values.get("user", user)
        password = values.get("password", password)
    if not user or password is None or password == "":
        die("missing BMC credentials: set KURRENT_BMC_USER/KURRENT_BMC_PASSWORD "
            "or pass --credential-file", 1)
    return user, password


def build_opener(args):
    if args.insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif args.ca_cert:
        context = ssl.create_default_context(cafile=args.ca_cert)
    else:
        context = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
    )


class Redfish:
    def __init__(self, args, user, password):
        self.args = args
        self.base = f"{args.scheme}://{args.bmc}:{args.port}"
        try:
            self.opener = build_opener(args)
        except (OSError, ssl.SSLError) as error:
            die(f"cannot initialise TLS: {error}", 1)
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = f"Basic {token}"

    def request(self, method, path, body=None):
        url = path if path.startswith("http") else self.base + path
        data = None
        headers = {"Authorization": self.auth, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.args.timeout) as response:
                raw = response.read()
                return response.status, self._decode(raw)
        except urllib.error.HTTPError as error:
            raw = error.read(16384)
            return error.code, self._decode(raw)
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as error:
            die(f"{method} {path} failed: {error}", 3)

    @staticmethod
    def _decode(raw):
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}


def get(client, path, code=3):
    status, body = client.request("GET", path)
    if status in (401, 403):
        die(f"GET {path}: authentication rejected ({status})", 1)
    if status >= 400:
        die(f"GET {path}: HTTP {status}", code)
    return body


def find_member(collection, name):
    wanted = name.lower()
    for member in collection.get("Members", []):
        ident = str(member.get("Id", "")).lower()
        label = str(member.get("Name", "")).lower()
        if ident == wanted or label == wanted or member.get("@odata.id", "").rstrip("/").endswith("/" + wanted):
            return member
    return None


def to_der(raw):
    if b"-----BEGIN CERTIFICATE-----" in raw:
        return ssl.PEM_cert_to_DER_cert(raw.decode())
    return raw


def openssl(der, *flags):
    result = subprocess.run(
        ["openssl", "x509", "-inform", "DER", "-noout", *flags],
        input=der, capture_output=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.decode(errors="replace").strip()


def cert_facts(raw):
    der = to_der(raw)
    subject = openssl(der, "-subject", "-nameopt", "RFC2253").removeprefix("subject=")
    serial = openssl(der, "-serial").removeprefix("serial=").upper()
    return {
        "der": der,
        "sha256": hashlib.sha256(der).hexdigest(),
        "subject": subject,
        "serial": serial,
        "pem": ssl.DER_cert_to_PEM_cert(der),
    }


def read_certificate(client, member):
    body = get(client, member["@odata.id"])
    facts = {"subject": body.get("Subject", ""), "serial": str(body.get("SerialNumber", "")).upper(),
             "sha256": None}
    text = body.get("CertificateString")
    if text:
        try:
            payload = text.encode()
            if str(body.get("CertificateType", "PEM")).upper() == "DER":
                payload = base64.b64decode(text)
            parsed = cert_facts(payload)
            facts.update({"sha256": parsed["sha256"], "subject": parsed["subject"] or facts["subject"],
                          "serial": parsed["serial"] or facts["serial"]})
        except (ValueError, ssl.SSLError):
            pass
    return facts


def normalize(value):
    return "".join(str(value).split()).lower()


def matches(facts, wanted):
    if wanted["sha256"] and facts.get("sha256"):
        return facts["sha256"] == wanted["sha256"]
    if not normalize(wanted["subject"]) or not normalize(wanted["serial"]):
        return False
    return (normalize(facts.get("subject", "")) == normalize(wanted["subject"])
            and normalize(facts.get("serial", "")) == normalize(wanted["serial"]))


def microsoft_ca_present(certificates):
    for facts in certificates:
        subject = normalize(facts.get("subject", ""))
        if "microsoft" in subject and ("uefica" in subject or "windowsproduction" in subject):
            return True
    return False


def read_database(client, secureboot_path):
    databases = get(client, secureboot_path + "/SecureBootDatabases")
    db_member = find_member(databases, "db")
    if not db_member:
        die("Redfish exposes no db in SecureBootDatabases", 3)
    db_path = db_member["@odata.id"]
    database = get(client, db_path)
    link = database.get("Certificates", {}).get("@odata.id")
    if not link:
        die("Redfish db exposes no Certificates collection", 3)
    collection = get(client, link)
    certificates = []
    for member in collection.get("Members", []):
        if "@odata.id" not in member:
            continue
        certificates.append(read_certificate(client, member))
    return link, certificates


def read_readback(client, link, wanted, retries):
    certificates = []
    for attempt in range(max(1, retries)):
        certificates = []
        collection = get(client, link)
        for member in collection.get("Members", []):
            if "@odata.id" in member:
                certificates.append(read_certificate(client, member))
        for facts in certificates:
            if matches(facts, wanted):
                return ("strong" if facts.get("sha256") and wanted["sha256"] else "weak", certificates)
        if attempt + 1 < max(1, retries):
            time.sleep(1)
    return ("none", certificates)


def emit(report, path):
    line = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if path:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    print(line)


def run(args):
    user, password = load_credentials(args)
    client = Redfish(args, user, password)

    with open(args.cert, "rb") as handle:
        wanted = cert_facts(handle.read())
    if not wanted["subject"] or not wanted["sha256"]:
        die(f"{args.cert} is not a valid X.509 certificate", 1)
    log(f"certificate {wanted['subject']} sha256={wanted['sha256']}")

    systems = get(client, "/redfish/v1/Systems")
    members = systems.get("Members", [])
    if not members:
        die("Redfish exposes no ComputerSystem", 3)
    system_path = (f"/redfish/v1/Systems/{urllib.parse.quote(str(args.system))}"
                   if args.system else members[0]["@odata.id"])
    system = get(client, system_path)
    secureboot_path = system.get("SecureBoot", {}).get("@odata.id") or system_path + "/SecureBoot"
    secureboot = get(client, secureboot_path)
    state_before = {
        "enabled": secureboot.get("SecureBootEnable"),
        "mode": secureboot.get("SecureBootMode"),
        "current_boot": secureboot.get("SecureBootCurrentBoot"),
    }
    log(f"system={system_path.rsplit('/', 1)[-1]} secureboot={state_before}")

    link, certificates = read_database(client, secureboot_path)
    has_ours = any(matches(facts, wanted) for facts in certificates)
    has_microsoft = microsoft_ca_present(certificates)
    log(f"db certificates={len(certificates)} ours={has_ours} microsoft_ca={has_microsoft}")
    if not has_microsoft and not args.allow_no_microsoft_ca:
        die("db has no Microsoft UEFI CA; the golden image's shim would not verify "
            "(override only with --allow-no-microsoft-ca)", 1)

    warnings = []
    if state_before.get("mode") == "SetupMode":
        warnings.append("BMC reports SetupMode; enable Secure Boot after enrolling")
    if not has_microsoft:
        warnings.append("Microsoft UEFI CA absent; OS loader chain is platform-specific")

    action = "skipped"
    if not has_ours:
        if args.dry_run:
            action = "dry-run"
        else:
            status, body = client.request(
                "POST", link,
                {"CertificateString": wanted["pem"], "CertificateType": "PEM"},
            )
            if status in (200, 201, 204):
                action = "imported"
            elif status == 409:
                action = "skipped"
            elif status in (400, 404, 405, 501):
                die(f"BMC refused certificate import (HTTP {status}); "
                    "fall back to the USB/BIOS path", 3)
            else:
                die(f"certificate import failed (HTTP {status})", 4)
            log(f"import {action} (HTTP {status})")

    verification = "not-run"
    if action in ("imported", "skipped"):
        verification, after = read_readback(client, link, wanted, args.verify_retries)
        has_microsoft_after = microsoft_ca_present(after)
        log(f"read-back verification={verification} microsoft_ca={has_microsoft_after}")
        if not has_microsoft_after:
            warnings.append("Microsoft UEFI CA missing after import")
        if verification == "none" and not args.allow_unverified_readback:
            emit({
                "bmc": args.bmc, "system": system_path.rsplit("/", 1)[-1],
                "action": action, "verification": verification,
                "certificate": {"subject": wanted["subject"], "sha256": wanted["sha256"]},
                "secureboot_before": state_before, "secureboot_after": state_before,
                "warnings": warnings,
                "errors": ["BMC does not expose a verifiable certificate; rerun with "
                           "--allow-unverified-readback only after manual confirmation"],
            }, args.report)
            return 2
        if verification == "none":
            warnings.append("BMC exposes no certificate identity; repeated runs may "
                            "import duplicates, so verify db manually")
        if verification == "weak":
            warnings.append("read-back matched subject/serial but the BMC exposes no "
                            "CertificateString for a fingerprint check")

    state_after = state_before
    if args.enable_secureboot and not args.dry_run:
        status, body = client.request("PATCH", secureboot_path, {"SecureBootEnable": True})
        if status not in (200, 204):
            die(f"could not enable Secure Boot (HTTP {status}); check the BIOS policy", 4)
        secureboot = get(client, secureboot_path)
        state_after = {
            "enabled": secureboot.get("SecureBootEnable"),
            "mode": secureboot.get("SecureBootMode"),
            "current_boot": secureboot.get("SecureBootCurrentBoot"),
        }
        warnings.append("Secure Boot changes take effect after a reboot")

    emit({
        "bmc": args.bmc,
        "system": system_path.rsplit("/", 1)[-1],
        "action": action,
        "verification": verification,
        "certificate": {"subject": wanted["subject"], "sha256": wanted["sha256"]},
        "secureboot_before": state_before,
        "secureboot_after": state_after,
        "warnings": warnings,
        "errors": [],
    }, args.report)
    return 0


def main(argv):
    try:
        args = parse_args(argv)
        if args.insecure:
            log("WARNING: BMC TLS verification is disabled (--insecure)")
        return run(args)
    except ToolError as error:
        log(f"ERROR: {error}")
        return error.code


sys.exit(main(sys.argv[1:]))
PY
