"""NVMe-oF 凭据（DHHC-1，按 Worker 跟盘）签发、推送与对账。

签发时机：nvmet 节点上建系统盘即自动签发（issue_credential），密钥随子系统就位；
手工端点只用于显式轮换或吊销——吊销是明确的「不用认证」，不会被后续建盘复活
（只有无条目才签发，吊销后条目已删）。
"""

import base64
import copy
import datetime as dt
import hashlib
import hmac
import logging
import secrets
import threading
import time
import zlib
from typing import Any


from .client import AgentAPIError
from ..core.config import settings
from ..core.stores import agents, credential_pushes, credentials, record, store
from ..core.utils import now_iso, worker_disks


log = logging.getLogger("control-plane")


def _base_nqn_from_target(nqn: str | None) -> str | None:
    """盘 NQN 的命名空间前缀：`nqn.2026-07.com.kurrent:worker-01.ubuntu.<os_tag>` → 前缀。
    盘 NQN 由控制面按统一模板 `base:worker_id.os.<os_tag>` 生成（无存量盘），前缀重拼 = 盘记录权威值；
    盘记录缺 nqn → 返回 None（不派生，不兼容遗留）。"""
    if nqn and ":" in nqn:
        return nqn.rsplit(":", 1)[0]
    return None


def _host_nqn_for(base_nqn: str, worker_id: str) -> str:
    """Host NQN 派生（发起端身份，host. 前缀区分角色）：与盘 NQN 同域并立。"""
    return f"{base_nqn}:host.{worker_id}"


_UNSET = object()

# 推送结果四值：ok=已送达且 TLS-PSK 已全部应用；degraded=认证已送达但宿主未启用
# TLS（不当作成功，引导仍会拒绝）；failed=未送达/应用数量不足（对账持续重放）；
# n/a=目标 Agent 非 nvmet 后端（凭据不适用，非故障）。
PUSH_OK = "ok"
PUSH_DEGRADED = "degraded"
PUSH_FAILED = "failed"
PUSH_NA = "n/a"


def _credential_targets(record_: dict) -> dict[str, list[str]]:
    """worker 的推送目标：agent_id → 该 Agent 上本 worker 的盘 NQN 列表。

    NVMe-oF 子系统标识 = NQN（盘记录权威字段）；缺 nqn（存量盘）跳过该盘，不派生
    （不兼容遗留）：该盘不支持 NVMe-oF 凭据/引导。"""
    groups: dict[str, list[str]] = {}
    for disk in worker_disks(record_):
        if disk.get("nqn"):
            groups.setdefault(disk["agent"], []).append(disk["nqn"])
    return groups


def _registered_agent_ids() -> set[str]:
    return {agent.id for agent in agents.load()}


def _sync_targets(record_: dict, registered: set[str] | None = None) -> dict[str, list[str]]:
    """有同步语义的推送目标：`_credential_targets` ∩ 已登记 Agent。

    未登记的 Agent（登记被删 / 尚未 enroll）无从投递——对账据此剪除记录，投影也须据此
    排除，否则会显示永不收敛的「待推送」（对账只服务已登记 Agent，永远不会消费它）。
    批量调用（对账）可传入预取的 registered 以避免逐 worker 重读登记表。"""
    if registered is None:
        registered = _registered_agent_ids()
    return {agent_id: nqns for agent_id, nqns in _credential_targets(record_).items()
            if agent_id in registered}


