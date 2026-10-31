"""引导链端点（不鉴权）：healthz / boot-vars / devices/report / devices/challenge 及注入投影逻辑。"""

from typing import Any

from fastapi import HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from .. import audit, ratelimit, runtime_status
from ..principal import PrincipalRouter, principal

from ..config import settings
from ..stores import agents, credentials, devices, dnsmasq, record, store
from ..trust import (
    admission_allowed,
    consume_nonce,
    device_for_boot,
    issue_nonce,
    parse_pubkey,
    sha256_hex,
    verify_boot_identity,
    verify_rotate_signature,
    window_open,
)
from ..utils import (
    canonical_hostname,
    clean_str,
    client_host,
    client_source,
    default_disk_for,
    normalize_boot_mac,
    now_iso,
    parse_uint,
)
from .workers import MENU_NAV_ITEMS, _base_nqn_from_target, _host_nqn_for

router = PrincipalRouter()


@router.get("/healthz")
@principal("anonymous")
def healthz():
    """进程存活 + 声明派生自检：启动期派生失败返回 503（详情只在已认证 GET /config）。"""
    if runtime_status.errors():
        return JSONResponse(status_code=503, content={"status": "degraded"})
    return {"status": "ok"}


def _enforce_rate(scope: str, request: Request, route: str = "") -> None:
    """匿名引导端点限速：超限 429 + 告警；按来源 IP（见 ratelimit 模块）。"""
    source = client_source(request)
    retry = ratelimit.check(scope, source)
    if retry is None:
        return
    audit.alarm("boot_rate_limited", severity="medium",
                detail=f"{scope} from {source}", route=route or f"/devices/{scope}",
                client_ip=source)
    raise HTTPException(status_code=429, detail="too many requests",
                        headers={"Retry-After": str(int(retry))})


@router.get("/boot-vars")
@principal("anonymous")
def boot_vars(
    request: Request,
    mac: str | None = None,
    hostname: str | None = None,
    nonce: str | None = None,
    sig: str | None = None,
    output_format: str = Query("ipxe", alias="format"),
):
    _enforce_rate("boot_vars", request, route="/boot-vars")
    if output_format not in {"ipxe", "json"}:
        raise HTTPException(400, "format must be ipxe or json")
    # iPXE 脚本拼 URL 时不做百分号编码：base64 签名（DER）中的 '+' 原样进入 query，
    # Starlette 按 form-urlencoded 规则把 '+' 解码为空格导致验签失败（verify_failed）。
    # base64 字符集不含空格，将空格还原为 '+' 即可同时兼容 %2B 显式编码与未编码两种传递。
    if sig:
        sig = sig.replace(" ", "+")
    payload = _boot_vars_payload(mac=mac, hostname=hostname, nonce=nonce, sig=sig)
    if output_format == "json":
        return JSONResponse(_boot_vars_json(payload))
    return Response(_boot_vars_ipxe(payload), media_type="text/plain")


