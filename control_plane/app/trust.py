"""信任根域（T3/T4）：注册窗口、证明强制开关、挑战 nonce、TPM 证明验证。

被 boot（引导链证明）与 settings（窗口/开关管理端点）两个域共用，独立成域。
契约见 .local/blueprint/ipxe-stateless-handoff.md（§5.2 变量与接口契约）。
"""

import datetime as _dt
import hashlib
import re
import secrets
import threading
import time
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import load_der_public_key

from .attestation import (
    AK_TYPES,
    AK_TYPE_ECC_P256,
    AK_TYPE_RSA2048,
    AK_UNREGISTERED,
    NONCE_INVALID,
    PCR_MISMATCH,
    PCR_SELECTION,
    QUOTE_INVALID,
    QuoteError,
    baseline_matches,
    verify_quote,
)
from .config import CHALLENGE_NONCE_TTL_SECONDS
from .stores import devices, record, runtime_settings
from .utils import normalize_boot_mac, now_iso

# 挑战 nonce 存储：进程内存（单容器单进程），绑定 mac、短 TTL、一次性（消费即删）。
# 每个 MAC 可同时有多个未消费 nonce（上限见 NONCE_MAX_OUTSTANDING_PER_MAC），
# 防止无鉴权的 challenge 抢占唯一槽位、使真实设备的签名失效。
_nonce_store: dict[str, dict[str, float]] = {}
_nonce_lock = threading.Lock()
NONCE_MAX_OUTSTANDING_PER_MAC = 8

AK_NAME_HEX_LEN = 68  # TPM2B_NAME: 2 字节算法前缀 + 32 字节 SHA-256 摘要
AK_PUBLIC_MAX_HEX = 4096


def device_for_boot(mac: str | None) -> dict[str, Any] | None:
    """boot-vars 请求的 MAC 反查设备台账。"""
    normalized = normalize_boot_mac(mac) if mac else None
    if not normalized:
        return None
    data = devices.load()
    return data["devices"].get(normalized)


def window_record() -> dict[str, Any] | None:
    """注册窗口记录（settings.json registration_window）：无/非法 → None。"""
    rec = runtime_settings.get("registration_window")
    if not isinstance(rec, dict) or not rec.get("opened_at") or not rec.get("ttl_minutes"):
        return None
    return rec


def window_open() -> bool:
    """窗口开启判定：有记录且未过期（TTL 到期自动关闭，懒计算）。"""
    rec = window_record()
    if not rec:
        return False
    try:
        closes = _dt.datetime.fromisoformat(rec["opened_at"]) + _dt.timedelta(minutes=int(rec["ttl_minutes"]))
    except (ValueError, TypeError):
        return False
    return _dt.datetime.now().astimezone() < closes


def window_limit(rec: dict[str, Any] | None = None) -> int | None:
    """窗口放行额度（max_devices，台）：未记录/未配置/非法 → None（不限，仍受 TTL 约束）。
    额度语义见 boot 入池判定：只约束「未知设备自动入池」，不约束密钥认领与管理员手动登记。"""
    rec = rec if rec is not None else window_record()
    if not rec:
        return None
    limit = rec.get("max_devices")
    # bool 是 int 子类，显式排除；<=0 视为未配置（不引入「零额度」这种半开状态）
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return None
    return limit


def _count_admitted(devices_map: dict[str, Any], opened_at: str) -> int:
    """统计窗口期内已放行的设备数（派生自设备台账，不另存计数器）。
    口径：source == "ipxe" 且 first_seen >= 窗口开启时刻——即本窗口期内经 PXE 上报自动入池的条目；
    管理员手动登记 / 清单导入（source=manual）是显式动作，不占额度；
    窗口开启前的存量设备（认领 AK 者亦然）不计入。"""
    try:
        opened = _dt.datetime.fromisoformat(opened_at)
    except (ValueError, TypeError):
        return 0
    count = 0
    for device in devices_map.values():
        if not isinstance(device, dict) or device.get("source") != "ipxe":
            continue
        try:
            seen = _dt.datetime.fromisoformat(str(device.get("first_seen") or ""))
        except (ValueError, TypeError):
            continue
        if seen >= opened:
            count += 1
    return count


def window_quota_left(devices_map: dict[str, Any] | None = None) -> int | None:
    """放行额度余量：None = 不限；0 = 已用尽。
    传入 devices_map 时以调用方持锁加载的台账为准（boot 上报路径锁内复核，避免并发超额放行）。"""
    rec = window_record()
    if not rec:
        return 0
    limit = window_limit(rec)
    if limit is None:
        return None
    devices_map = devices_map if devices_map is not None else devices.load()["devices"]
    return max(0, limit - _count_admitted(devices_map, rec["opened_at"]))