def _forget_worker_credentials(worker_id: str) -> None:
    """删除 Worker 时清空它在凭据域的全部痕迹：密钥条目 + 该 worker 的推送台账。

    不清理会留孤儿——条目对已不存在的 worker 既不可见（`GET /credential` 404）也删不掉；
    同 id 的 worker 若日后被建出来，还会静默继承旧密钥并被推给新机器的盘。"""
    with credentials.locked():
        creds = credentials.load()
        if creds["credentials"].pop(worker_id, None) is not None:
            credentials.save(creds)
            record("credential.forget", "ok", worker_id=worker_id, reason="worker.delete")
    _next_generation(worker_id)  # 保留单调墓碑，防止迟到 push 复活已删 worker 的 key
    prefix = f"{worker_id}/"  # 推送台账键 = "<worker_id>/<agent_id>"（worker_id 不含 "/"）
    with credential_pushes.locked():
        data = credential_pushes.load()
        stale = [key for key in data["pushes"] if key.startswith(prefix)]
        if stale:
            for key in stale:
                data["pushes"].pop(key)
            credential_pushes.save(data)


def _push_state_key(worker_id: str, agent_id: str) -> str:
    return f"{worker_id}/{agent_id}"


def _set_push_state(worker_id: str, agent_id: str, result: str,
                    secret: str | None, generation: int, error: str = "") -> bool:
    """回写推送状态记录（secret_hash 记期望密钥审计哈希，"" = 期望吊销）。

    台账按 generation 单调：迟到的旧推送结果不能覆盖较新的记录（乱序/并发重放）。"""
    entry: dict[str, Any] = {
        "worker_id": worker_id,
        "agent_id": agent_id,
        "secret_hash": _secret_hash(secret) if secret else "",
        "generation": generation,
        "result": result,
        "updated_at": now_iso(),
    }
    if error:
        entry["error"] = error
    with credential_pushes.locked():
        data = credential_pushes.load()
        prev = data["pushes"].get(_push_state_key(worker_id, agent_id), {})
        if generation < int(prev.get("generation", 0)):
            return False
        data["pushes"][_push_state_key(worker_id, agent_id)] = entry
        credential_pushes.save(data)
    return True


def _push_result(resp: object, *, secret: str | None, tls_psk: str | None,
                 expected: int) -> tuple[str, str]:
    """按 Agent 回报判定推送结果：TLS-PSK 必须逐对应用到目标端，不能只看 HTTP 200。

    Agent 不可达时 set_credential 直接抛错；这里处理 200 但未真正应用的情况：
    - applied == expected → ok；
    - 全部缺失且原因是宿主未启用 TLS（409）→ degraded（认证已写，引导仍会拒绝）；
    - 其他（含旧 Agent 不回报、部分应用、子系统 404）→ failed，由对账重试。"""
    if not isinstance(resp, dict):
        return PUSH_FAILED, "agent response missing tls application status"
    if secret is None:
        if expected == 0:
            return PUSH_OK, ""
        if resp.get("tls_revoked") == expected:
            return PUSH_OK, ""
        return PUSH_FAILED, (
            f"tls psk revoke not applied: {resp.get('tls_revoked', 'missing')}/{expected}")
    if not tls_psk or expected == 0:
        return PUSH_OK, ""
    applied = resp.get("tls_applied")
    if not isinstance(applied, int):
        return PUSH_FAILED, "agent response missing tls_applied"
    app_expected = resp.get("tls_expected")
    if app_expected != expected:
        return PUSH_FAILED, (
            f"agent tls target mismatch: expected {expected}, got {app_expected}")
    if applied == expected:
        return PUSH_OK, ""
    unavailable = resp.get("tls_unavailable") or 0
    missing = resp.get("tls_missing") or 0
    detail = f"tls psk not applied: {applied}/{expected}"
    if resp.get("tls_error"):
        detail += f" ({resp['tls_error']})"
    if unavailable and not missing and applied + unavailable == expected:
        return PUSH_DEGRADED, detail
    return PUSH_FAILED, detail


