"""主体类与路由授权矩阵（认证体系 P2）。

主体类由凭据类型决定，不由字符串自报：
- nginx 只在 mTLS 链校验 SUCCESS 时按 issuer 注入 X-Client-Class（human/component）；
- Bearer → automation；会话 Cookie → human；无凭据 → anonymous。

路由在定义时声明允许的主体类；未声明在导入期失败（默认拒绝）。全局依赖在第一层
比对主体类与路由声明：已确定的错误类返回 403；anonymous 不在这里拦，留给
authenticate 返回 401（保持 CLI 的凭据发现语义）。
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.routing import APIRoute

from .auth import SESSION_COOKIE
from .config import CLIENT_CLASS_HEADER, settings

PRINCIPAL_CLASSES = frozenset(
    {"anonymous", "device", "component", "control", "human", "automation"}
)


def _normalize(allowed) -> frozenset[str]:
    if isinstance(allowed, str):
        allowed = {allowed}
    return frozenset(allowed)


def principal(*classes):
    """路由级主体类声明；必须放在 @router.get/post 之内（先于路由注册执行）。"""
    allowed = _normalize(classes)
    if not allowed:
        raise ValueError("principal declaration cannot be empty")

    def decorator(endpoint):
        endpoint.__principal__ = allowed
        return endpoint

    return decorator


class PrincipalRoute(APIRoute):
    """路由必须带 principal 声明；缺失时在应用装配期抛错（容器起不来）。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        allowed = getattr(self.endpoint, "__principal__", None)
        if not allowed:
            raise RuntimeError(f"route {self.path} missing principal declaration")
        unknown = set(allowed) - PRINCIPAL_CLASSES
        if unknown:
            raise RuntimeError(f"route {self.path} has unknown principal classes: {sorted(unknown)}")
        self.principal = frozenset(allowed)


class PrincipalRouter(APIRouter):
    """在路由器级给主体类默认值；单条路由可用 principal= 覆盖。"""

    def __init__(self, *args, principal=None, **kwargs):
        self._default_principal = _normalize(principal) if principal is not None else None
        kwargs.setdefault("route_class", PrincipalRoute)
        super().__init__(*args, **kwargs)

    def add_api_route(self, path, endpoint, *, principal=None, **kwargs):
        allowed = _normalize(principal) if principal is not None else getattr(endpoint, "__principal__", None)
        if not allowed:
            allowed = self._default_principal
        if not allowed:
            raise RuntimeError(f"route {path} missing principal declaration")
        endpoint.__principal__ = allowed
        super().add_api_route(path, endpoint, **kwargs)


def principal_class(request: Request) -> str:
    """认证结果的主体类；只接受 nginx 注入的 class，不解析 DN 语义。"""
    if settings.insecure:
        return "human"
    cls = request.headers.get(CLIENT_CLASS_HEADER, "").strip().lower()
    if cls in PRINCIPAL_CLASSES:
        return cls
    if request.headers.get("Authorization", "").startswith("Bearer "):
        return "automation"
    if SESSION_COOKIE in request.cookies:
        return "human"
    return "anonymous"


def enforce_principal(request: Request) -> None:
    """全局依赖：主体类不在路由允许集合内 → 403（身份已确定，权限不足）。"""
    if settings.insecure:
        return
    allowed = getattr(request.scope.get("endpoint"), "__principal__", None)
    if not allowed:
        return
    cls = principal_class(request)
    if cls == "anonymous":
        return
    if cls not in allowed:
        raise HTTPException(status_code=403, detail=f"forbidden: principal {cls}")
