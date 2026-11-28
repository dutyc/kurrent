"""TPM 2.0 quote 解析与验证。

固件在每次引导时用 AK 对 PCR 0/2/4/7 做 SHA-256 quote；签名以 DER 编码，
quote 本体为 TPMS_ATTEST 原始字节（不含 TPM2B_ATTEST 的 2 字节长度前缀）。
控制面用注册期 TOFU 记录的 AK 公钥验签，再把 pcrDigest 与平台基线比较。

本模块只做纯函数校验，不读写状态、不记录审计。
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import load_der_public_key

TPM_GENERATED_VALUE = 0xFF544347
TPM_ST_ATTEST_QUOTE = 0x8018
TPM_ALG_SHA256 = 0x000B

PCR_SELECTION = (0, 2, 4, 7)
PCR_SELECT_BYTES = 3
PCR_DIGEST_LEN = 32

CLOCK_INFO_LEN = 17
MAX_PCR_SELECTION_COUNT = 16

NO_TPM = "no_tpm"
AK_UNREGISTERED = "ak_unregistered"
NONCE_INVALID = "nonce_invalid"
QUOTE_INVALID = "quote_invalid"
PCR_MISMATCH = "pcr_mismatch"
SB_DISABLED = "sb_disabled"

AK_TYPE_ECC_P256 = "ecc_p256"
AK_TYPE_RSA2048 = "rsa2048"
AK_TYPES = (AK_TYPE_ECC_P256, AK_TYPE_RSA2048)


class QuoteError(Exception):
    """quote 校验失败，reason 为稳定的审计原因码。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, count: int) -> bytes:
        end = self.pos + count
        if count < 0 or end > len(self.data):
            raise QuoteError(QUOTE_INVALID)
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return int.from_bytes(self.take(2), "big")

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "big")

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "big")

    def tpm2b(self) -> bytes:
        return self.take(self.u16())


def _expected_bitmap() -> bytes:
    bitmap = bytearray(PCR_SELECT_BYTES)
    for pcr in PCR_SELECTION:
        bitmap[pcr // 8] |= 1 << (pcr % 8)
    return bytes(bitmap)


def parse_quote(blob: bytes) -> dict:
    """解析 TPMS_ATTEST，返回 quote 字段；任何格式偏差都抛 QuoteError。"""
    reader = _Reader(blob)
    magic = reader.u32()
    if magic != TPM_GENERATED_VALUE:
        raise QuoteError(QUOTE_INVALID)
    if reader.u16() != TPM_ST_ATTEST_QUOTE:
        raise QuoteError(QUOTE_INVALID)

    qualified_signer = reader.tpm2b()
    extra_data = reader.tpm2b()
    clock_info = reader.take(CLOCK_INFO_LEN)
    firmware_version = reader.u64()

    count = reader.u32()
    if count > MAX_PCR_SELECTION_COUNT:
        raise QuoteError(QUOTE_INVALID)
    selection = []
    for _ in range(count):
        alg = reader.u16()
        size = reader.u8()
        selection.append((alg, reader.take(size)))

    pcr_digest = reader.tpm2b()
    if reader.pos != len(blob):
        raise QuoteError(QUOTE_INVALID)

    return {
        "qualified_signer": qualified_signer,
        "extra_data": extra_data,
        "pcr_selection": selection,
        "pcr_digest": pcr_digest,
        "clock_info": clock_info,
        "firmware_version": firmware_version,
    }


def _verify_signature(key, ak_type: str, blob: bytes, signature: bytes) -> None:
    if ak_type == AK_TYPE_ECC_P256:
        if not isinstance(key, ec.EllipticCurvePublicKey):
            raise QuoteError(QUOTE_INVALID)
        try:
            key.verify(signature, blob, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature as exc:
            raise QuoteError(QUOTE_INVALID) from exc
        return
    if ak_type == AK_TYPE_RSA2048:
        if not isinstance(key, rsa.RSAPublicKey):
            raise QuoteError(QUOTE_INVALID)
        try:
            key.verify(signature, blob, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as exc:
            raise QuoteError(QUOTE_INVALID) from exc
        return
    raise QuoteError(QUOTE_INVALID)


def verify_quote(
    ak_public_hex: str,
    ak_type: str,
    blob_hex: str,
    signature_hex: str,
    nonce: str,
) -> dict:
    """验证 quote 的签名、nonce 绑定与 PCR 选择，返回 pcr_digest 等字段。

    extraData 与新件 nonce 不一致说明 quote 是为别的挑战做的，按 nonce 失败处理；
    签名或结构问题按 quote 失败处理。基线比较由调用方完成。
    """
    try:
        key = load_der_public_key(bytes.fromhex(ak_public_hex))
    except (ValueError, TypeError) as exc:
        raise QuoteError(QUOTE_INVALID) from exc

    try:
        blob = bytes.fromhex(blob_hex)
        signature = bytes.fromhex(signature_hex)
    except (ValueError, TypeError) as exc:
        raise QuoteError(QUOTE_INVALID) from exc

    parsed = parse_quote(blob)
    _verify_signature(key, ak_type, blob, signature)

    if parsed["extra_data"] != nonce.encode("utf-8"):
        raise QuoteError(NONCE_INVALID)

    selection = parsed["pcr_selection"]
    if len(selection) != 1 or selection[0][0] != TPM_ALG_SHA256:
        raise QuoteError(QUOTE_INVALID)
    if selection[0][1] != _expected_bitmap():
        raise QuoteError(QUOTE_INVALID)

    digest = parsed["pcr_digest"]
    if len(digest) != PCR_DIGEST_LEN:
        raise QuoteError(QUOTE_INVALID)

    return {
        "pcr_digest": digest.hex(),
        "qualified_signer": parsed["qualified_signer"].hex(),
        "firmware_version": parsed["firmware_version"],
        "clock_info": parsed["clock_info"].hex(),
    }


def baseline_matches(digest_hex: str, baseline_hex: str | None) -> bool:
    """pcrDigest 是否与已记录的平台基线一致；无基线视为不匹配。"""
    if not baseline_hex:
        return False
    return digest_hex.strip().lower() == str(baseline_hex).strip().lower()