def _push_one(worker_id: str, agent_id: str, sub_nqns: list[str], secret: str | None,
              *, audit_always: bool, generation: int) -> str:
    """推送单个 (worker, agent) 对并回写状态记录；返回 ok|degraded|failed|n/a|skipped。

    审计：用户动作（audit_always=True）每次记录；对账重放仅在结果或期望密钥变化时
    记录（周期重放不刷屏）。Agent 登记缺失（登记删除语义 = 仅清控制面记录）跳过且
    不留状态——登记恢复后由对账补推；非 nvmet 后端记 n/a（凭据不适用，不计故障）。"""
    try:
        agent = agents.get(agent_id)
    except KeyError:
        record("credential.push", "skipped", worker_id=worker_id, agent=agent_id,
               reason="agent not registered")
        return "skipped"
    with credential_pushes.locked():
        prev = credential_pushes.load()["pushes"].get(_push_state_key(worker_id, agent_id), {})
    # Host NQN base：盘 NQN 前缀（与 base_nqn 投影同机制）——同一 worker 持多盘
    # 时各盘同 agent 同域，base 一致；缺 nqn 的盘已跳过，此处恒有值
    host_nqns = [_host_nqn_for(_base_nqn_from_target(sub_nqns[0]), worker_id)]
    # TLS-PSK 随认证密钥派生（_derive_tls_psk）；secret=None（吊销）→ 同步吊销目标端 PSK
    tls_psk = _derive_tls_psk(secret) if secret else None
    try:
        resp = agents.client(agent).set_credential(worker_id, secret, sub_nqns, host_nqns,
                                                   tls_psk=tls_psk, generation=generation)
        result, error = _push_result(resp, secret=secret, tls_psk=tls_psk,
                                     expected=len(sub_nqns) * len(host_nqns))
    except AgentAPIError as exc:
        if exc.status_code == 400 and "backend=nvmet" in exc.detail:
            result, error = PUSH_NA, ""
        else:
            result, error = PUSH_FAILED, exc.detail
    except Exception as exc:
        result, error = PUSH_FAILED, str(exc)
    if not _set_push_state(worker_id, agent_id, result, secret, generation, error):
        # 旧 generation 的迟到结果：不覆盖台账，也不重复审计
        return result
    if result == PUSH_FAILED:
        log.warning(f"credential push failed: worker={worker_id} agent={agent_id}: {error}")
    desired_hash = _secret_hash(secret) if secret else ""
    if audit_always or prev.get("result") != result or prev.get("secret_hash", "") != desired_hash:
        extra: dict[str, Any] = {"worker_id": worker_id, "agent_id": agent_id,
                                 "injected": bool(secret), "sub_count": len(sub_nqns),
                                 "host_count": len(host_nqns)}
        status = result
        if result == PUSH_NA:
            status, extra["reason"] = "skipped", "backend not nvmet"
        if error:
            extra["error"] = error
        record("credential.push", status, **extra)
    return result


def _push_state(worker_id: str, agent_id: str) -> dict:
    with credential_pushes.locked():
        return credential_pushes.load()["pushes"].get(_push_state_key(worker_id, agent_id), {})


def _no_sync_needed(prev: dict, secret: str | None) -> bool:
    """无密钥且该对从无推送历史 → 无需同步。

    nvmet 建盘已自动签发密钥，此处无密钥只剩两类：盘在非 nvmet 后端（DHHC-1 不适用，
    推送只会得 n/a），或密钥已被显式吊销（有历史记录，不走本判定）。这两类都不该产生
    新推送记录，否则 agent 离线时会显示「同步失败 / 待同步」的虚假故障。"""
    return not prev and not secret


