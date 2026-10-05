"""nvmet-host 宿主服务单测：mock configfs（NVMET_CONFIGFS 重定向）+ TestClient。

覆盖：鉴权、healthz、子系统 CRUD（严格模式/namespace/port 挂载）、host 认证
（全局 hosts/<hostnqn>/dhchap_key 写密钥即启用 + allowed_hosts symlink 准入）、
删除顺序（先摘 port / allowed_hosts 再删）。
真实内核 nvmet 验证留部署环境（本机无内核 nvmet）。
"""

import os

# Windows 路径段不允许冒号（WinError 267）：mock configfs 的 NQN 用点分隔（真实 NQN 冒号语义不受影响）
NQN = "nqn.2026-07.com.test.worker-01.ubuntu"
BACKING = "/srv/iscsi/worker-01.ubuntu.img"
# device_path 由宿主按 spec.nvmetHost.diskDir（conftest kurrent.yaml）重拼前缀
DEVICE_PATH = "/srv/nvmet-disks/worker-01.ubuntu.img"
HOST_NQN = "nqn.2026-07.com.kurrent.host.worker-01"
SECRET = "DHHC-1:01:YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo="
# 多盘同 worker：同一 hostnqn 被两个子系统（两块盘）共用
NQN_B = "nqn.2026-07.com.test.worker-01-b.ubuntu"
BACKING_B = "/srv/iscsi/worker-01-b.ubuntu.img"


def test_healthz_no_auth(nvmet, client, configfs):
    """healthz 是唯一不鉴权端点；configfs 未就绪（无 subsystems）→ false。"""
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "configfs": False}


def test_healthz_configfs_ready(nvmet, client, configfs):
    os.makedirs(configfs / "subsystems")
    assert client.get("/healthz").json()["configfs"] is True


def test_requires_auth(nvmet):
    """mTLS 鉴权：TLS 层由 uvicorn CERT_REQUIRED + 内部 CA 强制
    （TestClient 无法模拟 TLS 层），应用层 verify_client_cert 仅确认连接信息存在——
    无连接信息（request.client=None）→ 401。"""
    import pytest
    from fastapi import HTTPException

    class _NoPeer:
        client = None

    with pytest.raises(HTTPException) as exc_info:
        nvmet.verify_client_cert(_NoPeer())
    assert exc_info.value.status_code == 401


def test_capabilities(client):
    res = client.get("/capabilities")
    assert res.status_code == 200
    caps = res.json()
    assert caps["backend"] == "nvmet"
    assert caps["cd"] is False
    assert caps["port"] == {"trtype": "tcp", "trsvcid": "4420", "tsas": "none"}


def test_ensure_port(client, configfs):
    res = client.post("/port", params={"trsvcid": "4420"})
    assert res.status_code == 200
    port = configfs / "ports" / "1"
    assert (port / "addr_trtype").read_text().strip() == "tcp"
    assert (port / "addr_traddr").read_text().strip() == "0.0.0.0"
    assert (port / "addr_trsvcid").read_text().strip() == "4420"
    assert not (port / "addr_tsas").exists()  # 不写即无 TLS（内核属性仅接受 tls1.3）


def test_create_subsystem_strict(client, configfs, symlinks):
    """创建子系统：严格模式（allow_any_host=0）+ namespace/1 + port 挂载。"""
    res = client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    assert res.status_code == 201
    sub = configfs / "subsystems" / NQN
    assert (sub / "attr_allow_any_host").read_text().strip() == "0"
    ns = sub / "namespaces" / "1"
    # 盘路径按 spec.nvmetHost.diskDir 重拼（Agent 容器内路径 → 宿主容器内路径）
    assert (ns / "device_path").read_text().strip() == DEVICE_PATH
    assert (ns / "enable").read_text().strip() == "1"
    link = configfs / "ports" / "1" / "subsystems" / NQN
    assert os.path.islink(link)
    # configfs symlink 目标按进程 cwd 解析，实现必须用绝对路径
    assert symlinks[str(link)] == str(configfs / "subsystems" / NQN)


def test_create_duplicate_409(client, configfs):
    client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    res = client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    assert res.status_code == 409


def test_list_subsystems(client, configfs):
    client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    client.post("/subsystems", json={"nqn": NQN + ".2", "backing": BACKING + "2"})
    res = client.get("/subsystems")
    assert res.status_code == 200
    subs = {s["nqn"]: s for s in res.json()["subsystems"]}
    assert set(subs) == {NQN, NQN + ".2"}
    assert subs[NQN]["namespaces"] == [{"nsid": 1, "device_path": DEVICE_PATH}]
    assert subs[NQN]["hosts"] == []