def admission_allowed(devices_map: dict[str, Any] | None = None) -> bool:
    """入池放行判定：窗口开启 且 额度未用尽（额度缺省 = 不限）。
    仅约束未知设备自动入池；AK 认领（存量补身份）只看 window_open()。"""
    if not window_open():
        return False
    left = window_quota_left(devices_map)
    return left is None or left > 0


def window_status() -> dict[str, Any]:
    """窗口状态投影（管理端点用）：时间维度（open/remaining）+ 额度维度（max_devices/admitted/…）。"""
    empty = {
        "open": False,
        "opened_at": None,
        "ttl_minutes": None,
        "closes_at": None,
        "remaining_seconds": 0,
        "max_devices": None,
        "admitted": 0,
        "remaining_devices": None,
        "quota_exhausted": False,
    }
    rec = window_record()
    if not rec:
        return empty
    try:
        opened = _dt.datetime.fromisoformat(rec["opened_at"])
        closes = opened + _dt.timedelta(minutes=int(rec["ttl_minutes"]))
    except (ValueError, TypeError):
        return empty
    remaining = max(0, int((closes - _dt.datetime.now().astimezone()).total_seconds()))
    limit = window_limit(rec)
    admitted = _count_admitted(devices.load()["devices"], rec["opened_at"])
    return {
        "open": remaining > 0,
        "opened_at": rec["opened_at"],
        "ttl_minutes": int(rec["ttl_minutes"]),
        "closes_at": closes.isoformat(),
        "remaining_seconds": remaining,
        "max_devices": limit,
        "admitted": admitted,
        "remaining_devices": None if limit is None else max(0, limit - admitted),
        "quota_exhausted": limit is not None and admitted >= limit,
    }


def enforcement_enabled() -> bool:
    """设备身份验签强制开关（2026-08-21 裁定）：默认开启，无密钥设备拒绝引导。

    显式关闭仅用于过渡/排障（存量无密钥设备临时引导），凭据在任何模式下都不注入。
    """
    return bool(runtime_settings.get("enforce_device_auth", True))


def issue_nonce(mac: str) -> str:
    """签发一次性 nonce（32B，64 hex），绑定 mac、短 TTL。

    每个 MAC 保留多个未消费的 nonce（上限 NONCE_MAX_OUTSTANDING_PER_MAC）：
    challenge 是无鉴权端点，若只保留单槽，任何局域网主机都能反复挑战把它顶掉，
    使真实设备已签好的 nonce 失效（引导 DoS）。消费只删除命中的那一项。
    """
    nonce = secrets.token_hex(32)
    with _nonce_lock:
        now = time.time()
        expired = [m for m, e in _nonce_store.items() if not e or now > max(e.values())]
        for m in expired:
            del _nonce_store[m]
        entries = _nonce_store.setdefault(mac, {})
        if len(entries) >= NONCE_MAX_OUTSTANDING_PER_MAC:
            # 丢最早到期的一项（所有项 TTL 相同，即最早签发）；不按请求顺序影响其它设备
            del entries[min(entries, key=entries.get)]
        entries[nonce] = now + CHALLENGE_NONCE_TTL_SECONDS
    return nonce


def consume_nonce(mac: str, nonce: str) -> bool:
    """消费 nonce（一次性）；不存在/不匹配/过期 → False。

    只删除命中的那一项：不匹配时保留其它 nonce，无鉴权请求不能借错误 nonce
    清空设备的待用槽位。
    """
    with _nonce_lock:
        entries = _nonce_store.get(mac)
        if not entries:
            return False
        expires = entries.pop(nonce, None)
        if not entries:
            _nonce_store.pop(mac, None)
        return expires is not None and time.time() <= expires


def parse_ak_public(value: str | None, ak_type: str | None) -> str | None:
    """校验 AK 公钥：SPKI DER 的 hex，类型须与 ak_type 匹配。
    合法 → 规范化 hex；无参数/非法 → None（注册/认领拒绝，不阻断上报）。"""
    if not value or ak_type not in AK_TYPES:
        return None
    compact = value.strip().lower()
    if not compact or len(compact) > AK_PUBLIC_MAX_HEX:
        return None
    if len(compact) % 2 or not re.fullmatch(r"[0-9a-f]+", compact):
        return None
    try:
        key = load_der_public_key(bytes.fromhex(compact))
    except Exception:
        return None
    if ak_type == AK_TYPE_ECC_P256:
        return compact if isinstance(key, ec.EllipticCurvePublicKey) else None
    if ak_type == AK_TYPE_RSA2048:
        return compact if isinstance(key, rsa.RSAPublicKey) else None
    return None