def _issue_credential(worker_id: str, backend: str) -> bool:
    """为 nvmet 节点上的新子系统签发认证密钥：无条目则生成，已有条目原样保留。

    建盘即签发——盘一旦在 nvmet 上有了子系统，认证密钥就是它的配套凭据，由系统签发
    而非逐个手工设置（批量建盘 N 台即 N 份密钥）。已有条目视为已签发：按 Worker 跟盘，
    同一 worker 的多盘共用一把密钥，建盘不触发轮换。非 nvmet 后端不签发——DHHC-1 是
    NVMe-oF 专属（iSCSI 走 CHAP，不在本链路）。返回是否新签发。"""
    if backend != "nvmet":
        return False
    now = now_iso()
    with credentials.locked():
        creds = credentials.load()
        if worker_id in creds["credentials"]:
            return False
        secret = _generate_dhhc1()
        creds["credentials"][worker_id] = {
            "worker_id": worker_id,
            "secret": secret,
            "secret_hash": _secret_hash(secret),
            "created_at": now,
            "updated_at": now,
        }
        credentials.save(creds)
    record("credential.issue", "ok", worker_id=worker_id, backend=backend)
    return True


def _push_credentials(worker_id: str, secret: object = _UNSET) -> None:
    """把 worker 的 NVMe-oF 凭据期望状态推送给持有其盘的 Agent（失败仅审计与记录，不阻断主流程）。
    secret 缺省取密钥库当前条目（无条目 → None 吊销推送）；审计不记密钥本体。"""
    if secret is _UNSET:
        with credentials.locked():
            entry = credentials.load()["credentials"].get(worker_id)
        secret = entry.get("secret") if entry else None
    with store.locked():
        record_ = store.load_workers()["workers"].get(worker_id)
    if not record_:
        return
    pending = [(agent_id, sub_nqns) for agent_id, sub_nqns in _sync_targets(record_).items()
               if not _no_sync_needed(_push_state(worker_id, agent_id), secret)]
    if not pending:
        return
    generation = _next_generation(worker_id)
    for agent_id, sub_nqns in pending:
        _push_one(worker_id, agent_id, sub_nqns, secret, audit_always=True,
                  generation=generation)


_reconcile_thread_started = False


def _rotation_due(entry: dict, now: dt.datetime) -> bool:
    """密钥条目是否到达轮换周期（rotated_at > updated_at > created_at 取最新可用基准）。"""
    hours = settings.credential_rotation_hours
    if hours <= 0:
        return False
    for key in ("rotated_at", "updated_at", "created_at"):
        raw = str(entry.get(key) or "")
        if raw:
            try:
                return (now - dt.datetime.fromisoformat(raw)).total_seconds() >= hours * 3600
            except ValueError:
                continue
    return False


def rotate_due_credentials(now: dt.datetime | None = None) -> dict[str, int]:
    """到期 DHHC-1 轮换：先推送到全部目标 Agent，全部送达才在台账切换密钥。

    推送失败（或部分送达）时把已送达目标回推旧密钥、台账保持不变，下个周期重试——
    切换前引导投影仍是旧密钥，宿主改新密钥的窗口只覆盖推送本身。没有 nvmet 目标的
    worker 跳过（无法证明新密钥已送达）。用户手工设置过的密钥同样按 updated_at 参与
    轮换；轮换周期 <=0 时整体关闭。
    """
    now = now or dt.datetime.now().astimezone()
    with credentials.locked():
        entries = copy.deepcopy(credentials.load()["credentials"])
    with store.locked():
        workers = store.load_workers()["workers"]
    registered = _registered_agent_ids()
    rotated = failed = skipped = 0
    for worker_id, entry in entries.items():
        if not isinstance(entry, dict) or not _rotation_due(entry, now):
            continue
        old_secret = str(entry.get("secret") or "")
        record_ = workers.get(worker_id)
        targets = _sync_targets(record_, registered) if record_ else {}
        if not old_secret or not targets:
            skipped += 1
            continue
        new_secret = _generate_dhhc1()
        new_generation = _next_generation(worker_id)
        delivered: list[str] = []
        for agent_id, sub_nqns in targets.items():
            if _push_one(worker_id, agent_id, sub_nqns, new_secret,
                         audit_always=False, generation=new_generation) in (PUSH_FAILED, PUSH_DEGRADED):
                break
            delivered.append(agent_id)
        if len(delivered) != len(targets):
            rollback_generation = _next_generation(worker_id) if delivered else new_generation
            for agent_id in delivered:
                _push_one(worker_id, agent_id, targets[agent_id], old_secret,
                          audit_always=False, generation=rollback_generation)
            record("credential.rotate", "failed", worker_id=worker_id,
                   delivered=len(delivered), targets=len(targets))
            failed += 1
            continue
        with credentials.locked():
            creds = credentials.load()
            current = creds["credentials"].get(worker_id) or {}
            if current.get("secret") != old_secret:
                skipped += 1  # 推送期间被手工改写：以台账为准，下个周期再判
                continue
            current["secret"] = new_secret
            current["secret_hash"] = _secret_hash(new_secret)
            current["updated_at"] = now.isoformat()
            current["rotated_at"] = now.isoformat()
            creds["credentials"][worker_id] = current
            credentials.save(creds)
        record("credential.rotate", "ok", worker_id=worker_id, targets=len(targets))
        rotated += 1
    return {"rotated": rotated, "failed": failed, "skipped": skipped}


