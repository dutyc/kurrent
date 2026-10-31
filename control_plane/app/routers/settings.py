"""设置管理端点（Bearer 鉴权）：注册窗口开启/查询/关闭、设备身份验签强制开关。

窗口/开关状态逻辑在 trust 域（被 boot 验签链路共用），本模块仅 HTTP 壳。
"""

from fastapi import Depends, HTTPException, Request

from ..auth import authenticate, authorize_request
from ..config import REGISTRATION_WINDOW_TTL_MIN_MINUTES, settings
from ..models import OpenRegistrationWindowRequest, SetEnforcementRequest
from ..stores import record, runtime_settings
from ..trust import enforcement_enabled, window_open, window_record, window_status
from ..utils import client_host, now_iso

from ..principal import PrincipalRouter

router = PrincipalRouter(
    dependencies=[Depends(authenticate), Depends(authorize_request)],
    principal={"human", "automation"},
)


@router.get("/settings/registration-window")
def get_registration_window():
    """查询注册窗口状态：时间维度（open / 开启时刻 / TTL / 关闭时刻 / 剩余秒数）
    + 额度维度（max_devices / admitted / remaining_devices / quota_exhausted）
    + limits（当前声明的 TTL/额度上限，WebUI 按它做输入约束）。
    TTL 到期自动关闭（懒计算，不主动清理）；额度用尽不再放行新设备（不主动清窗口记录）。
    未配置/已关闭 → open=false。"""
    return _window_payload()


def _window_payload() -> dict:
    """窗口状态 + 当前声明的上限（WebUI 按 limits 收敛输入控件）。"""
    status = window_status()
    status["limits"] = {
        "ttl_minutes_min": REGISTRATION_WINDOW_TTL_MIN_MINUTES,
        "ttl_minutes_max": settings.registration_window_max_ttl_minutes,
        "max_devices": settings.registration_window_max_devices,
    }
    return status


@router.post("/settings/registration-window", status_code=201)
def open_registration_window(req: OpenRegistrationWindowRequest, request: Request):
    """开启注册窗口（部署期）：TTL 上限来自声明 spec.registration（不可配置为永久），
    注册只在窗口期可发生。max_devices 为放行额度（台），省略/null = 不限；已放行数
    派生自设备台账（窗口期内 source=ipxe 且 first_seen >= 开启时刻的条目），不另存计数器。
    已开启 → 409（先关闭再开）；过期残留记录可直接重开（覆盖）。"""
    ttl_max = settings.registration_window_max_ttl_minutes
    devices_max = settings.registration_window_max_devices
    if not (REGISTRATION_WINDOW_TTL_MIN_MINUTES <= req.ttl_minutes <= ttl_max):
        raise HTTPException(
            400,
            f"ttl_minutes must be within {REGISTRATION_WINDOW_TTL_MIN_MINUTES}-{ttl_max}",
        )
    if req.max_devices is not None and not (1 <= req.max_devices <= devices_max):
        raise HTTPException(400, f"max_devices must be within 1-{devices_max}")
    record_ = {"opened_at": now_iso(), "ttl_minutes": req.ttl_minutes, "max_devices": req.max_devices}
    with runtime_settings.locked():
        if window_open():
            raise HTTPException(409, "registration window already open")
        runtime_settings.set("registration_window", record_)
    record("settings.registration_window", "open", ttl_minutes=req.ttl_minutes,
           max_devices=req.max_devices, client=client_host(request))
    return _window_payload()


@router.delete("/settings/registration-window")
def close_registration_window(request: Request):
    """提前关闭注册窗口：窗口期外注册被拒。从未开启/无记录 → 409。"""
    with runtime_settings.locked():
        if not window_record():
            raise HTTPException(409, "registration window is not open")
        runtime_settings.set("registration_window", None)
    record("settings.registration_window", "close", client=client_host(request))
    return {"open": False}


@router.get("/settings/enforcement")
def get_enforcement():
    """查询设备身份验签强制开关（过渡期兼容：关闭时无密钥设备照现状放行）。"""
    return {"enabled": enforcement_enabled()}


@router.put("/settings/enforcement")
def set_enforcement(req: SetEnforcementRequest, request: Request):
    """切换设备身份验签强制开关：开启后无 key_hash 设备 /boot-vars 直接拒绝下发
    （注入四条件第 4 条硬性，已绑定也不放行）；存量设备认领完成后由管理员开启。"""
    enabled = bool(runtime_settings.set("enforce_device_auth", req.enabled))
    record("settings.enforcement", "ok", enabled=enabled, client=client_host(request))
    return {"enabled": enabled}
