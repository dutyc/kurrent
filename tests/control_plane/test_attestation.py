"""TPM quote 解析与验签的单元测试。

quote 向量按 TPM 2.0 结构手工构造（TPMS_ATTEST + DER 签名），用于验证解析器
与验签逻辑；真实 TPM 的端到端验证在 swtpm 环境单独进行。
"""

import json
import struct
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from control_plane.app import attestation as att

PCR0_2_4_7 = bytes([0x95, 0x00, 0x00])
NONCE = "a" * 64


def _tpm2b(payload: bytes) -> bytes:
    return struct.pack(">H", len(payload)) + payload


def build_quote(
    extra_data: bytes = NONCE.encode(),
    digest: bytes = b"\x11" * 32,
    *,
    magic: int = att.TPM_GENERATED_VALUE,
    attest_type: int = att.TPM_ST_ATTEST_QUOTE,
    alg: int = att.TPM_ALG_SHA256,
    bitmap: bytes = PCR0_2_4_7,
    qualified_signer: bytes = b"\x00\x0b" + b"\xaa" * 32,
    firmware_version: int = 0x0001000200030004,
) -> bytes:
    body = struct.pack(">IH", magic, attest_type)
    body += _tpm2b(qualified_signer)
    body += _tpm2b(extra_data)
    body += bytes(att.CLOCK_INFO_LEN)
    body += struct.pack(">Q", firmware_version)
    body += struct.pack(">I", 1)
    body += struct.pack(">H", alg) + bytes([len(bitmap)]) + bitmap
    body += _tpm2b(digest)
    return body


@pytest.fixture(scope="module")
def ec_identity():
    key = ec.generate_private_key(ec.SECP256R1())
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return key, spki.hex()


@pytest.fixture(scope="module")
def rsa_identity():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return key, spki.hex()


class TestParseQuote:
    def test_parses_fields(self):
        blob = build_quote()
        parsed = att.parse_quote(blob)
        assert parsed["extra_data"] == NONCE.encode()
        assert parsed["pcr_digest"] == b"\x11" * 32
        assert parsed["pcr_selection"] == [(att.TPM_ALG_SHA256, PCR0_2_4_7)]
        assert parsed["firmware_version"] == 0x0001000200030004

    def test_rejects_bad_magic(self):
        with pytest.raises(att.QuoteError) as exc:
            att.parse_quote(build_quote(magic=0x12345678))
        assert exc.value.reason == att.QUOTE_INVALID

    def test_rejects_non_quote_type(self):
        with pytest.raises(att.QuoteError) as exc:
            att.parse_quote(build_quote(attest_type=0x8017))
        assert exc.value.reason == att.QUOTE_INVALID

    def test_rejects_truncated(self):
        with pytest.raises(att.QuoteError) as exc:
            att.parse_quote(build_quote()[:-1])
        assert exc.value.reason == att.QUOTE_INVALID

    def test_rejects_trailing_bytes(self):
        with pytest.raises(att.QuoteError) as exc:
            att.parse_quote(build_quote() + b"\x00")
        assert exc.value.reason == att.QUOTE_INVALID


class TestVerifyQuote:
    def test_ecc_quote_passes(self, ec_identity):
        key, spki = ec_identity
        blob = build_quote()
        signature = key.sign(blob, ec.ECDSA(hashes.SHA256()))
        result = att.verify_quote(spki, att.AK_TYPE_ECC_P256, blob.hex(), signature.hex(), NONCE)
        assert result["pcr_digest"] == "11" * 32
        assert result["firmware_version"] == 0x0001000200030004

    def test_rsa_quote_passes(self, rsa_identity):
        key, spki = rsa_identity
        blob = build_quote()
        signature = key.sign(blob, padding.PKCS1v15(), hashes.SHA256())
        result = att.verify_quote(spki, att.AK_TYPE_RSA2048, blob.hex(), signature.hex(), NONCE)
        assert result["pcr_digest"] == "11" * 32

    def test_rejects_wrong_signature(self, ec_identity):
        _, spki = ec_identity
        blob = build_quote()
        other = ec.generate_private_key(ec.SECP256R1())
        signature = other.sign(blob, ec.ECDSA(hashes.SHA256()))
        with pytest.raises(att.QuoteError) as exc:
            att.verify_quote(spki, att.AK_TYPE_ECC_P256, blob.hex(), signature.hex(), NONCE)
        assert exc.value.reason == att.QUOTE_INVALID

    def test_rejects_tampered_blob(self, ec_identity):
        key, spki = ec_identity
        blob = build_quote()
        signature = key.sign(blob, ec.ECDSA(hashes.SHA256()))
        tampered = bytearray(blob)
        tampered[-1] ^= 0x01
        with pytest.raises(att.QuoteError) as exc:
            att.verify_quote(
                spki, att.AK_TYPE_ECC_P256, bytes(tampered).hex(), signature.hex(), NONCE
            )
        assert exc.value.reason == att.QUOTE_INVALID

    def test_rejects_nonce_mismatch(self, ec_identity):
        key, spki = ec_identity
        blob = build_quote(extra_data=b"b" * 64)
        signature = key.sign(blob, ec.ECDSA(hashes.SHA256()))
        with pytest.raises(att.QuoteError) as exc:
            att.verify_quote(spki, att.AK_TYPE_ECC_P256, blob.hex(), signature.hex(), NONCE)
        assert exc.value.reason == att.NONCE_INVALID

    def test_rejects_extra_pcr(self, ec_identity):
        key, spki = ec_identity
        blob = build_quote(bitmap=bytes([0x97, 0x00, 0x00]))
        signature = key.sign(blob, ec.ECDSA(hashes.SHA256()))
        with pytest.raises(att.QuoteError) as exc:
            att.verify_quote(spki, att.AK_TYPE_ECC_P256, blob.hex(), signature.hex(), NONCE)
        assert exc.value.reason == att.QUOTE_INVALID

    def test_rejects_wrong_hash_algorithm(self, ec_identity):
        key, spki = ec_identity
        blob = build_quote(alg=0x0004)
        signature = key.sign(blob, ec.ECDSA(hashes.SHA256()))
        with pytest.raises(att.QuoteError) as exc:
            att.verify_quote(spki, att.AK_TYPE_ECC_P256, blob.hex(), signature.hex(), NONCE)
        assert exc.value.reason == att.QUOTE_INVALID

    def test_rejects_digest_length(self, ec_identity):
        key, spki = ec_identity
        blob = build_quote(digest=b"\x11" * 20)
        signature = key.sign(blob, ec.ECDSA(hashes.SHA256()))
        with pytest.raises(att.QuoteError) as exc:
            att.verify_quote(spki, att.AK_TYPE_ECC_P256, blob.hex(), signature.hex(), NONCE)
        assert exc.value.reason == att.QUOTE_INVALID

    def test_rejects_unknown_ak_type(self, ec_identity):
        key, spki = ec_identity
        blob = build_quote()
        signature = key.sign(blob, ec.ECDSA(hashes.SHA256()))
        with pytest.raises(att.QuoteError) as exc:
            att.verify_quote(spki, "tpm_unknown", blob.hex(), signature.hex(), NONCE)
        assert exc.value.reason == att.QUOTE_INVALID

    def test_rejects_malformed_hex(self, ec_identity):
        _, spki = ec_identity
        with pytest.raises(att.QuoteError) as exc:
            att.verify_quote(spki, att.AK_TYPE_ECC_P256, "zz", "zz", NONCE)
        assert exc.value.reason == att.QUOTE_INVALID


