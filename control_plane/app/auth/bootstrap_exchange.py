"""一次性管理凭据交换：控制面 → 部署操作用户。

首次部署时 CLI 在交换目录写入 `request`；控制面启动完成（admin.conf 已签发）后
把 admin.conf 与它引用的证书复制到交换目录，重写为包内相对路径，最后写 `ready`
（逐文件 SHA-256）并删除 `request`。CLI 校验哈希与证书配对、安装到 ~/.kurrent、
完成 mTLS 自检后才删除交换包并写 `consumed`。

交换包是服务端凭据的**副本**：`<state>/control-plane/state/admin.conf` 仍是唯一
权威，轮换/重签以它为准。目录 0700、文件 0600；复制只允许 admin.conf 所在目录内的
普通文件，拒绝路径逃逸与符号链接。
"""

import datetime
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path

import yaml

log = logging.getLogger("control-plane")

REQUEST = "request"
READY = "ready"
CONSUMED = "consumed"
ADMIN_CONF = "admin.conf"
PKI_DIR = "pki"
_CERT_KEYS = ("certificate-authority", "client-certificate", "client-key")


class BootstrapExchangeError(RuntimeError):
    """交换失败：控制面启动记录错误（/healthz 503），init 按日志定位。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chown_like(path: Path, ref: Path) -> None:
    """把文件属主对齐交换目录（宿主操作员所有）；非 root/同属主时跳过。"""
    try:
        st = ref.stat()
        if path.stat().st_uid == st.st_uid and path.stat().st_gid == st.st_gid:
            return
        os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass


def _write_private(path: Path, body: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _inside(base: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(base)
        return True
    except ValueError:
        return False


def publish_requested_bundle(exchange_dir: Path, admin_conf: Path) -> bool:
    """若交换目录存在 request，则发布 admin.conf 副本与 ready 清单。

    返回 True 表示本次发布了交换包；没有 request 时返回 False（幂等重跑/无首次部署）。
    """
    exchange_dir = Path(exchange_dir)
    request = exchange_dir / REQUEST
    if not request.is_file():
        return False
    admin_conf = Path(admin_conf)
    if not admin_conf.is_file():
        raise BootstrapExchangeError(f"admin.conf is missing: {admin_conf}")

    try:
        doc = yaml.safe_load(admin_conf.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BootstrapExchangeError(f"cannot read admin.conf: {exc}") from exc
    if not isinstance(doc, dict):
        raise BootstrapExchangeError("admin.conf is not a mapping")
    server = str(doc.get("server") or "")
    missing = [key for key in _CERT_KEYS if not str(doc.get(key) or "").strip()]
    if not server or missing:
        raise BootstrapExchangeError(f"admin.conf is incomplete (server or {', '.join(missing)})")

    source_dir = admin_conf.parent.resolve()
    pki_dir = exchange_dir / PKI_DIR
    try:
        # 清理上一轮残留；request 本身保留到 ready 写完。
        for stale in (pki_dir, exchange_dir / ADMIN_CONF, exchange_dir / READY, exchange_dir / CONSUMED):
            if stale.is_dir():
                shutil.rmtree(stale)
            elif stale.exists():
                stale.unlink()
        pki_dir.mkdir(mode=0o700, parents=True)
        os.chmod(pki_dir, 0o700)
        # 目录本身也要对齐：控制面以 root 运行时 pki/ 默认 root:root 0700，
        # 部署用户无法进入，CLI 读 ready 列出的证书会 permission denied。
        _chown_like(pki_dir, exchange_dir)

        out_doc = dict(doc)
        copied: dict[str, Path] = {}
        for key in _CERT_KEYS:
            raw = str(doc[key])
            candidate = source_dir / raw
            if candidate.is_symlink():
                raise BootstrapExchangeError(f"admin.conf {key} is a symlink (refusing): {raw}")
            source = candidate.resolve()
            if not _inside(source_dir, source):
                raise BootstrapExchangeError(f"admin.conf {key} escapes the credential directory: {raw}")
            if not source.is_file():
                raise BootstrapExchangeError(f"admin.conf {key} is not a regular file: {raw}")
            name = source.name
            if name in copied and copied[name] != source:
                raise BootstrapExchangeError(f"certificate filename conflict in admin.conf: {name}")
            copied[name] = source
            destination = pki_dir / name
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600)
            _chown_like(destination, exchange_dir)
            out_doc[key] = f"{PKI_DIR}/{name}"

        body = (
            "# kurrent management credential (one-shot bootstrap exchange copy; "
            "the authoritative file stays in the control-plane state directory)\n"
            + yaml.safe_dump(out_doc, allow_unicode=True, sort_keys=False)
        )
        exchange_admin = exchange_dir / ADMIN_CONF
        _write_private(exchange_admin, body)
        _chown_like(exchange_admin, exchange_dir)

        files = sorted([ADMIN_CONF] + [f"{PKI_DIR}/{name}" for name in copied])
        ready = {
            "apiVersion": "kurrent.io/v1",
            "kind": "BootstrapBundle",
            "createdAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "files": files,
            "sha256": {name: _sha256(exchange_dir / name) for name in files},
        }
        _write_private(exchange_dir / READY, json.dumps(ready, indent=2) + "\n")
        _chown_like(exchange_dir / READY, exchange_dir)
    except BootstrapExchangeError:
        raise
    except Exception as exc:  # 文件系统错误统一转成可诊断的交换失败
        raise BootstrapExchangeError(f"publishing the bootstrap bundle failed: {exc}") from exc

    request.unlink()
    log.info("bootstrap exchange: published management credentials to %s", exchange_dir)
    return True