def reconcile_credentials_once() -> dict[str, int]:
    """对账全量重放（周期线程调用；可测核心）。

    期望（credentials.yml）× 目标（workers.yml 的盘 NQN 分组）逐对比较推送记录，
    密钥哈希一致且已送达（ok/n-a）才跳过——覆盖三类静默缺口：推送失败未重试、
    崩溃缝隙（台账已改、进程未推）、吊销未送达（期望吊销 = 无条目）。未被任何当前
    目标引用的记录剪除：Agent 注销后重新登记 / 盘迁移后无记录，会自然触发重推。"""
    with credentials.locked():
        creds = credentials.load()["credentials"]
    with store.locked():
        workers = store.load_workers()["workers"]
    targets: dict[str, tuple[str, str, list[str], str | None]] = {}
    registered = _registered_agent_ids()
    for worker_id, record_ in workers.items():
        secret = (creds.get(worker_id) or {}).get("secret")
        for agent_id, sub_nqns in _sync_targets(record_, registered).items():
            targets[_push_state_key(worker_id, agent_id)] = (worker_id, agent_id, sub_nqns, secret)
    with credential_pushes.locked():
        data = credential_pushes.load()
        stale = [key for key in data["pushes"] if key not in targets]
        if stale:
            for key in stale:
                data["pushes"].pop(key)
            credential_pushes.save(data)
    pushed = skipped = failed = 0
    for key, (worker_id, agent_id, sub_nqns, secret) in targets.items():
        with credential_pushes.locked():
            prev = credential_pushes.load()["pushes"].get(key, {})
        desired_hash = _secret_hash(secret) if secret else ""
        if _no_sync_needed(prev, secret):
            skipped += 1
            continue
        if prev.get("result") in (PUSH_OK, PUSH_NA) and prev.get("secret_hash", "") == desired_hash:
            # 台账 ok 只代表上次推送成功；宿主重启会清空 .nvme，必须回查 key 是否仍在。
            if prev.get("result") == PUSH_NA:
                skipped += 1
                continue
            try:
                host_nqns = [_host_nqn_for(_base_nqn_from_target(sub_nqns[0]), worker_id)]
                status = agents.client(agents.get(agent_id)).credential_status(
                    worker_id, sub_nqns, host_nqns)
                keys = status.get("keys") or []
                in_place = (
                    bool(keys) and all(k.get("present") for k in keys)
                    if secret else
                    bool(keys) and not any(k.get("present") for k in keys)
                )
            except Exception:
                in_place = False
            if in_place:
                skipped += 1
                continue
        generation = _next_generation(worker_id)
        if _push_one(worker_id, agent_id, sub_nqns, secret, audit_always=False,
                     generation=generation) == PUSH_FAILED:
            failed += 1
        else:
            pushed += 1
    return {"targets": len(targets), "pushed": pushed, "skipped": skipped, "failed": failed}


