"""nvmet-host 宿主服务单测：mock configfs（NVMET_CONFIGFS 重定向）+ TestClient。

覆盖：鉴权、healthz、子系统 CRUD（严格模式/namespace/双端口挂载）、host 认证
（全局 hosts/<hostnqn>/dhchap_key 写密钥即启用 + allowed_hosts symlink 准入）、
TLS-PSK 供给（psk key 注入 keyring + 子系统挂 TLS 端口）、删除顺序（先摘端口/
allowed_hosts 再删）。
真实内核 nvmet 验证留部署环境（本机无内核 nvmet）；keyring 用 fake（真实注入需
宿主 .nvme keyring + 容器 seccomp/systempaths 豁免，见 blueprint/nvme-tcp-tls-in-kernel.md）。
"""

import os

import pytest

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
PSK_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


class _FakeKeyring:
    """替身：真实 NvmePskKeyring 走内核 syscall + /proc/keys，单测环境不可用。"""

    def __init__(self):
        self.keys: dict[tuple[str, str], bytes] = {}

    def set_key(self, hostnqn, subsysnqn, psk):
        self.keys[(hostnqn, subsysnqn)] = psk
        return 0x2A

    def delete_key(self, hostnqn, subsysnqn):
        return self.keys.pop((hostnqn, subsysnqn), None) is not None


