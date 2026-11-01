"""认证体系 P2：三分根 CA、签发 profile、token 绑定、nginx 信任包。"""

import re
import shutil

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from control_plane.app import pki


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _cn(name) -> str:
    return name.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value


def test_ca_set_has_three_domains(tmp_path):
    ca_set = pki.ensure_ca_set(tmp_path)
    assert set(ca_set) == {"users", "nodes", "control"}
    for kind in ("users", "nodes", "control"):
        cert, key = ca_set[kind]
        assert _cn(cert.subject) == f"kurrent-ca-{kind}"
        assert (tmp_path / "ca" / kind / "ca.key").stat().st_mode & 0o777 == 0o600


def test_legacy_ca_promoted_to_nodes(tmp_path):
    """存量单 CA 原位提升为 nodes：不重新生成，现有组件证书继续有效。"""
    pki.ensure_ca_set(tmp_path)
    nodes_crt = (tmp_path / "ca" / "nodes" / "ca.crt").read_bytes()
    nodes_key = (tmp_path / "ca" / "nodes" / "ca.key").read_bytes()
    # 还原为迁移前布局：state/pki/ca.crt + ca.key
    (tmp_path / "ca.crt").write_bytes(nodes_crt)
    (tmp_path / "ca.key").write_bytes(nodes_key)
    shutil.rmtree(tmp_path / "ca")

    ca_set = pki.ensure_ca_set(tmp_path)
    assert ca_set["nodes"][0].public_bytes(serialization.Encoding.PEM) == nodes_crt
    assert not (tmp_path / "ca.crt").exists() and not (tmp_path / "ca.key").exists()
    assert _cn(ca_set["users"][0].subject) == "kurrent-ca-users"
    assert _cn(ca_set["control"][0].subject) == "kurrent-ca-control"


def test_component_csr_with_o_rejected(tmp_path):
    """组件 CSR 自报 O → 拒绝（组只能来自 users.yml）。"""
    ca_set = pki.ensure_ca_set(tmp_path)
    csr = pki._build_csr(_key(), "agent-abcdef012345", orgs=["admin"])
    with pytest.raises(ValueError, match="subject attribute not allowed"):
        pki.sign_component_certs(*ca_set["nodes"], "abcdef012345", "agent", csr, csr, [])


def test_component_cert_subject_is_cn_only_and_signed_by_nodes(tmp_path):
    ca_set = pki.ensure_ca_set(tmp_path)
    client_csr = pki._build_csr(_key(), "agent-abcdef012345")
    serving_csr = pki._build_csr(_key(), "agent-abcdef012345", ["127.0.0.1"])
    certs = pki.sign_component_certs(
        *ca_set["nodes"], "abcdef012345", "agent", client_csr, serving_csr, ["127.0.0.1"])
    cert = x509.load_pem_x509_certificate(certs["client.crt"])
    assert [attr.oid for attr in cert.subject] == [NameOID.COMMON_NAME]
    assert _cn(cert.issuer) == "kurrent-ca-nodes"


def test_nginx_bundle_contains_users_and_nodes_only(tmp_path):
    pki.ensure_ca_set(tmp_path)
    bundle = pki.write_nginx_trust_bundle(tmp_path)
    pem = bundle.read_bytes()
    blocks = re.findall(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.S)
    certs = [x509.load_pem_x509_certificate(block) for block in blocks]
    subjects = {_cn(c.subject) for c in certs}
    assert subjects == {"kurrent-ca-users", "kurrent-ca-nodes"}


def test_bootstrap_token_must_bind_agent(tmp_path):
    with pytest.raises(ValueError, match="must bind agent_id"):
        pki.issue_bootstrap_token(tmp_path)
    token = pki.issue_bootstrap_token(tmp_path, agent_id="23456789abcd")
    pki.validate_bootstrap_token(tmp_path, token, "23456789abcd", "agent")
    with pytest.raises(ValueError, match="agent mismatch"):
        pki.validate_bootstrap_token(tmp_path, token, "3456789abcde", "agent")
