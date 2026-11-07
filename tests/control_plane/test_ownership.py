"""状态根文件属主对齐：容器以 root 写入宿主挂载目录时，文件应归目录属主。"""

from pathlib import Path
from types import SimpleNamespace

from control_plane.app import utils


def test_align_owner_chowns_to_parent_owner(tmp_path, monkeypatch):
    target = tmp_path / "ledger.yml"
    target.write_text("x", encoding="utf-8")
    parent = tmp_path.stat()
    calls = []
    monkeypatch.setattr(utils.os, "chown", lambda path, uid, gid: calls.append((path, uid, gid)))
    real_stat = Path.stat

    def fake_stat(self):
        if self == target:
            return SimpleNamespace(st_uid=0, st_gid=0)
        return real_stat(self)

    monkeypatch.setattr(Path, "stat", fake_stat)

    utils.align_owner(target)

    assert calls == [(target, parent.st_uid, parent.st_gid)]


def test_align_owner_is_a_noop_when_owner_matches(tmp_path, monkeypatch):
    target = tmp_path / "ledger.yml"
    target.write_text("x", encoding="utf-8")
    calls = []
    monkeypatch.setattr(utils.os, "chown", lambda *args: calls.append(args))

    utils.align_owner(target)

    assert calls == []


def test_align_owner_ignores_permission_errors(tmp_path, monkeypatch):
    target = tmp_path / "ledger.yml"
    target.write_text("x", encoding="utf-8")
    real_stat = Path.stat

    def fake_stat(self):
        if self == target:
            return SimpleNamespace(st_uid=0, st_gid=0)
        return real_stat(self)

    def deny(*args):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(utils.os, "chown", deny)

    utils.align_owner(target)  # 不得抛出：无权限时由调用方的可读性检查兜底