def start_credential_reconciler() -> None:
    """启动控制面→Agent 凭据对账线程（FastAPI lifespan 调用；间隔 ≤0 禁用——测试环境）。

    三层互补：本线程覆盖「控制面→Agent」段；Agent reconcile 覆盖「Agent→宿主服务」段；
    Agent 启动重放覆盖其自身重启窗口（见 storager/agent NvmetCredentialCache）。"""
    global _reconcile_thread_started
    interval = settings.credential_reconcile_interval
    if interval <= 0 or _reconcile_thread_started:
        return
    _reconcile_thread_started = True

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                rotation = rotate_due_credentials()
                if rotation["rotated"] or rotation["failed"]:
                    log.info(f"credential rotation: {rotation}")
                summary = reconcile_credentials_once()
                if summary["failed"] or summary["pushed"]:
                    log.info(f"credential reconcile: {summary}")
            except Exception:
                log.exception("credential reconcile failed")

    threading.Thread(target=_loop, name="credential-reconcile", daemon=True).start()
    log.info(f"credential reconcile loop started: interval={interval}s")


def _validate_dhhc1(secret: str) -> None:
    """DHHC-1 密钥自检（nvmeof-credential-design.md 2.1 契约）：
    前缀、类型两位数字、base64 解码长度 36/68（32/64 字节密钥 + CRC32 小端 4 字节）、CRC 终值。
    非法 → ValueError（配置阶段即失败而非运行时暴露，对齐 nvmet-setup.sh 自检模式）。"""
    if not secret.startswith("DHHC-1:"):
        raise ValueError("secret must start with 'DHHC-1:'")
    rest = secret[len("DHHC-1:"):]
    kind, _, b64 = rest.partition(":")
    if not b64 or len(kind) != 2 or not kind.isdigit():
        raise ValueError("secret type must be two digits (e.g. 01 for SHA-256)")
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        raise ValueError("secret base64 payload is invalid") from None
    if len(raw) not in (36, 68):
        raise ValueError("secret key must be 32 or 64 bytes (36/68 payload incl. CRC32)")
    key, crc = raw[:-4], raw[-4:]
    if zlib.crc32(key) != int.from_bytes(crc, "little"):
        raise ValueError("secret CRC32 checksum mismatch")


def _generate_dhhc1() -> str:
    """生成 DHHC-1:01:<base64(32B 随机 ‖ CRC32 小端终值)>（固件 2.1 契约）。

    与 CLI `workers credential set --generate` 同一格式；产出必过 `_validate_dhhc1`
    （生成与自检互为闭环）。明文不落审计、不经 API 返回，仅存密钥库供 /boot-vars 注入。"""
    key = secrets.token_bytes(32)
    crc = zlib.crc32(key)
    return "DHHC-1:01:" + base64.b64encode(key + crc.to_bytes(4, "little")).decode("ascii")


def _secret_hash(secret: str) -> str:
    """审计用哈希（不用于注入，注入用明文）：sha256 全量，审计输出仅前缀。"""
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _next_generation(worker_id: str) -> int:
    """分配并持久化下一次推送的 generation（每次推送单调递增）。

    generation 存在 credentials.yml 顶层 generations（吊销后条目删除仍保留墓碑），
    Agent 据此拒绝迟到的旧 push；台账按 (worker, agent) 记录并只接受更新的值。"""
    with credentials.locked():
        data = credentials.load()
        gens = data.get("generations")
        if not isinstance(gens, dict):
            gens = {}
            data["generations"] = gens
        entry = data["credentials"].get(worker_id)
        try:
            current = max(int(gens.get(worker_id, 0)),
                          int((entry or {}).get("generation", 0)))
        except (TypeError, ValueError):
            current = 0
        generation = current + 1
        gens[worker_id] = generation
        if entry is not None:
            entry["generation"] = generation
        credentials.save(data)
        return generation