class TestBaseline:
    def test_matches_ignores_case(self):
        assert att.baseline_matches("AB" * 32, "ab" * 32)

    def test_mismatch(self):
        assert not att.baseline_matches("ab" * 32, "cd" * 32)

    def test_missing_baseline_does_not_match(self):
        assert not att.baseline_matches("ab" * 32, None)
        assert not att.baseline_matches("ab" * 32, "")


DATA = Path(__file__).with_name("data") / "swtpm_quote.json"


@pytest.fixture(scope="module")
def swtpm_vector():
    """swtpm 0.10.1 + OVMF 产出的真实 quote（AK 公钥、quote、DER 签名）。"""
    return json.loads(DATA.read_text(encoding="utf-8"))


class TestSwtpmVector:
    """真实 TPM quote：结构、nonce 绑定与签名都由 swtpm 生成，不是构造向量。"""

    def test_parses_real_quote(self, swtpm_vector):
        parsed = att.parse_quote(bytes.fromhex(swtpm_vector["quote"]))
        assert parsed["extra_data"] == swtpm_vector["nonce"].encode()
        assert parsed["pcr_selection"] == [(att.TPM_ALG_SHA256, PCR0_2_4_7)]
        assert parsed["pcr_digest"].hex() == swtpm_vector["pcr_digest"]

    def test_accepts_real_signature(self, swtpm_vector):
        att.verify_quote(
            swtpm_vector["ak_public"], att.AK_TYPE_ECC_P256,
            swtpm_vector["quote"], swtpm_vector["quote_sig"], swtpm_vector["nonce"],
        )

    def test_rejects_wrong_nonce(self, swtpm_vector):
        with pytest.raises(att.QuoteError) as exc:
            att.verify_quote(
                swtpm_vector["ak_public"], att.AK_TYPE_ECC_P256,
                swtpm_vector["quote"], swtpm_vector["quote_sig"], "b" * 64,
            )
        assert exc.value.reason == att.NONCE_INVALID

    def test_rejects_tampered_quote(self, swtpm_vector):
        blob = bytearray.fromhex(swtpm_vector["quote"])
        blob[-1] ^= 0x01
        with pytest.raises(att.QuoteError) as exc:
            att.verify_quote(
                swtpm_vector["ak_public"], att.AK_TYPE_ECC_P256,
                blob.hex(), swtpm_vector["quote_sig"], swtpm_vector["nonce"],
            )
        assert exc.value.reason == att.QUOTE_INVALID

    def test_rejects_other_ak(self, swtpm_vector, ec_identity):
        _, other_spki = ec_identity
        with pytest.raises(att.QuoteError) as exc:
            att.verify_quote(
                other_spki, att.AK_TYPE_ECC_P256,
                swtpm_vector["quote"], swtpm_vector["quote_sig"], swtpm_vector["nonce"],
            )
        assert exc.value.reason == att.QUOTE_INVALID

    def test_baseline_compare(self, swtpm_vector):
        digest = swtpm_vector["pcr_digest"]
        assert att.baseline_matches(digest, digest)
        assert not att.baseline_matches(digest, "cd" * 32)