@router.get("/devices/report")
@principal("anonymous")
def device_report(
    request: Request,
    mac: str,
    uuid: str | None = Query(None),
    manufacturer: str | None = Query(None),
    product: str | None = Query(None),
    serial: str | None = Query(None),
    cpumodel: str | None = Query(None),
    mem_total: str | None = Query(None, alias="mem-total"),
    mem_type: str | None = Query(None, alias="mem-type"),
    mem_speed: str | None = Query(None, alias="mem-speed"),
    chip: str | None = Query(None),
    busid: str | None = Query(None),
    pubkey: str | None = Query(None),
):
    """iPXE 设备信息上报（不鉴权）：11 字段指纹 + 可带 pubkey（ECDSA P-256 公钥，注册/认领用），
    宽松解析（空值容忍，mem 兼容 0x hex/十进制），更新指纹 + last_seen。
    注册只在窗口期（2026-08-21 裁定）：未知 MAC 仅在窗口开启、额度未用尽且带有效公钥时入池；
    存量设备窗口期内带公钥上报 = 密钥认领（key_hash 填充，不占放行额度）。
    返回空响应（chain 无脚本副作用）。"""
    _enforce_rate("report", request)
    normalized = normalize_boot_mac(mac)
    if not normalized:
        return Response("#!ipxe\n", media_type="text/plain", status_code=200)  # 非法 MAC 忽略，不阻断引导
    fingerprint = {k: v for k, v in {
        "manufacturer": clean_str(manufacturer),
        "product": clean_str(product),
        "serial": clean_str(serial),
        "cpumodel": clean_str(cpumodel),
        "mem_total": parse_uint(mem_total),
        "mem_type": clean_str(mem_type),
        "mem_speed": parse_uint(mem_speed),
        "chip": clean_str(chip),
        "busid": clean_str(busid),
    }.items() if v is not None}
    now = now_iso()
    key_hex = parse_pubkey(pubkey)  # 合法 → 规范化 hex；无参数/非法 → None
    window_open_ = window_open()  # 密钥认领条件：只看窗口期，不看额度
    updated = False
    claimed = False
    with devices.locked():
        data = devices.load()
        devs = data["devices"]
        existing = devs.get(normalized)
        if existing:
            if existing.get("state") == "revoked":
                return Response("#!ipxe\n", media_type="text/plain", status_code=200)  # 吊销设备不更新、不复活
            existing.setdefault("fingerprint", {}).update(fingerprint)
            # 无鉴权上报只能补空：已有 UUID 是绑定比对依据，不允许被自报值覆盖
            clean_uuid = clean_str(uuid)
            if clean_uuid and not existing.get("uuid"):
                existing["uuid"] = clean_uuid
            existing["last_seen"] = now
            if window_open_ and key_hex:
                old_key = existing.get("key_hash")
                if old_key:
                    if old_key != key_hex:
                        # 密钥不一致：拒绝覆盖（吊销/重注册走删登记流程），仅审计
                        record("device.claim", "rejected", mac=normalized, reason="key_mismatch")
                else:
                    existing["key_hash"] = key_hex
                    existing["pubkey_hash"] = sha256_hex(bytes.fromhex(key_hex))
                    claimed = True
            devices.save(data)
            updated = True
        else:
            # 入池放行：窗口期 + 额度未用尽 + 带有效公钥。额度在锁内以本次加载的台账复核，
            # 避免并发上报各自读到余量 1 而超额放行（已放行数派生自台账，不另存计数器）。
            if not (key_hex and admission_allowed(devs)):
                return Response("#!ipxe\n", media_type="text/plain", status_code=200)
            devs[normalized] = {
                "mac": normalized,
                "uuid": clean_str(uuid),
                "state": "pooled",
                "bound_worker_id": None,
                "key_hash": key_hex,
                "pubkey_hash": sha256_hex(bytes.fromhex(key_hex)),
                "source": "ipxe",
                "fingerprint": fingerprint,
                "first_seen": now,
                "last_seen": now,
            }
            devices.save(data)
            try:
                dnsmasq.unblock(normalized)  # 清除历史遗留的 DHCP 封禁（清退后重新登记）
            except Exception:
                record("device.dhcp_unblock", "error", mac=normalized)
            updated = False
    if updated:
        record("device.report", "ok", mac=normalized, client=client_host(request), updated=True)
        if claimed:
            record("device.claim", "ok", mac=normalized)
    else:
        record("device.register", "ok", mac=normalized, source="ipxe", client=client_host(request))
    return Response("#!ipxe\n", media_type="text/plain", status_code=200)


@router.get("/devices/challenge")
@principal("anonymous")
def device_challenge(mac: str, request: Request):
    """挑战端点（不鉴权）：一次性 nonce，短 TTL、绑定 mac。nonce 本身无秘密，仅防重放。
    设备不存在/未认领/已吊销 → 404（吊销设备不得进入验签链路，也不给 nonce）。
    响应 #!ipxe 脚本体 set nonce <64hex>（iPXE chain 直接消费为 ${nonce}）。"""
    _enforce_rate("challenge", request)
    normalized = normalize_boot_mac(mac)
    if not normalized:
        raise HTTPException(400, "invalid mac")
    device = device_for_boot(normalized)
    if not device or not device.get("key_hash") or device.get("state") == "revoked":
        raise HTTPException(404, "device not registered or not claimed")
    nonce = issue_nonce(normalized)
    return Response(f"#!ipxe\nset nonce {nonce}\n", media_type="text/plain")


