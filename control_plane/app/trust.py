"""信任根域（T3/T4，2026-08-21）：注册窗口、强制验签开关、挑战 nonce、设备公钥/签名验证。

被 boot（引导链验签）与 settings（窗口/开关管理端点）两个域共用，独立成域。
契约见 blueprint/ipxe-stateless-handoff.md（§5.2 变量与接口契约）。
"""

import base64
import datetime as _dt
import hashlib
import re
import secrets
import threading
import time
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, EllipticCurvePublicKey

from .config import CHALLENGE_NONCE_TTL_SECONDS
from .stores import devices, record, runtime_settings
from .utils import normalize_boot_mac

# 挑战 nonce 存储：进程内存（单容器单进程），绑定 mac、短 TTL、一次性（消费即删）。
# 每个 MAC 可同时有多个未消费 nonce（上限见 NONCE_MAX_OUTSTANDING_PER_MAC），
# 防止无鉴权的 challenge 抢占唯一槽位、使真实设备的签名失效。
_nonce_store: dict[str, dict[str, float]] = {}
_nonce_lock = threading.Lock()
NONCE_MAX_OUTSTANDING_PER_MAC = 8


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
    窗口开启前的存量设备（认领 key_hash 者亦然）不计入。"""
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
    仅约束未知设备自动入池；密钥认领（存量补 key_hash）只看 window_open()。"""
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
    """设备身份验签强制开关（显式开关，2026-08-21 裁定）：开启后无密钥设备拒绝引导。"""
    return bool(runtime_settings.get("enforce_device_auth", False))


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


def parse_pubkey(value: str | None) -> str | None:
    """校验 ECDSA P-256 公钥（未压缩点 0x04||X||Y，65B，130 hex 字符；契约见 ipxe-stateless-handoff §5.2）。
    合法 → 规范化 hex；无参数/非法 → None（注册/认领拒绝，不阻断上报）。"""
    if not value:
        return None
    compact = value.strip().lower()
    if len(compact) != 130 or not re.fullmatch(r"[0-9a-f]{130}", compact):
        return None
    try:
        EllipticCurvePublicKey.from_encoded_point(SECP256R1(), bytes.fromhex(compact))
    except Exception:
        return None
    return compact


def verify_device_signature(mac: str, nonce: str, sig_b64: str, pubkey_hex: str) -> bool:
    """ECDSA P-256 验签：签名数据 = nonce||mac（UTF-8 字节拼接，契约见 ipxe-stateless-handoff §5.2）；
    签名 = base64(DER)；公钥 = 未压缩点 hex（key_hash）。任何异常 → False。

    签名不包含 hostname：hostname 由客户端自报且服务端不与台账比对，放进签名串不增加身份约束；
    身份由 MAC → 设备台账绑定与一次性 nonce 保证。"""
    try:
        pub = EllipticCurvePublicKey.from_encoded_point(SECP256R1(), bytes.fromhex(pubkey_hex))
        data = f"{nonce}{mac}".encode("utf-8")
        pub.verify(base64.b64decode(sig_b64), data, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def verify_rotate_signature(mac: str, nonce: str, new_pubkey_hex: str,
                            sig_b64: str, old_pubkey_hex: str) -> bool:
    """设备密钥轮换验签：签名数据 = rotate||mac||new_pubkey||nonce（无分隔拼接，
    与引导验签同一约定），签名者是旧密钥。任何异常 → False。"""
    try:
        pub = EllipticCurvePublicKey.from_encoded_point(
            SECP256R1(), bytes.fromhex(old_pubkey_hex))
        data = f"rotate{mac}{new_pubkey_hex}{nonce}".encode("utf-8")
        pub.verify(base64.b64decode(sig_b64), data, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_boot_identity(
    mac: str | None,
    nonce: str | None,
    sig: str | None,
    worker_id: str,
) -> tuple[bool, bool]:
    """设备身份签名验证（挑战-响应，trust-root-blueprint §6.1）。

    返回 (allowed, verified)：
    - allowed=False：拒绝下发引导变量。强制开启时无 mac / 无 key_hash / 无签名一律拒绝；
      带 nonce+sig 但重放或验签失败，任何模式下都拒绝。
    - verified=True：仅当 mac 在册、nonce 一次性且签名有效。只有该状态允许注入 NVMe-oF
      凭据（nbft_secret / nbft_tls_psk）；过渡期允许下发非凭据变量，但绝不注入凭据。
    无 mac 无法验证身份（仅 hostname 的识别链）：强制开启 → 拒绝，过渡期 → 允许但未验签。
    """
    normalized = normalize_boot_mac(mac) if mac else None
    if not normalized:
        if enforcement_enabled():
            record("boot_vars.credential", "rejected", mac="", worker_id=worker_id, reason="missing_mac")
            return False, False
        return True, False
    device = device_for_boot(normalized)
    key_hash = device.get("key_hash") if device else None
    if not key_hash:
        if enforcement_enabled():
            record("boot_vars.credential", "rejected", mac=normalized, worker_id=worker_id, reason="no_key")
            return False, False
        return True, False
    if not nonce or not sig:
        if enforcement_enabled():
            record("boot_vars.credential", "rejected", mac=normalized, worker_id=worker_id, reason="missing_sig")
            return False, False
        return True, False
    if not consume_nonce(normalized, nonce):
        record("boot_vars.credential", "rejected", mac=normalized, worker_id=worker_id, reason="nonce_invalid")
        return False, False
    if not verify_device_signature(normalized, nonce, sig, key_hash):
        record("boot_vars.credential", "rejected", mac=normalized, worker_id=worker_id, reason="verify_failed")
        return False, False
    return True, True
