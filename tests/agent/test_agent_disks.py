"""Agent 建盘/母盘扫描单测：GPT 扇区布局探测、母盘 4K 校验（POST /lun/disk 拒绝/放行）、
MasterScanner gpt 字段。

背景：nvmet file backend 的逻辑块大小跟随底层 FS 块（典型 4096 B），512 字节扇区布局的
GPT 母盘在 4K 导出下被发起端按 4K 解释、EDK2 找不到 ESP（生产实测 sanboot EFI_NOT_FOUND）
——建盘阶段校验阻断（NvmetBackend.validate_disk_file）。
"""

import os

import pytest

NQN_UBUNTU = "nqn.2026-07.com.test:worker-01.ubuntu"
IQN_UBUNTU = "iqn." + NQN_UBUNTU[4:]


def _write_gpt(path: str, offset: int) -> None:
    """写最小扇区布局文件：GPT 头签名 'EFI PART' 落在指定字节偏移（LBA1）。"""
    with open(path, "wb") as f:
        f.seek(offset)
        f.write(b"EFI PART")


# ============================ disk_gpt_layout ============================


def test_disk_gpt_layout(tmp_path):
    """三态探测：512 盘命中 offset 512；4K 盘命中 offset 4096；无签名/文件缺失 → none。"""
    from app.nvmet import disk_gpt_layout

    p512 = tmp_path / "m512.img"
    _write_gpt(str(p512), 512)
    assert disk_gpt_layout(str(p512)) == "512"
    p4k = tmp_path / "m4k.img"
    _write_gpt(str(p4k), 4096)
    assert disk_gpt_layout(str(p4k)) == "4k"
    praw = tmp_path / "raw.img"
    praw.write_bytes(b"x" * 8192)
    assert disk_gpt_layout(str(praw)) == "none"
    assert disk_gpt_layout(str(tmp_path / "missing.img")) == "none"


def test_backend_validate_disk_file(backend, tmp_path):
    """validate_disk_file：512 布局 400 拒绝（含重建指引）；4K 原生与无签名放行。"""
    from fastapi import HTTPException

    b, _, _ = backend
    p512 = tmp_path / "m512.img"
    _write_gpt(str(p512), 512)
    with pytest.raises(HTTPException) as exc_info:
        b.validate_disk_file(str(p512))
    assert exc_info.value.status_code == 400
    assert "4096-byte sectors" in exc_info.value.detail
    p4k = tmp_path / "m4k.img"
    _write_gpt(str(p4k), 4096)
    b.validate_disk_file(str(p4k))  # 不抛
    raw = tmp_path / "raw.img"
    raw.write_bytes(b"x" * 8192)
    b.validate_disk_file(str(raw))  # 无 GPT 签名：告警放行


# ============================ POST /lun/disk（母盘克隆校验） ============================


def test_create_disk_rejects_512_gpt(agent_client, monkeypatch):
    """512 布局母盘克隆：400 + 可操作指引；不落 backing、不建子系统。"""
    import app.main as agent_main

    client, backend, calls = agent_client
    monkeypatch.setattr(agent_main, "DISK_DIR", backend.disk_dir)
    _write_gpt(os.path.join(backend.disk_dir, "_tpl_win10.img"), 512)
    res = client.post("/lun/disk", json={"iqn": IQN_UBUNTU, "master": "_tpl_win10.img"})
    assert res.status_code == 400
    assert "512-byte-sector" in res.json()["detail"]
    assert not os.path.exists(os.path.join(backend.disk_dir, "worker-01.ubuntu.img"))
    assert not any(c[0] == "create_subsystem" for c in calls)


def test_create_disk_accepts_4k_gpt(agent_client, monkeypatch):
    """4K 原生 GPT 母盘：正常克隆落盘 + 建子系统。"""
    import app.main as agent_main

    client, backend, calls = agent_client
    monkeypatch.setattr(agent_main, "DISK_DIR", backend.disk_dir)
    _write_gpt(os.path.join(backend.disk_dir, "_tpl_ubuntu.img"), 4096)
    res = client.post("/lun/disk", json={"iqn": IQN_UBUNTU, "master": "_tpl_ubuntu.img"})
    assert res.status_code == 200
    backing = os.path.join(backend.disk_dir, "worker-01.ubuntu.img")
    assert os.path.exists(backing)
    assert ("create_subsystem", NQN_UBUNTU, backing) in calls


# ============================ 母盘判定（大小写不敏感） ============================


def test_backend_scan_skips_master_case_insensitive(backend):
    """母盘标记判定大小写不敏感：_TPL_ / _Tpl_ 变体也不得注册为子系统——
    否则母盘被当普通盘引导，且又不在 /masters 清单里（双重失败）。"""
    b, _, _ = backend
    for name in ("_tpl_ubuntu_2204.img", "_TPL_win11.img", "_Tpl_x.img", "worker-01.ubuntu.img"):
        with open(os.path.join(b.disk_dir, name), "wb") as f:
            f.write(b"x" * 1024)
    result = b.scan()
    assert {c["iqn"] for c in result["created"]} == {"iqn.2026-07.com.test:worker-01.ubuntu"}
    assert result["skipped"] == []  # 母盘被过滤：既不建也不报（与母盘清单口径一致）


def test_master_scanner_case_insensitive(tmp_path):
    """母盘清单同样认大小写变体（与 scan 的跳过条件严格一致：是母盘 ⇔ 进清单 ⇔ 不建子系统）。"""
    import app.main as agent_main

    d = tmp_path / "masters"
    d.mkdir()
    _write_gpt(str(d / "_tpl_ubuntu.img"), 4096)
    _write_gpt(str(d / "_TPL_win11.img"), 512)
    (d / "worker-01.ubuntu.img").write_bytes(b"x")
    scanner = agent_main.MasterScanner(str(d))
    items = scanner._scan()
    assert {i["name"]: i["gpt"] for i in items} == {
        "_tpl_ubuntu.img": "4k", "_TPL_win11.img": "512"}


# ============================ MasterScanner ============================


def test_master_scanner_gpt_field(tmp_path):
    """母盘清单带 gpt 布局字段（前端据此标注 512 不可克隆）；非 _tpl_ 文件不入清单。"""
    import app.main as agent_main

    d = tmp_path / "masters"
    d.mkdir()
    _write_gpt(str(d / "_tpl_ubuntu.img"), 4096)
    _write_gpt(str(d / "_tpl_win10.img"), 512)
    (d / "worker-01.ubuntu.img").write_bytes(b"x")
    scanner = agent_main.MasterScanner(str(d))
    items = scanner._scan()
    assert {i["name"]: i["gpt"] for i in items} == {
        "_tpl_ubuntu.img": "4k", "_tpl_win10.img": "512"}