@router.post("/devices/rotate")
@principal("anonymous")
def device_rotate(request: Request, mac: str, pubkey: str, nonce: str, sig: str):
    """设备密钥轮换（不鉴权，凭旧密钥签名）：签名串 = rotate||mac||new_pubkey||nonce。

    nonce 由 /devices/challenge 签发（一次性消费）。成功后 key_hash 换成新公钥，
    旧密钥在 /devices/boot-vars 的验签链路上立即失效；不发新证书、不重开注册窗口。
    """
    _enforce_rate("rotate", request)
    normalized = normalize_boot_mac(mac)
    if not normalized:
        raise HTTPException(400, "invalid mac")
    new_key = parse_pubkey(pubkey)
    if not new_key:
        raise HTTPException(400, "invalid pubkey")
    if not nonce or not sig:
        raise HTTPException(400, "nonce and sig required")
    with devices.locked():
        data = devices.load()
        device = data["devices"].get(normalized)
        if not device:
            raise HTTPException(404, "device not found")
        if device.get("state") == "revoked":
            raise HTTPException(403, "device revoked")
        old_key = device.get("key_hash")
        if not old_key:
            raise HTTPException(409, "device has no key; claim it during the registration window first")
        if not consume_nonce(normalized, nonce):
            raise HTTPException(401, "nonce invalid, expired, or replayed")
        if not verify_rotate_signature(normalized, nonce, new_key, sig, old_key):
            raise HTTPException(401, "invalid signature for the current device key")
        device["key_hash"] = new_key
        device["key_rotated_at"] = now_iso()
        devices.save(data)
    record("device.key.rotate", "ok", mac=normalized)
    audit.emit("credential", principal_class="device", principal=normalized,
               route="POST /devices/rotate", decision="allow", reason="key_rotated",
               client_ip=client_source(request),
               fingerprint_sha256=sha256_hex(new_key.encode()))
    return {"mac": normalized, "rotated": True}


def _boot_vars_payload(
    mac: str | None,
    hostname: str | None,
    nonce: str | None = None,
    sig: str | None = None,
) -> dict[str, Any]:
    """启动变量投影。识别链：mac→设备台账→绑定 worker；hostname 仅作兜底。
    防冒领（D2）：带 mac 的请求须来自该 worker 绑定的设备（绑定即认证），不符 → 拒绝下发空脚本。
    注入四条件第 4 条（2026-08-21）：设备身份签名验证——强制开关开启后，无 key_hash / 无签名 /
    验签失败一律拒绝（已绑定也不放行）；过渡期无密钥设备照现状放行（防冒领）。
    注册语义：注册只在窗口期（走 /devices/report），boot-vars 无写副作用、无注册通道。"""
    match = _find_worker_for_boot(mac=mac, hostname=hostname)
    if match:
        if _boot_binding_ok(match[0], mac):
            allowed, verified = verify_boot_identity(mac, nonce, sig, match[0])
            if not allowed:
                return {}  # 第 4 条不通过 → 拒绝下发
            payload = _worker_boot_payload(match, include_credentials=verified)
            # NVMe-oF 凭据注入审计（C2，不记密钥本体，只记 injected/verified 布尔）
            injected = bool(payload.get("nbft_secret"))
            record("boot_vars.credential", "ok", mac=mac or "", hostname=hostname or "",
                   worker_id=match[0], injected=injected, verified=verified)
            audit.boot_credential_observed(injected=injected, verified=verified,
                                           worker_id=match[0], mac=mac or "")
            return payload
        return {}  # 冒领/未绑定设备请求 → 拒绝下发
    device = device_for_boot(mac)
    if device:
        # 池中未绑定：reboot 循环等待绑定；吊销/异常态：空脚本（绑定设备必然命中 dnsmasq → worker）
        if device.get("state") == "pooled":
            return _reboot_boot_payload()
        return {}
    return {}  # 未注册：无注册通道（注册只在窗口期，走 report）