@pytest.fixture()
def keyring(nvmet, monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setattr(nvmet.manager, "keyring", fake)
    return fake


@pytest.fixture()
def tls_port(nvmet):
    """置位 TLS 端口就绪（startup 的 ensure_tls_port 不在单测里跑）。"""
    nvmet.manager.tls_ready = True
    yield
    nvmet.manager.tls_ready = False


def test_healthz_no_auth(nvmet, client, configfs):
    """healthz 是唯一不鉴权端点；configfs 未就绪（无 subsystems）→ false。"""
    res = client.get("/healthz")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["configfs"] is False
    assert body["crl"] == {}


def test_healthz_configfs_ready(nvmet, client, configfs):
    os.makedirs(configfs / "subsystems")
    assert client.get("/healthz").json()["configfs"] is True


def test_requires_auth(nvmet):
    """mTLS 鉴权：TLS 层由 uvicorn CERT_REQUIRED + 内部 CA 强制。
    UDS 无 TCP 对端，request.client 恒 None，应用层以 scheme=https 断言 TLS 已生效——
    scheme=http（明文启动）→ 401。"""
    import pytest
    from fastapi import HTTPException

    class _Plain:
        scope = {"scheme": "http"}

    class _TLS:
        scope = {"scheme": "https"}

    with pytest.raises(HTTPException) as exc_info:
        nvmet.verify_client_cert(_Plain())
    assert exc_info.value.status_code == 401
    nvmet.verify_client_cert(_TLS())  # 不抛异常 = TLS 路径放行


def test_plain_http_rejected(nvmet):
    """明文起服（或代理未走 TLS）时受保护端点必须 401，防止断言空转。"""
    from fastapi.testclient import TestClient

    plain = TestClient(nvmet.app, base_url="http://testserver")
    assert plain.get("/capabilities").status_code == 401


def test_capabilities(client, nvmet):
    """数据面只有一个端口（TLS 1.3）；ready=false 表示 startup 未创建成功/未创建。"""
    res = client.get("/capabilities")
    assert res.status_code == 200
    caps = res.json()
    assert caps["backend"] == "nvmet"
    assert caps["cd"] is False
    assert caps["port"] == {
        "port_id": nvmet.manager.tls_port_id, "trtype": "tcp",
        "trsvcid": "4421", "tsas": "tls1.3", "ready": False,
    }


def test_capabilities_port_none_when_disabled(client, nvmet, monkeypatch):
    """数据面端口停用（NVMET_TLS_PORT_ID 空）→ 上报 null，控制面据此拒绝引导。"""
    monkeypatch.setattr(nvmet.manager, "tls_port_id", "")
    assert client.get("/capabilities").json()["port"] is None


def test_ensure_port(client, configfs, nvmet):
    """POST /port 幂等确认数据面端口：addr_tsas=tls1.3，且不建历史明文口。"""
    res = client.post("/port")
    assert res.status_code == 200
    port = configfs / "ports" / nvmet.manager.tls_port_id
    assert (port / "addr_trtype").read_text().strip() == "tcp"
    assert (port / "addr_traddr").read_text().strip() == "0.0.0.0"
    assert (port / "addr_trsvcid").read_text().strip() == "4421"
    assert (port / "addr_tsas").read_text().strip() == "tls1.3"
    # 明文口不再创建
    assert not (configfs / "ports" / nvmet.manager.legacy_plain_port_id).exists()


def test_ensure_tls_port_writes_tsas(client, configfs, nvmet):
    """数据面端口：addr_tsas=tls1.3。"""
    info = nvmet.manager.ensure_tls_port()
    assert info["enabled"] is True
    assert nvmet.manager.tls_ready is True
    port = configfs / "ports" / nvmet.manager.tls_port_id
    assert (port / "addr_trtype").read_text().strip() == "tcp"
    assert (port / "addr_traddr").read_text().strip() == "0.0.0.0"
    assert (port / "addr_trsvcid").read_text().strip() == "4421"
    assert (port / "addr_tsas").read_text().strip() == "tls1.3"


def test_remove_legacy_plain_port(client, configfs, nvmet):
    """升级清理：历史明文口连同其子系统挂载一并拆除，幂等。"""
    plain = configfs / "ports" / nvmet.manager.legacy_plain_port_id
    (plain / "subsystems").mkdir(parents=True)
    os.symlink(configfs / "subsystems", plain / "subsystems" / NQN)
    assert nvmet.manager.remove_legacy_plain_port() == nvmet.manager.legacy_plain_port_id
    assert not plain.exists()
    assert nvmet.manager.remove_legacy_plain_port() is None  # 幂等


def test_create_subsystem_attaches_data_port(client, configfs, nvmet, tls_port):
    """数据面端口就绪时，子系统挂到该口（固件段与内核段共用）。"""
    client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    assert os.path.islink(configfs / "ports" / nvmet.manager.tls_port_id / "subsystems" / NQN)


def test_create_subsystem_no_attach_when_not_ready(client, configfs, nvmet):
    """端口未就绪（startup 失败/未启用）时不挂载——不能指向不存在的端口。"""
    client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    assert not os.path.exists(configfs / "ports" / nvmet.manager.tls_port_id / "subsystems" / NQN)


def test_create_subsystem_strict(client, configfs, symlinks, nvmet, tls_port):
    """创建子系统：严格模式（allow_any_host=0）+ namespace/1 + 数据面端口挂载。"""
    res = client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    assert res.status_code == 201
    sub = configfs / "subsystems" / NQN
    assert (sub / "attr_allow_any_host").read_text().strip() == "0"
    ns = sub / "namespaces" / "1"
    # 盘路径按 spec.nvmetHost.diskDir 重拼（Agent 容器内路径 → 宿主容器内路径）
    assert (ns / "device_path").read_text().strip() == DEVICE_PATH
    assert (ns / "enable").read_text().strip() == "1"
    link = configfs / "ports" / nvmet.manager.tls_port_id / "subsystems" / NQN
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


def test_delete_subsystem_detaches_port(client, configfs, nvmet, tls_port):
    client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    client.put(f"/subsystems/{NQN}/hosts",
               json={"hostnqn": HOST_NQN, "secret": SECRET})
    assert os.path.islink(configfs / "ports" / nvmet.manager.tls_port_id / "subsystems" / NQN)
    res = client.delete(f"/subsystems/{NQN}")
    assert res.status_code == 200
    assert not (configfs / "subsystems" / NQN).exists()
    assert not (configfs / "ports" / nvmet.manager.tls_port_id / "subsystems" / NQN).exists()


def test_set_tls_key_injects_and_attaches(client, configfs, nvmet, keyring, tls_port):
    """TLS-PSK 供给：psk key 入 keyring（identity 由 hostnqn+subsysnqn 构造）+ 子系统挂 TLS 口。"""
    client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    res = client.put(f"/subsystems/{NQN}/tls",
                     json={"hostnqn": HOST_NQN, "psk": PSK_HEX})
    assert res.status_code == 200
    assert keyring.keys[(HOST_NQN, NQN)] == bytes.fromhex(PSK_HEX)
    assert os.path.islink(configfs / "ports" / nvmet.manager.tls_port_id / "subsystems" / NQN)


def test_set_tls_key_rejects_bad_psk(client, configfs, keyring, tls_port):
    client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    assert client.put(f"/subsystems/{NQN}/tls",
                      json={"hostnqn": HOST_NQN, "psk": "zz"}).status_code == 400
    assert client.put(f"/subsystems/{NQN}/tls",
                      json={"hostnqn": HOST_NQN, "psk": "ab" * 16}).status_code == 400
    assert keyring.keys == {}


def test_set_tls_key_missing_subsystem_404(client, configfs, keyring, tls_port):
    res = client.put(f"/subsystems/{NQN}/tls",
                     json={"hostnqn": HOST_NQN, "psk": PSK_HEX})
    assert res.status_code == 404


def test_set_tls_key_409_when_tls_disabled(client, configfs, nvmet, keyring, monkeypatch):
    """TLS 端口未配置（NVMET_TLS_PORT_ID 空）→ 明确 409，不静默写入。"""
    monkeypatch.setattr(nvmet.manager, "tls_port_id", "")
    client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    res = client.put(f"/subsystems/{NQN}/tls",
                     json={"hostnqn": HOST_NQN, "psk": PSK_HEX})
    assert res.status_code == 409


def test_delete_tls_key(client, configfs, keyring, tls_port):
    client.post("/subsystems", json={"nqn": NQN, "backing": BACKING})
    client.put(f"/subsystems/{NQN}/tls", json={"hostnqn": HOST_NQN, "psk": PSK_HEX})
    res = client.delete(f"/subsystems/{NQN}/tls/{HOST_NQN}")
    assert res.status_code == 200
    assert res.json()["deleted"] is True
    assert (HOST_NQN, NQN) not in keyring.keys


def test_delete_tls_key_absent_is_false(client, configfs, keyring, tls_port):
    res = client.delete(f"/subsystems/{NQN}/tls/{HOST_NQN}")
    assert res.status_code == 200
    assert res.json()["deleted"] is False


def test_delete_subsystem_404(client, configfs):
    res = client.delete(f"/subsystems/{NQN}")
    assert res.status_code == 404


def test_key_serials_scoped_to_nvme_keyring(nvmet, tmp_path, monkeypatch):
    """回归：只认 .nvme 内挂载的 key，忽略 /proc/keys 里其它 keyring 的同名条目。

    真实故障：手工 `keyctl padd` 进会话 keyring 的旧 psk key 对 root 进程在 /proc/keys
    可见，但不在 .nvme 里；按 /proc/keys 全局找 key 会命中它，UNLINK 得 ENOENT（实测），
    整条 TLS 推送 500；且 .nvme 里真正的旧 key 不被摘除，重复 key 越积越多。
    """
    import struct

    kf = tmp_path / "keys"
    # 顺序即 /proc/keys 顺序：0x11 是「可见但不在 .nvme」的陈旧同名 key，摆在最前
    kf.write_text(
        "00000010 I------     1 perm 1f1f0000     0     0 keyring   .nvme: 1\n"
        "00000011 I--Q---     1 perm 3b010000     0     0 psk       NVMe0R01 h s\n"
        "00000012 I--Q---     1 perm 3b010000     0     0 psk       NVMe0R01 h s\n",
        encoding="utf-8")
    nk = nvmet.NvmePskKeyring(keys_file=str(kf))

    class _FakeLibc:
        """.nvme 内实际只挂 0x12；0x11 只存在于别的 keyring。"""

        def syscall(self, nr, op, *args):
            if op == nvmet.NvmePskKeyring._KEYCTL_READ:
                buf, buflen = args[1], args[2]
                data = struct.pack("<i", 0x12)
                buf.raw = data + b"\x00" * (buflen - len(data))
                return len(data)
            return 0

    monkeypatch.setattr(nk, "_libc", _FakeLibc())
    assert nk._key_serials("NVMe0R01 h s") == [0x12]


def test_keyring_serial_found_when_empty(nvmet, tmp_path):
    """回归：空 keyring 在 /proc/keys 里渲染为 `.nvme: empty`（后缀不是数字）。

    真实故障：宿主重启后 nvme-keyring 模块把 .nvme 重建为空，/proc/keys 显示
    `.nvme: empty`；旧解析只剥离 `: <digits>`，于是 keyring_serial() 找不到 .nvme →
    `PUT /subsystems/{nqn}/tls` 恒定 500 → reconcile 再也注不回 PSK（新 TLS 连接全超时）。
    """
    kf = tmp_path / "keys"
    kf.write_text(
        "20f492d5 I------     4 perm 1f1f0000     0     0 keyring   .nvme: empty\n",
        encoding="utf-8")
    nk = nvmet.NvmePskKeyring(keys_file=str(kf))
    assert nk.keyring_serial() == 0x20F492D5
