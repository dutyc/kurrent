"""审计日志端点（Bearer 鉴权）：游标分页读取 + 按设备 MAC 过滤。"""

from typing import Literal

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
def get_operations(since: int = 0, limit: int = 1000, mac: str | None = None,
                   op: str | None = None, order: Literal["asc", "desc"] = "asc"):
    """审计日志（游标分页）；mac/op 可选：规范化/精确匹配后仅返回对应记录，
    过滤在计数 limit 之前完成。order=desc 只取最新一页（新到旧），用于概览类查询。"""
    if mac is not None:
        mac = canonical_mac(mac)
    return operations.read(since=since, limit=limit, desc=order == "desc", mac=mac, op=op)