def _worker_boot_payload(
    match: tuple[str, dict[str, Any]],
    *,
    include_credentials: bool,
) -> dict[str, Any]:
    """按已识别 worker 投影启动变量。凭据仅在设备身份验签通过后注入。"""
    worker_id, record_ = match
    disk = default_disk_for(record_)
    agent_id = disk.get("agent") if disk else None
    boot = record_.get("boot") or {}
    menu_default = _menu_default_for(record_)
    if menu_default == "reboot":
        # 未配置默认启动：短超时快速重启，等待管理员建盘/设默认系统
        menu_timeout = settings.auto_boot_timeout_ms
    else:
        menu_timeout = boot.get("menu_timeout") or boot.get("menu-timeout") or settings.boot_menu_timeout
    payload: dict[str, Any] = {
        "worker_id": worker_id,
        "menu_default": str(menu_default),
        "menu_timeout": int(menu_timeout),
    }
    # 系统标识投影（2026-08-30 MAIN MENU 动态化）：menu.ipxe 通用 OS 项只消费
    # ${os} / ${os-version} 做展示；盘 NQN 由 disk-nqn 原样下发，不再由固件重拼后缀。
    # 无盘（default_disk_for 返回 None）时不投影，reboot 循环等待建盘。
    if disk and disk.get("os"):
        payload["os"] = str(disk["os"]).lower()
        # os_version 为备注（'' = 无版本）：空值不下发，menu 侧 os-label 拼接自适应
        if disk.get("os_version"):
            payload["os_version"] = str(disk["os_version"]).lower()
    if agent_id:
        # disk-nqn：盘记录里的完整 NQN 权威值，由固件原样拼进 root-path。
        # 不在客户端用 hostname/os/os-tag 重拼——DHCP hostname 滞后或缺省不应影响数据面。
        nqn = disk.get("nqn")
        base_nqn = _base_nqn_from_target(nqn)
        # base-iqn 投影保持 iSCSI 形态（安装器 iSCSI 引导消费）：来源 = 盘 IQN（由盘 NQN 派生）前缀
        base_iqn = _base_iqn_from_target(disk.get("iqn"))
        try:
            storager_ip = agents.storager_ip_for(agent_id)
        except Exception:
            return {}
        backend = _backend_for(agent_id)
        # 只投影 iSCSI root 连接符（差异点），root-path 拼装由 iPXE 侧完成：
        # stgt 需 `:::1:`（lun 占位 1），LIO 需 `::::`（空占位）；
        # nvmet 后端无 iSCSI target，不下发该键，menu 的 iSCSI 安装器项 isset 守卫跳过。
        if nqn:
            payload["disk_nqn"] = str(nqn)
        payload["base_iqn"] = base_iqn
        payload["storager_ip"] = storager_ip
        if backend in {"stgt", "lio"}:
            payload["iscsi_sep"] = ":::1:" if backend == "stgt" else "::::"
        # Host NQN 注入（C2 凭据链路配套）：base 与盘 NQN 同域（节点侧
        # spec.agent.nqnBase 权威，经盘 NQN 前缀派生）；无 base（盘缺 nqn 存量）
        # → 不注入，固件走默认 hostnqn（与 base-nqn 投影同条件）
        if base_nqn:
            payload["hostnqn"] = _host_nqn_for(base_nqn, worker_id)
    # NVMe-oF 认证密钥注入（C2，按 Worker 跟盘裁定）：绑定 worker 在密钥库有条目时注入。
    # 固件侧消费：menu 拼 nvme://...?secret=${nbft-secret}（C3 已启用，secret 条件化拼装）；
    # 无条目 → 不注入，固件走明文连接（兼容未启用认证的 target）。
    secret = _credential_secret_for(worker_id) if include_credentials else None
    if secret:
        payload["nbft_secret"] = secret
    # NVMe/TCP 传输加密（默认全链路，无明文回退）：PSK 是认证密钥的纯函数
    # （workers._derive_tls_psk 域分离派生，轮换认证密钥即同时轮换 PSK，故此处现算，
    # 不新增存储字段）；端口取 Agent 上报的数据面端口（能力即真相，与 _backend_for 同源）。
    # nvmet 后端必须同时具备认证密钥与就绪的数据面端口——任一缺失即拒绝下发引导变量
    # （记审计），因为明文口已撤除，放行只会得到一条连不上的 root-path。
    if agent_id and _backend_for(agent_id) == "nvmet":
        if not include_credentials:
            record("boot_vars.encryption", "denied", worker_id=worker_id, reason="unverified")
            return {}
        tls_port = _nvmet_data_port_for(agent_id)
        if not (secret and tls_port):
            record("boot_vars.encryption", "denied", worker_id=worker_id,
                   reason=("no_credential" if not secret else "port_not_ready"))
            return {}
        from .workers import _derive_tls_psk

        payload["nbft_tls_psk"] = _derive_tls_psk(secret)
        payload["nbft_tls_port"] = tls_port
    return payload