def test_set_host_auth(client, configfs, symlinks):
    """host 认证：全局 hosts/<hostnqn>/dhchap_key 写 DHHC-1 明文（无换行，写 key 即启用），
    子系统先切严格模式（allow_any_host=0，内核在 allow_any=1 时拒绝 link 显式 host），
    再 symlink 挂到子系统 allowed_hosts/ 完成准入。"""
    client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    # 模拟遗留/手工创建的宽松子系统（内核默认 allow_any_host=1）
    (configfs / "subsystems" / NQN / "attr_allow_any_host").write_text("1")
    res = client.put(f"/subsystems/{NQN}/hosts",
                     json={"hostnqn": HOST_NQN, "secret": SECRET})
    assert res.status_code == 200
    # set_host 兜底切严格模式：allow_any_host=1 时内核拒绝 link 显式 host（-EINVAL）
    assert (configfs / "subsystems" / NQN / "attr_allow_any_host").read_text().strip() == "0"
    host_dir = configfs / "hosts" / HOST_NQN
    assert (host_dir / "dhchap_key").read_text() == SECRET  # newline=False：内容与密钥完全一致
    link = configfs / "subsystems" / NQN / "allowed_hosts" / HOST_NQN
    assert os.path.islink(link)
    # symlink 目标按进程 cwd 解析，实现必须用绝对路径
    assert symlinks[str(link)] == str(host_dir)


def test_set_host_idempotent(client, configfs):
    client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    for _ in range(2):
        res = client.put(f"/subsystems/{NQN}/hosts",
                         json={"hostnqn": HOST_NQN, "secret": SECRET})
        assert res.status_code == 200


def test_set_host_missing_subsystem_404(client, configfs):
    res = client.put(f"/subsystems/{NQN}/hosts",
                     json={"hostnqn": HOST_NQN, "secret": SECRET})
    assert res.status_code == 404


def test_delete_host(client, configfs):
    client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    client.put(f"/subsystems/{NQN}/hosts",
               json={"hostnqn": HOST_NQN, "secret": SECRET})
    res = client.delete(f"/subsystems/{NQN}/hosts/{HOST_NQN}")
    assert res.status_code == 200
    # 先摘 allowed_hosts 挂载，再删全局 hosts/<hostnqn>
    assert not (configfs / "subsystems" / NQN / "allowed_hosts" / HOST_NQN).exists()
    assert not (configfs / "hosts" / HOST_NQN).exists()


def test_delete_host_shared_entry_kept_until_last_reference(client, configfs):
    """多盘同 worker 共用同一 hostnqn：逐个 delete_host 期间全局条目必须保留。
    真实内核 configfs 对仍被 allowed_hosts symlink 引用的目录 rmdir 返回 EBUSY（实测）——
    若先删/报错，吊销会卡在第一个子系统、其余盘的旧密钥原样留存。"""
    for nqn, backing in ((NQN, BACKING), (NQN_B, BACKING_B)):
        client.post("/subsystems", json={"nqn": nqn, "backing": backing})
        client.put(f"/subsystems/{nqn}/hosts", json={"hostnqn": HOST_NQN, "secret": SECRET})
    # 第一块盘摘链接：仍被第二块盘引用 → 全局条目保留，密钥不动
    assert client.delete(f"/subsystems/{NQN}/hosts/{HOST_NQN}").status_code == 200
    assert not (configfs / "subsystems" / NQN / "allowed_hosts" / HOST_NQN).exists()
    assert (configfs / "hosts" / HOST_NQN / "dhchap_key").read_text() == SECRET
    # 最后一个引用摘除 → 回收
    assert client.delete(f"/subsystems/{NQN_B}/hosts/{HOST_NQN}").status_code == 200
    assert not (configfs / "hosts" / HOST_NQN).exists()


def test_delete_subsystem_gc_host_entry(client, configfs):
    """删除子系统回收其 host 链：全局条目在最后一个引用消失后才删（无引用残留即回收）。"""
    for nqn, backing in ((NQN, BACKING), (NQN_B, BACKING_B)):
        client.post("/subsystems", json={"nqn": nqn, "backing": backing})
        client.put(f"/subsystems/{nqn}/hosts", json={"hostnqn": HOST_NQN, "secret": SECRET})
    assert client.delete(f"/subsystems/{NQN}").status_code == 200
    assert (configfs / "hosts" / HOST_NQN).is_dir()   # 仍被 worker-01-b 引用
    assert client.delete(f"/subsystems/{NQN_B}").status_code == 200
    assert not (configfs / "hosts" / HOST_NQN).exists()


def test_delete_subsystem_detaches_port(client, configfs):
    client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    client.put(f"/subsystems/{NQN}/hosts",
               json={"hostnqn": HOST_NQN, "secret": SECRET})
    res = client.delete(f"/subsystems/{NQN}")
    assert res.status_code == 200
    assert not (configfs / "subsystems" / NQN).exists()
    assert not (configfs / "ports" / "1" / "subsystems" / NQN).exists()  # port 挂载已摘


def test_delete_subsystem_404(client, configfs):
    res = client.delete(f"/subsystems/{NQN}")
    assert res.status_code == 404


