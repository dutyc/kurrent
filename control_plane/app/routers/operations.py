"""审计日志端点（Bearer 鉴权）：游标分页读取 + 按设备 MAC 过滤。"""

from fastapi import Depends

from ..auth import authenticate, authorize_request
from ..stores import operations
from ..utils import canonical_mac

from ..principal import PrincipalRouter

router = PrincipalRouter(
    dependencies=[Depends(authenticate), Depends(authorize_request)],
    principal={"human", "automation"},
)


@router.get("/operations")
def get_operations(since: int = 0, limit: int = 1000, mac: str | None = None):
    """审计日志（游标分页）；mac 可选：规范化后仅返回该设备的操作（用于设备绑定记录查看）。"""
    if mac is not None:
        mac = canonical_mac(mac)
    result = operations.read(since=since, limit=limit)
    if mac is not None:
        result["entries"] = [e for e in result["entries"] if e.get("mac") == mac]
        result["next_cursor"] = result["entries"][-1]["id"] if result["entries"] else since
    return result
