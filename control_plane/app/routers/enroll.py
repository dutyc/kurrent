"""组件 PKI 引导/轮换接口。

- POST /enroll：组件首次注册。凭据 = bootstrap token（Bearer 头，经 nginx TLS 传输）：
  token 必须绑定 component + agent_id（kurrent token create --agent-id 签发）；
  nvmet-host 的 token 由 agent enroll 按能力派生（同样绑 agent_id）。
  校验通过后用 nodes CA 签发 client + serving 证书，响应同时下发 nodes/control 两个
  信任锚（节点用 control CA 校验控制面客户端，用 nodes CA 校验同伴组件）。
- POST /renew：证书轮换。凭据 = 组件现有 client cert（nginx 该路径强制 mTLS，
  issuer 必须是 ca-nodes，class=component）；校验 CN == <prefix>-<agent_id> 后重签。
  **在册校验只对非 agent 组件成立**：nvmet-host 要求在册（否则 400），而 agent
  组件与 /enroll 同语义（_issue 内自动登记）——因此从 agents.yml 移除一个
  agent 并不会拒绝它的 renew，它会在下次轮换时按节点声明重新登记。

两个接口都不挂 verify_control_token（那是 webui 的鉴权；组件凭据是证书/token 本身）。
"""

import logging

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel, field_validator

from .. import audit, config, pki, stores
from ..auth import settings
from ..principal import PrincipalRouter, principal
from ..utils import AGENT_ID_RE, client_host

log = logging.getLogger("control-plane")

router = PrincipalRouter(prefix="/enroll", tags=["enroll"])


class EnrollReq(BaseModel):
    agent_id: str
    component: str  # agent | nvmet-host
    csr_client: str  # PEM，CN = <prefix>-<agent_id>
    csr_serving: str  # PEM，CN 同上；签发 serving cert 用
    serving_sans: list[str] = []
    base_url: str = ""  # 控制面可达地址；agent 首次引导时上报，自动登记写入
    remark: str = ""  # 备注（人类识别用）；agent 首次引导时上报，随自动登记写入
    capabilities: dict = {}  # agent 上报自身能力（backend/cd），自动登记推导角色/标签；旧 agent 可缺省

    @field_validator("agent_id")
    @classmethod
    def _check_agent_id(cls, value: str) -> str:
        if not AGENT_ID_RE.fullmatch(value):
            raise ValueError("agent_id must be 12 lowercase hex characters")
        return value


def _issue(agent_id: str, component: str, req: EnrollReq) -> dict:
    """校验在册（agent 组件自动登记）+ 签发，返回 {certificates, ca_crt}。"""
    try:
        stores.agents.get(agent_id)  # agents.yml 在册校验（两类组件共用）
    except KeyError:
        if component != "agent":
            # nvmet-host 与 agent 共享 agent_id：须 agent 组件先行在册（部署序
            # 保证 agent 先引导；nvmet-host 容器 restart 重试即可）
            raise HTTPException(400, f"agent not registered: {agent_id}") from None
        # 自动登记（首次引导自动注册）：默认 disk 角色，
        # base_url 由 agent 引导时上报（声明 spec.agent.advertiseUrl），加入后可改；
        # remark（声明 metadata.remark）随首次登记写入，后续在台账侧修改；
        # 初始标签：agent 上报能力 → tags=[auto,storage]，此后归人工；
        # cd 能力 → role.cd；旧 agent 不上报时兼容默认（仅 auto 标签、无 cd 角色）。
        # 后端类型不进 tags：它是节点运行事实，由 /boot-vars 实时查 /capabilities 判定
        # （标签与实况漂移且标签优先，会拼出错误的 iSCSI 连接符）。
        base_url = req.base_url.strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            raise HTTPException(400, "base_url must start with http:// or https://")
        backend = str(req.capabilities.get("backend") or "").strip()
        tags = ("auto", "storage") if backend else ("auto",)
        with stores.store.locked():
            stores.agents.add(agent_id, base_url, role_disk=True,
                              role_cd=bool(req.capabilities.get("cd")),
                              enabled=True, tags=tags, remark=req.remark.strip())
        log.info("enroll: auto-registered agent %s (base_url=%s, backend=%s)",
                 agent_id, base_url, backend or "-")
    else:
        # 已在册：按节点声明刷新声明拥有的字段（A2 裁定：声明权威——storager/kurrent.yaml
        # 是节点事实的来源，enroll/renew 同路径覆盖，节点改地址/备注后对齐不再依赖重装）。
        # 控制面属性（enabled/storager_ip/tags）不碰；旧 agent 未上报的字段保留现有值。
        # 仅 agent 组件刷新（nvmet-host 的请求体不承载节点声明字段）。
        if component == "agent":
            with stores.store.locked():
                changed = stores.agents.refresh_from_declaration(
                    agent_id,
                    base_url=req.base_url,
                    role_cd=bool(req.capabilities["cd"]) if "cd" in req.capabilities else None,
                    remark=req.remark,
                )
            if changed:
                log.info("enroll: refreshed agent %s from node declaration "
                         "(base_url/remark/role.cd)", agent_id)

    ca_set = pki.ensure_ca_set(config.settings.pki_dir)
    ca_cert, ca_key = ca_set["nodes"]
    try:
        certs = pki.sign_component_certs(
            ca_cert, ca_key, agent_id, component,
            req.csr_client.encode(), req.csr_serving.encode(), req.serving_sans,
            pki_dir=config.settings.pki_dir,
        )
    except ValueError as exc:
        audit.alarm("bootstrap_cn_mismatch", detail=str(exc),
                    route="POST /enroll", principal=agent_id)
        raise HTTPException(400, str(exc)) from exc
    resp = {
        "certificates": {name: pem.decode() for name, pem in certs.items()},
        # 节点信任锚：control 用于校验控制面客户端；nodes 用于校验同伴组件
        "ca_nodes_crt": pki.ca_cert_pem(config.settings.pki_dir, "nodes").decode(),
        "ca_control_crt": pki.ca_cert_pem(config.settings.pki_dir, "control").decode(),
        # 组件侧证书轮换阈值（声明 spec.pki.renewThreshold）：节点落盘后按它决定
        # 剩余生命周期低于多少时 renew；旧节点忽略该字段（保留内置默认）
        "renew_threshold": config.settings.renew_threshold,
    }
    # 首次引导即拿到吊销表：入站按 serial 校验 peer，出站 TLS 加载同一份。
    # CRL 生成失败不让 enroll 失败（节点会以 degraded 状态启动并重试拉取）。
    try:
        resp.update(pki.component_crls(config.settings.pki_dir))
    except Exception:
        log.exception("enroll: component CRL bundle unavailable")
    # nvmet-host 组件凭据派生（能力上报驱动，签发不预知后端）：agent 上报
    # backend=nvmet → 签发绑 agent_id 的派生 token 随响应下发（agent 落盘供
    # nvmet-host 容器引导）；派生前清理旧条目（幂等重建）；stgt/lio 无该组件不派生。
    # enroll 与 renew 同路径：agent 证书轮换时顺带刷新派生 token。
    if component == "agent" and str(req.capabilities.get("backend") or "").strip() == "nvmet":
        pki.revoke_agent_tokens(config.settings.pki_dir, agent_id, "nvmet-host")
        resp["nvmet_token"] = pki.issue_bootstrap_token(
            config.settings.pki_dir, agent_id, "nvmet-host")
        log.info("enroll: derived nvmet-host bootstrap token for %s", agent_id)
    return resp