def _reboot_boot_payload() -> dict[str, Any]:
    """池中未绑定/已注册未配置：reboot 循环（短超时），等待绑定或配置完成。"""
    return {"menu_default": "reboot", "menu_timeout": settings.auto_boot_timeout_ms}


def _backend_for(agent_id: str) -> str:
    """返回 Agent 的存储后端类型（stgt | lio | nvmet）。

    一律实时查询 Agent `/capabilities` 的 `backend` 字段（节点自报，能力即真相）：
    后端类型是节点的运行事实，不由控制面登记信息推导——`agents.yml` 的 `tags` 只作人工
    标签，不参与本判定（标签与实况漂移时会拼出错误的 iSCSI 连接符，且标签优先会盖掉实况）。
    未登记 / 查询失败 / 取值非法 → 默认 stgt（保持既有格式兼容）。
    """
    try:
        agent = agents.get(agent_id)
    except KeyError:
        return "stgt"
    try:
        backend = str(agents.client(agent).capabilities().get("backend", "")).lower()
        if backend in {"stgt", "lio", "nvmet"}:
            return backend
    except Exception:
        pass
    return "stgt"


def _nvmet_data_port_for(agent_id: str) -> str:
    """返回 Agent 上报的 NVMe/TCP 数据面端口（`/capabilities.port.trsvcid`）。

    数据面端口即 TLS 1.3 端口（默认全链路加密，无明文口）。与 `_backend_for` 同源
    （能力即真相）：端口是节点运行事实，不从登记信息推导。端口未启用/未就绪
    （`ready` 为假）或查询失败 → 返回空串，调用方据此拒绝引导。
    """
    try:
        agent = agents.get(agent_id)
    except KeyError:
        return ""
    try:
        caps = agents.client(agent).capabilities()
    except Exception:
        return ""
    port = caps.get("port") or {}
    if not isinstance(port, dict) or not port.get("ready"):
        return ""
    return str(port.get("trsvcid") or "")