def _derive_tls_psk(secret: str) -> str:
    """从 worker 的 DHHC-1 认证密钥派生 NVMe/TCP TLS-PSK（32 字节，64 位十六进制）。

    HMAC-SHA256 域分离派生（RFC 2104 PRF）——与 DHHC 认证共用同一把源密钥，**轮换认证
    密钥即同时轮换 TLS-PSK**，因此推送台账的期望指纹不必扩展（PSK 是密钥的纯函数，
    `_push_one` 每次现算）。目标端（nvmet-host keyring）与发起端（initramfs /boot-vars）
    用同一串，identity 见 .local/blueprint/nvme-tcp-tls-in-kernel.md §5。"""
    raw = base64.b64decode(secret.split(":", 2)[2], validate=True)[:32]
    return hmac.new(raw, b"kurrent-nvme-tls-psk-v1", hashlib.sha256).hexdigest()


def _credential_meta(entry: dict[str, Any]) -> dict[str, Any]:
    """凭据元数据视图：不返回明文；secret_hash 只给前缀便于轮换比对。"""
    return {
        "worker_id": entry["worker_id"],
        "exists": True,
        "secret_hash": str(entry.get("secret_hash", ""))[:15],
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
    }


def _credential_push_view(worker_id: str, record_: dict, *, desired: str | None) -> dict[str, Any]:
    """推送状态投影（GET/PUT/DELETE 响应）：逐 Agent 明细 + 汇总，供 CLI/WebUI 呈现。

    汇总 result：ok=全部适用目标已送达 / degraded=认证已写但目标端 TLS 未启用、PSK 未生效 /
    failed=任一未送达 / pending=等待对账重放（含密钥已轮换、记录仍是旧值）/
    n-a=目标均非 nvmet 后端（凭据不适用）/ none=无同步目标（未设置凭据且无推送历史，
    或盘所在 Agent 未登记）。"""
    targets = _sync_targets(record_)
    if not targets:
        return {"result": "none", "updated_at": None, "agents": []}
    desired_hash = _secret_hash(desired) if desired else ""
    with credential_pushes.locked():
        pushes = credential_pushes.load()["pushes"]
    items: list[dict[str, Any]] = []
    for agent_id in targets:
        prev = pushes.get(_push_state_key(worker_id, agent_id)) or {}
        if _no_sync_needed(prev, desired):
            # 未设置凭据且无历史：无同步语义，不显示为「待同步」
            continue
        result = prev.get("result", "")
        item: dict[str, Any] = {"agent_id": agent_id, "updated_at": prev.get("updated_at") or None}
        if not prev:
            item["result"] = "pending"
        elif result == PUSH_FAILED:
            item["result"] = PUSH_FAILED
            item["error"] = prev.get("error", "")
        elif result == PUSH_NA:
            item["result"] = PUSH_NA
        elif result == PUSH_DEGRADED and prev.get("secret_hash", "") == desired_hash:
            item["result"] = PUSH_DEGRADED
            item["error"] = prev.get("error", "")
        elif result == PUSH_OK and prev.get("secret_hash", "") == desired_hash:
            item["result"] = PUSH_OK
        else:
            # 记录为旧值（轮换后未重推）：待对账重放
            item["result"] = "pending"
        items.append(item)
    if not items:
        return {"result": "none", "updated_at": None, "agents": []}
    results = [item["result"] for item in items]
    if PUSH_FAILED in results:
        overall = PUSH_FAILED
    elif "pending" in results:
        overall = "pending"
    elif PUSH_DEGRADED in results:
        overall = PUSH_DEGRADED
    elif all(r == PUSH_NA for r in results):
        overall = PUSH_NA
    else:
        overall = PUSH_OK
    stamps = [item["updated_at"] for item in items if item["updated_at"]]
    return {"result": overall, "updated_at": max(stamps) if stamps else None, "agents": items}