@router.post("", status_code=201)
@principal("anonymous", "automation")
def enroll(req: EnrollReq, request: Request, authorization: str = Header("", alias="Authorization")):
    """首次引导：Bearer bootstrap token → 原子占额度 → 签发；签发失败退回额度。"""
    token = authorization[len("Bearer "):].strip() if authorization.startswith("Bearer ") else ""
    # nvmet-host 与 agent 共享 agent_id：agent 未在册时先拒绝（不消耗额度，
    # 部署序保证 agent 先引导；nvmet-host 容器 restart 重试即可）
    if req.component != "agent":
        try:
            stores.agents.get(req.agent_id)
        except KeyError:
            raise HTTPException(400, f"agent not registered: {req.agent_id}") from None
    try:
        token_id = pki.reserve_bootstrap_token(
            config.settings.pki_dir, token, req.agent_id, req.component)
    except ValueError as exc:
        if "mismatch" in str(exc):
            audit.alarm("bootstrap_identity_mismatch", detail=str(exc),
                        route="POST /enroll", principal=req.agent_id)
        audit.emit("credential", principal_class="anonymous", principal=req.agent_id,
                   issuer="token", route="POST /enroll", decision="deny",
                   reason=str(exc), request_id=getattr(request.state, "request_id", ""),
                   client_ip=client_host(request))
        raise HTTPException(401, str(exc)) from exc
    audit.emit("credential", principal_class="anonymous", principal=req.agent_id,
               issuer="token", route="POST /enroll", decision="allow",
               reason="bootstrap_token", token_id=token_id,
               request_id=getattr(request.state, "request_id", ""),
               client_ip=client_host(request))
    try:
        return _issue(req.agent_id, req.component, req)
    except Exception:
        pki.release_bootstrap_token(config.settings.pki_dir, token_id)
        raise


@router.get("/crl")
@principal("component")
def component_crl() -> dict:
    """组件定期拉取吊销表（nodes + control），无需重签或重启即可生效。

    nginx 只放行 class=component 的 mTLS 请求；返回 PEM 与 next_update，
    节点侧解析后用于入站 serial 校验与出站 TLS 上下文。
    """
    return pki.component_crls(config.settings.pki_dir)


@router.post("/renew")
@principal("component")
def renew(req: EnrollReq,
          x_client_cert_dn: str = Header("", alias=config.CLIENT_CERT_DN_HEADER),
          x_client_cert_serial: str = Header("", alias=config.CLIENT_CERT_SERIAL_HEADER),
          x_client_class: str = Header("", alias=config.CLIENT_CLASS_HEADER)):
    """证书轮换：nginx 按 issuer 判定 class=component 后透传 DN；CN 匹配才重签。

    已吊销序列号一律拒绝（403）：nginx 的 CRL 校验在握手层，但 CRL reload 存在
    窗口，应用侧按登记表兜底，避免吊销后的证书换出一张新的有效证书。
    """
    if x_client_class != "component":
        raise HTTPException(403, f"renew requires a component certificate (class={x_client_class!r})")
    cn = pki.parse_dn_cn(x_client_cert_dn)
    prefix = pki.COMPONENT_PREFIX.get(req.component)
    if not prefix or cn != f"{prefix}-{req.agent_id}":
        raise HTTPException(401, f"client cert cn mismatch: {cn!r}")
    if x_client_cert_serial and pki.component_cert_revoked(config.settings.pki_dir,
                                                           x_client_cert_serial):
        audit.alarm("unknown_issuer_or_crl", detail="renew with revoked serial",
                    route="POST /enroll/renew", principal=req.agent_id)
        raise HTTPException(403, f"certificate revoked: serial {x_client_cert_serial}")
    return _issue(req.agent_id, req.component, req)