def parse_ak_name(value: str | None) -> str | None:
    """校验 TPM2B_NAME：2 字节算法前缀 + 32 字节摘要，共 68 hex。"""
    if not value:
        return None
    compact = value.strip().lower()
    if len(compact) != AK_NAME_HEX_LEN or not re.fullmatch(r"[0-9a-f]+", compact):
        return None
    return compact


def _commit_attestation(mac: str, state: str, reason: str | None,
                        digest: str | None = None,
                        pending_digest: str | None = None) -> None:
    """写回证明状态；首次通过时记录 PCR 基线，基线不符时留下待批摘要。"""
    now = now_iso()
    with devices.locked():
        data = devices.load()
        device = data["devices"].get(mac)
        if not device:
            return
        attestation = {
            "state": state,
            "last_at": now,
            "last_reason": reason,
        }
        if pending_digest:
            attestation["pending_digest"] = pending_digest
        device["attestation"] = attestation
        if digest:
            device["pcr_baseline"] = {
                "selection": list(PCR_SELECTION),
                "digest_sha256": digest,
                "recorded_at": now,
            }
        devices.save(data)
    record("device.attest", "ok" if state == "proven" else "failed",
           mac=mac, state=state, **({"reason": reason} if reason else {}))


def verify_attestation(
    mac: str | None,
    nonce: str | None,
    quote: str | None,
    quote_sig: str | None,
    worker_id: str,
) -> tuple[bool, bool]:
    """TPM quote 验证：结构、签名、nonce 绑定、PCR 基线。

    返回 (allowed, verified)：
    - allowed=False：拒绝下发引导变量。强制开启（默认）时无 mac / 无 AK / 无 quote 一律拒绝；
      重放、签名错误、PCR 不符在任何模式下都拒绝。
    - verified=True：AK 在册、nonce 一次性、quote 签名有效且 PCR 与基线一致。
      只有该状态允许注入 NVMe-oF 凭据。

    顺序：先做无副作用的 quote 校验，再消费 nonce，避免格式错误也烧掉 nonce；
    最后比对基线，首次通过时记录基线（TOFU）。
    """
    normalized = normalize_boot_mac(mac) if mac else None
    if not normalized:
        if enforcement_enabled():
            record("boot_vars.credential", "rejected", mac="", worker_id=worker_id, reason="missing_mac")
            return False, False
        return True, False

    device = device_for_boot(normalized)
    identity = (device or {}).get("identity") or {}
    ak_public = identity.get("ak_public")
    ak_type = identity.get("ak_type")

    if not ak_public or not ak_type:
        if enforcement_enabled():
            record("boot_vars.credential", "rejected", mac=normalized, worker_id=worker_id,
                   reason=AK_UNREGISTERED)
            return False, False
        return True, False

    if not nonce or not quote or not quote_sig:
        if enforcement_enabled():
            record("boot_vars.credential", "rejected", mac=normalized, worker_id=worker_id,
                   reason=QUOTE_INVALID)
            return False, False
        return True, False

    try:
        result = verify_quote(ak_public, ak_type, quote, quote_sig, nonce)
    except QuoteError as exc:
        record("boot_vars.credential", "rejected", mac=normalized, worker_id=worker_id,
               reason=exc.reason)
        _commit_attestation(normalized, "unproven", exc.reason)
        return False, False

    if not consume_nonce(normalized, nonce):
        record("boot_vars.credential", "rejected", mac=normalized, worker_id=worker_id,
               reason=NONCE_INVALID)
        _commit_attestation(normalized, "unproven", NONCE_INVALID)
        return False, False

    baseline = ((device or {}).get("pcr_baseline") or {}).get("digest_sha256")
    if baseline:
        if not baseline_matches(result["pcr_digest"], baseline):
            record("boot_vars.credential", "rejected", mac=normalized, worker_id=worker_id,
                   reason=PCR_MISMATCH)
            _commit_attestation(normalized, "mismatch", PCR_MISMATCH,
                                pending_digest=result["pcr_digest"])
            return False, False
        _commit_attestation(normalized, "proven", None)
    else:
        _commit_attestation(normalized, "proven", None, digest=result["pcr_digest"])
    return True, True


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