def _boot_vars_ipxe(payload: dict[str, Any]) -> str:
    lines = ["#!ipxe"]
    if not payload:
        lines.append("# no per-worker boot vars found")
        return "\n".join(lines) + "\n"
    # reboot 循环 payload(池中未绑定)无 worker_id,统一用 unbound 标识
    lines.append(f"# boot vars for {payload.get('worker_id', 'unbound')}")
    if payload.get("disk_nqn"):
        lines.append(f"set disk-nqn {payload['disk_nqn']}")
    if payload.get("base_iqn"):
        lines.append(f"set base-iqn {payload['base_iqn']}")
    if payload.get("storager_ip"):
        lines.append(f"set storager-ip {payload['storager_ip']}")
    if payload.get("iscsi_sep"):
        lines.append(f"set iscsi-sep {payload['iscsi_sep']}")
    if payload.get("nbft_secret"):
        lines.append(f"set nbft-secret {payload['nbft_secret']}")
    if payload.get("nbft_tls_psk"):
        lines.append(f"set nbft-tls-psk {payload['nbft_tls_psk']}")
    if payload.get("nbft_tls_port"):
        lines.append(f"set nbft-tls-port {payload['nbft_tls_port']}")
    if payload.get("hostnqn"):
        lines.append(f"set hostnqn {payload['hostnqn']}")
    if payload.get("os"):
        lines.append(f"set os {payload['os']}")
    if payload.get("os_version"):
        lines.append(f"set os-version {payload['os_version']}")
    lines.extend(
        [
            f"set menu-default {payload['menu_default']}",
            f"set menu-timeout {payload['menu_timeout']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _boot_vars_json(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    result: dict[str, Any] = {
        "menu_default": payload["menu_default"],
        "menu_timeout": payload["menu_timeout"],
    }
    if payload.get("disk_nqn"):
        result["disk_nqn"] = payload["disk_nqn"]
    if payload.get("base_iqn"):
        result["base_iqn"] = payload["base_iqn"]
    if payload.get("storager_ip"):
        result["storager_ip"] = payload["storager_ip"]
    if payload.get("iscsi_sep"):
        result["iscsi_sep"] = payload["iscsi_sep"]
    if payload.get("nbft_secret"):
        result["nbft_secret"] = payload["nbft_secret"]
    if payload.get("nbft_tls_psk"):
        result["nbft_tls_psk"] = payload["nbft_tls_psk"]
    if payload.get("nbft_tls_port"):
        result["nbft_tls_port"] = payload["nbft_tls_port"]
    if payload.get("hostnqn"):
        result["hostnqn"] = payload["hostnqn"]
    if payload.get("os"):
        result["os"] = payload["os"]
    if payload.get("os_version"):
        result["os_version"] = payload["os_version"]
    return result


def _base_iqn_from_target(iqn: str | None) -> str:
    if iqn and ":" in iqn:
        return iqn.rsplit(":", 1)[0]
    return "iqn.2026-07.com.controller"


def _credential_secret_for(worker_id: str) -> str | None:
    """按 Worker 跟盘：返回该 worker 的 DHHC-1 密钥（无条目 → None，不注入）。"""
    with credentials.locked():
        creds = credentials.load()
        entry = creds["credentials"].get(worker_id)
    return entry.get("secret") if entry else None


def _find_worker_for_boot(mac: str | None, hostname: str | None) -> tuple[str, dict[str, Any]] | None:
    """身份识别：设备台账的绑定（mac → bound_worker_id）是唯一权威，优先于 hostname。

    dnsmasq 的 MAC → hostname 只是绑定操作的投影（写文件 + HUP），更新可能滞后：
    若先按 hostname 识别，陈旧投影会把请求导向旧 Worker 或触发「未绑定」拒绝。
    故顺序为：mac → 设备台账 → hostname → dnsmasq 投影（仅存量/无台账兜底）。"""
    data = store.load_workers()
    workers = data["workers"]
    normalized_mac = normalize_boot_mac(mac) if mac else None
    if normalized_mac:
        device = device_for_boot(normalized_mac)
        if device and device.get("state") == "bound":
            worker_id = device.get("bound_worker_id") or ""
            record_ = workers.get(worker_id) if worker_id else None
            if record_:
                return worker_id, record_
    if hostname:
        try:
            found = _find_worker_by_hostname(workers, canonical_hostname(hostname))
            if found:
                return found
        except HTTPException:
            pass
    if normalized_mac:
        for binding in dnsmasq.list_bindings():
            if binding.mac == normalized_mac:
                found = _find_worker_by_hostname(workers, binding.hostname)
                if found:
                    return found
    return None


def _find_worker_by_hostname(workers: dict[str, Any], hostname: str) -> tuple[str, dict[str, Any]] | None:
    if hostname in workers:
        return hostname, workers[hostname]
    for worker_id, record_ in workers.items():
        if record_.get("hostname") == hostname:
            return worker_id, record_
    return None


def _menu_default_for(record_: dict[str, Any]) -> str:
    """默认启动项：default_disk（指向具体盘）> boot.menu_default（显式配置）> reboot（未配置时循环重启等待）。
    2026-08-30 MAIN MENU 动态化：菜单 OS 项已收敛为唯一通用项 boot-os，
    显式 menu_default 中的 OS 语义值（旧系统名）统一归一到 boot-os；
    非 OS 导航值（menu-diag / menu-install / config / shell / reboot / exit）保留原样。"""
    if record_.get("default_disk"):
        return "boot-os"
    boot = record_.get("boot") or {}
    menu_default = boot.get("menu_default") or boot.get("menu-default")
    if menu_default:
        md = str(menu_default).lower()
        if md not in MENU_NAV_ITEMS:
            return "boot-os"  # OS 语义默认值归一（脚本已无对应标签，choose --default 须指向现有项）
        return md
    return "reboot"


def _boot_binding_ok(worker_id: str, mac: str | None) -> bool:
    """防冒领（D2）：带 mac 的启动请求须来自该 worker 绑定的设备（绑定即认证）；
    未带 mac（仅 hostname）无法校验身份，保持兼容放行。"""
    if not mac:
        return True
    device = device_for_boot(mac)
    return bool(device and device.get("state") == "bound" and device.get("bound_worker_id") == worker_id)
