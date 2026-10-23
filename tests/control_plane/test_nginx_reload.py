"""nginx reload 哨兵：控制面写计数器，nginx watcher 回写 ack（不再依赖 docker.sock）。"""

import threading
import time

import pytest

from control_plane.app import nginx_reload


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    trigger = tmp_path / "nginx.reload"
    ack = tmp_path / "nginx.ack"
    monkeypatch.setattr(nginx_reload, "RELOAD_TRIGGER", trigger)
    monkeypatch.setattr(nginx_reload, "RELOAD_ACK", ack)
    return trigger, ack


def _fake_watcher(trigger, ack, want: int):
    """模拟 nginx watcher：看到目标计数器就回写 ack。"""
    def run():
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                value = int(trigger.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                value = 0
            if value >= want:
                ack.write_text(f"{value}\n", encoding="utf-8")
                return
            time.sleep(0.01)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_reload_waits_for_ack(paths):
    trigger, ack = paths
    thread = _fake_watcher(trigger, ack, want=1)
    out = nginx_reload.reload_nginx(timeout=1)
    thread.join(timeout=1)
    assert out == {"status": "ok", "counter": "1"}
    assert trigger.read_text(encoding="utf-8").strip() == "1"


def test_reload_counter_increments(paths):
    trigger, ack = paths
    trigger.write_text("4\n", encoding="utf-8")
    ack.write_text("4\n", encoding="utf-8")
    thread = _fake_watcher(trigger, ack, want=5)
    out = nginx_reload.reload_nginx(timeout=1)
    thread.join(timeout=1)
    assert out["counter"] == "5"


def test_reload_times_out_without_ack(paths):
    with pytest.raises(RuntimeError, match="acknowledge"):
        nginx_reload.reload_nginx(timeout=0.1)
