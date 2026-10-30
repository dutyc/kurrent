"""Redfish db enrollment tool: end-to-end against a mock BMC."""

import base64
import json
import os
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "secureboot" / "enroll-redfish.sh"
USER = "admin"
PASSWORD = "correct-horse-battery-staple"


def openssl(*args, data=None):
    return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True).stdout


def make_cert(directory, name, cn, san=None):
    key = directory / f"{name}.key"
    crt = directory / f"{name}.crt"
    command = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(crt), "-days", "30", "-subj", f"/CN={cn}",
    ]
    if san:
        command += ["-addext", f"subjectAltName={san}"]
    subprocess.run(command, capture_output=True, check=True)
    return crt


def facts(pem):
    der = openssl("x509", "-outform", "DER", data=pem)
    subject = openssl("x509", "-noout", "-subject", "-nameopt", "RFC2253", data=pem)
    serial = openssl("x509", "-noout", "-serial", data=pem)
    return {
        "pem": pem,
        "subject": subject.decode().strip().removeprefix("subject="),
        "serial": serial.decode().strip().removeprefix("serial=").upper(),
    }


class State:
    def __init__(self):
        self.certs = []
        self.expose_string = True
        self.expose_identity = True
        self.post_status = 201
        self.enabled = False
        self.mode = "SetupMode"
        self.writes = 0

    def add(self, pem):
        item = facts(pem)
        item["id"] = str(len(self.certs) + 1)
        self.certs.append(item)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _authorized(self):
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        return self.headers.get("Authorization") == expected

    def _send(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _certificate(self, cert):
        body = {"Id": cert["id"]}
        if self.server.state.expose_identity:
            body.update({"Subject": cert["subject"], "SerialNumber": cert["serial"]})
        if self.server.state.expose_string:
            body["CertificateString"] = cert["pem"].decode()
            body["CertificateType"] = "PEM"
        self._send(200, body)

    def do_GET(self):
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        state = self.server.state
        base = "/redfish/v1/Systems/1"
        certificates = base + "/SecureBoot/SecureBootDatabases/db/Certificates"
        if self.path == "/redfish/v1/Systems":
            return self._send(200, {"Members": [{"@odata.id": base}]})
        if self.path == base:
            return self._send(200, {"SecureBoot": {"@odata.id": base + "/SecureBoot"}})
        if self.path == base + "/SecureBoot":
            return self._send(200, {
                "SecureBootEnable": state.enabled,
                "SecureBootMode": state.mode,
                "SecureBootCurrentBoot": "Enabled" if state.enabled else "Disabled",
            })
        if self.path == base + "/SecureBoot/SecureBootDatabases":
            return self._send(200, {"Members": [{"Id": "db", "@odata.id": base + "/SecureBoot/SecureBootDatabases/db"}]})
        if self.path == base + "/SecureBoot/SecureBootDatabases/db":
            return self._send(200, {"Id": "db", "Certificates": {"@odata.id": certificates}})
        if self.path == certificates:
            members = [{"@odata.id": f"{certificates}/{cert['id']}"} for cert in state.certs]
            return self._send(200, {"Members": members})
        for cert in state.certs:
            if self.path == f"{certificates}/{cert['id']}":
                return self._certificate(cert)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        state = self.server.state
        path = "/redfish/v1/Systems/1/SecureBoot/SecureBootDatabases/db/Certificates"
        if self.path != path:
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if state.post_status != 201:
            return self._send(state.post_status, {"error": "rejected"})
        state.writes += 1
        state.add(body["CertificateString"].encode())
        cert = state.certs[-1]
        self._send(201, {"Id": cert["id"]})

    def do_PATCH(self):
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if "SecureBootEnable" in body:
            self.server.state.enabled = bool(body["SecureBootEnable"])
            self.server.state.mode = "UserMode" if body["SecureBootEnable"] else "SetupMode"
            self.server.state.writes += 1
        self._send(200, {})


@pytest.fixture
def bmc():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.state = State()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def certs(tmp_path):
    return {
        "microsoft": make_cert(tmp_path, "microsoft", "Microsoft Corporation UEFI CA 2011"),
        "signer": make_cert(tmp_path, "signer", "kurrent-test-signer"),
        "tls": make_cert(tmp_path, "tls", "127.0.0.1", san="IP:127.0.0.1"),
    }


def run_tool(server, cert, *extra, tls=False, env=None):
    command = [
        str(TOOL), "--bmc", "127.0.0.1", "--port", str(server.server_address[1]),
        "--cert", str(cert), "--timeout", "5", "--verify-retries", "1",
    ]
    if not tls:
        command += ["--scheme", "http", "--insecure"]
    environment = {**os.environ, "KURRENT_BMC_USER": USER, "KURRENT_BMC_PASSWORD": PASSWORD}
    if env:
        environment.update(env)
    return subprocess.run(command + list(extra), capture_output=True, text=True, env=environment)


def report(result):
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_imports_and_verifies_fingerprint(bmc, certs):
    bmc.state.add(certs["microsoft"].read_bytes())
    result = run_tool(bmc, certs["signer"])
    assert result.returncode == 0, result.stderr
    assert report(result)["action"] == "imported"
    assert report(result)["verification"] == "strong"
    assert len(bmc.state.certs) == 2
    assert bmc.state.certs[-1]["subject"].endswith("kurrent-test-signer")


def test_second_run_is_idempotent(bmc, certs):
    bmc.state.add(certs["microsoft"].read_bytes())
    bmc.state.add(certs["signer"].read_bytes())
    result = run_tool(bmc, certs["signer"])
    assert result.returncode == 0, result.stderr
    assert report(result)["action"] == "skipped"
    assert bmc.state.writes == 0
    assert len(bmc.state.certs) == 2


def test_refuses_without_microsoft_ca(bmc, certs):
    result = run_tool(bmc, certs["signer"])
    assert result.returncode == 1
    assert "Microsoft UEFI CA" in result.stderr
    assert bmc.state.writes == 0
    assert bmc.state.certs == []


def test_dry_run_never_writes(bmc, certs):
    bmc.state.add(certs["microsoft"].read_bytes())
    result = run_tool(bmc, certs["signer"], "--dry-run")
    assert result.returncode == 0, result.stderr
    assert report(result)["action"] == "dry-run"
    assert bmc.state.writes == 0
    assert len(bmc.state.certs) == 1


def test_weak_readback_is_reported(bmc, certs):
    bmc.state.add(certs["microsoft"].read_bytes())
    bmc.state.expose_string = False
    result = run_tool(bmc, certs["signer"])
    assert result.returncode == 0, result.stderr
    assert report(result)["verification"] == "weak"
    assert any("fingerprint" in warning for warning in report(result)["warnings"])


def test_unverifiable_readback_requires_opt_in(bmc, certs):
    bmc.state.add(certs["microsoft"].read_bytes())
    bmc.state.expose_string = False
    bmc.state.expose_identity = False
    result = run_tool(bmc, certs["signer"], "--allow-no-microsoft-ca")
    assert result.returncode == 2
    assert report(result)["verification"] == "none"

    result = run_tool(bmc, certs["signer"], "--allow-no-microsoft-ca", "--allow-unverified-readback")
    assert result.returncode == 0, result.stderr
    assert report(result)["verification"] == "none"
    assert any("duplicates" in warning for warning in report(result)["warnings"])
    assert len(bmc.state.certs) == 3


def test_unsupported_bmc_points_to_usb_fallback(bmc, certs):
    bmc.state.add(certs["microsoft"].read_bytes())
    bmc.state.post_status = 501
    result = run_tool(bmc, certs["signer"])
    assert result.returncode == 3
    assert "USB" in result.stderr
    assert bmc.state.writes == 0


def test_enable_secureboot_is_reported(bmc, certs):
    bmc.state.add(certs["microsoft"].read_bytes())
    result = run_tool(bmc, certs["signer"], "--enable-secureboot")
    assert result.returncode == 0, result.stderr
    assert bmc.state.enabled is True
    assert report(result)["secureboot_after"]["enabled"] is True


def test_tls_verification_is_on_by_default(bmc, certs):
    bmc.state.add(certs["microsoft"].read_bytes())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs["tls"], certs["tls"].with_suffix(".key"))
    bmc.socket = context.wrap_socket(bmc.socket, server_side=True)

    untrusted = run_tool(bmc, certs["signer"], tls=True)
    assert untrusted.returncode == 3, untrusted.stderr
    assert "CERTIFICATE_VERIFY_FAILED" in untrusted.stderr or "certificate verify failed" in untrusted.stderr

    trusted = subprocess.run(
        [str(TOOL), "--bmc", "127.0.0.1", "--port", str(bmc.server_address[1]),
         "--cert", str(certs["signer"]), "--ca-cert", str(certs["tls"]),
         "--timeout", "5", "--verify-retries", "1"],
        capture_output=True, text=True,
        env={**os.environ, "KURRENT_BMC_USER": USER, "KURRENT_BMC_PASSWORD": PASSWORD},
    )
    assert trusted.returncode == 0, trusted.stderr
    assert report(trusted)["action"] == "imported"


def test_report_file_gets_one_json_line(bmc, certs, tmp_path):
    bmc.state.add(certs["microsoft"].read_bytes())
    report_path = tmp_path / "enrolment.jsonl"
    result = run_tool(bmc, certs["signer"], "--report", str(report_path))
    assert result.returncode == 0, result.stderr
    lines = report_path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["action"] == "imported"
    assert report_path.stat().st_mode & 0o777 == 0o600


def test_credential_file_must_be_private(bmc, certs, tmp_path):
    bmc.state.add(certs["microsoft"].read_bytes())
    credential = tmp_path / "bmc.cred"
    credential.write_text(f"user={USER}\npassword={PASSWORD}\n")
    credential.chmod(0o644)
    result = subprocess.run(
        [str(TOOL), "--bmc", "127.0.0.1", "--port", str(bmc.server_address[1]),
         "--cert", str(certs["signer"]), "--scheme", "http", "--insecure",
         "--credential-file", str(credential)],
        capture_output=True, text=True,
        env={k: v for k, v in os.environ.items() if k not in ("KURRENT_BMC_USER", "KURRENT_BMC_PASSWORD")},
    )
    assert result.returncode == 1
    assert "0600" in result.stderr
