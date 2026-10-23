"""认证审计中间件：每请求一条 allow/deny 事件（写失败 → 503，fail-closed）。

请求身份由 authenticate 写入 request.state.identity；认证失败时用 nginx 注入的证书
证据（issuer/serial/fingerprint/verify）补全审计字段。引导链端点与健康检查不在
管理面审计范围（它们的凭据证据由 boot_vars.credential / enroll 事件单独记录）。
"""

import logging
import uuid

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import audit, pki
from .principal import principal_class
from .utils import client_source

log = logging.getLogger("control-plane")

PUBLIC_PATHS = {"/healthz", "/boot-vars", "/devices/report", "/devices/challenge"}
COMPONENT_CLASSES = {"component", "control"}


def install(app) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _record_detail(request, exc: HTTPException):
        request.state.auth_error = str(exc.detail)
        # 保留原异常头（如 429 的 Retry-After）：审计中间件只补记，不改响应语义
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail},
                            headers=getattr(exc, "headers", None))

    @app.middleware("http")
    async def audit_request(request, call_next):
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        path = request.url.path
        if path in PUBLIC_PATHS:
            return await call_next(request)

        response = await call_next(request)
        status = response.status_code
        decision = "deny" if status in (401, 403) else "allow"
        reason = getattr(request.state, "auth_error", "") if decision == "deny" else ""
        ident = getattr(request.state, "identity", None)
        cls = ident.principal_class if ident else principal_class(request)

        cert_issuer = (request.headers.get("X-Client-Cert-Issuer", "")
                       or request.headers.get("X-Client-Cert-Raw-Issuer", ""))
        cert_serial = request.headers.get("X-Client-Cert-Serial", "")
        cert_fp = request.headers.get("X-Client-Cert-Fingerprint", "")
        if ident is not None and ident.method in {"cert", "session"}:
            principal = ident.name
            issuer = audit.issuer_name(ident.issuer or cert_issuer) or "session"
            serial = ident.serial or cert_serial
            fingerprint = ident.fingerprint or cert_fp
        elif ident is not None:
            principal = ident.name
            issuer = ident.issuer or ident.method
            serial, fingerprint = ident.serial, ident.fingerprint
        else:
            principal = pki.parse_dn_cn(request.headers.get("X-Client-Cert-DN", ""))
            issuer = audit.issuer_name(cert_issuer)
            serial, fingerprint = cert_serial, cert_fp

        event = "credential" if path.startswith("/enroll") else (
            "authz" if status == 403 else "authn")
        client_ip = client_source(request)
        try:
            audit.emit(
                event, principal_class=cls, principal=principal, issuer=issuer,
                serial=serial, fingerprint_sha256=fingerprint,
                route=f"{request.method} {path}", decision=decision, reason=reason,
                request_id=request_id, client_ip=client_ip,
            )
        except Exception:
            log.exception("audit write failed; rejecting %s %s", request.method, path)
            audit.alarm("audit_write_failure", detail=f"{request.method} {path}",
                        route=path)
            return JSONResponse(status_code=503, content={"detail": "audit unavailable"},
                                headers={"X-Request-Id": request_id})

        response.headers["X-Request-Id"] = request_id
        audit.check_users_file_change()
        if decision == "deny":
            if cls in COMPONENT_CLASSES and not path.startswith("/enroll"):
                audit.alarm("component_on_management_route",
                            detail=f"class={cls} status={status}", route=path,
                            principal=principal)
            audit.note_auth_failure(
                client_ip, route=f"{request.method} {path}", reason=reason,
                verify=request.headers.get("X-Client-Cert-Verify", ""),
                issuer=issuer, serial=serial, fingerprint=fingerprint,
            )
        return response
