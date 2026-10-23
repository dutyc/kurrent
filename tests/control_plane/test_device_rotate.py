"""设备密钥轮换：旧密钥签名 rotate||mac||new_pubkey||nonce，成功后旧密钥立即失效。"""

import base64

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

MAC = "00:11:22:33:44:55"
MAC_B = "00:11:22:33:44:66"


class Key:
    """ECDSA P-256 测试密钥：pubkey = 未压缩点 hex，sign = base64(DER)。"""

    def __init__(self):
        self.priv = ec.generate_private_key(ec.SECP256R1())
        self.pub = self.priv.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint).hex()

    def sign(self, data: str) -> str:
        return base64.b64encode(
            self.priv.sign(data.encode("utf-8"), ec.ECDSA(hashes.SHA256()))).decode()


def _challenge(client, mac=MAC) -> str:
    res = client.get("/devices/challenge", params={"mac": mac})
    assert res.status_code == 200, res.text
    return next(line[len("set nonce "):] for line in res.text.splitlines()
                if line.startswith("set nonce "))


def _rotate(client, *, mac=MAC, pubkey: str, nonce: str, sig: str):
    return client.post("/devices/rotate", params={
        "mac": mac, "pubkey": pubkey, "nonce": nonce, "sig": sig,
    })


def _claim(client, auth_headers, key: Key, mac=MAC):
    assert client.post("/settings/registration-window",
                       json={"ttl_minutes": 30}, headers=auth_headers).status_code == 201
    res = client.get("/devices/report", params={"mac": mac, "pubkey": key.pub})
    assert res.status_code == 200
    dev = client.get(f"/devices/{mac}", headers=auth_headers).json()
    assert dev["key_hash"] == key.pub


def _boot_vars(client, hostname, nonce, sig):
    return client.get("/boot-vars", params={
        "mac": MAC, "hostname": hostname, "nonce": nonce, "sig": sig,
    })


def test_rotate_invalidates_old_key_end_to_end(client, auth_headers):
    old = Key()
    _claim(client, auth_headers, old)
    worker = client.post("/workers", json={"worker_id": "worker-rotate", "mac": MAC},
                         headers=auth_headers)
    assert worker.status_code == 201, worker.text
    hostname = worker.json()["hostname"]

    nonce = _challenge(client)
    ok = _boot_vars(client, hostname, nonce, old.sign(f"{nonce}{MAC}"))
    assert "set menu-default reboot" in ok.text

    new = Key()
    nonce = _challenge(client)
    res = _rotate(client, pubkey=new.pub, nonce=nonce,
                  sig=old.sign(f"rotate{MAC}{new.pub}{nonce}"))
    assert res.status_code == 200, res.text
    assert res.json() == {"mac": MAC, "rotated": True}
    dev = client.get(f"/devices/{MAC}", headers=auth_headers).json()
    assert dev["key_hash"] == new.pub
    assert dev["key_rotated_at"]

    # 旧密钥在引导验签链路上立即失效
    nonce = _challenge(client)
    stale = _boot_vars(client, hostname, nonce, old.sign(f"{nonce}{MAC}"))
    assert stale.status_code == 200
    assert "# no per-worker boot vars found" in stale.text
    ops = client.get("/operations", params={"mac": MAC}, headers=auth_headers).json()["entries"]
    assert any(e["op"] == "boot_vars.credential" and e.get("reason") == "verify_failed"
               for e in ops)

    # 新密钥照常引导
    nonce = _challenge(client)
    fresh = _boot_vars(client, hostname, nonce, new.sign(f"{nonce}{MAC}"))
    assert "set menu-default reboot" in fresh.text


def test_rotate_replay_rejected(client, auth_headers):
    old = Key()
    _claim(client, auth_headers, old)
    new = Key()
    nonce = _challenge(client)
    sig = old.sign(f"rotate{MAC}{new.pub}{nonce}")
    assert _rotate(client, pubkey=new.pub, nonce=nonce, sig=sig).status_code == 200
    replay = _rotate(client, pubkey=new.pub, nonce=nonce, sig=sig)
    assert replay.status_code == 401
    assert "nonce" in replay.json()["detail"]


def test_rotate_requires_claimed_key(client, auth_headers):
    assert client.post("/devices", json={"mac": MAC_B}, headers=auth_headers).status_code == 201
    new = Key()
    res = _rotate(client, mac=MAC_B, pubkey=new.pub, nonce="0" * 64, sig="AAAA")
    assert res.status_code == 409
    assert "no key" in res.json()["detail"]


def test_rotate_revoked_device_rejected(client, auth_headers):
    old = Key()
    _claim(client, auth_headers, old)
    nonce = _challenge(client)
    assert client.delete(f"/devices/{MAC}", headers=auth_headers).status_code == 200
    new = Key()
    res = _rotate(client, pubkey=new.pub, nonce=nonce,
                  sig=old.sign(f"rotate{MAC}{new.pub}{nonce}"))
    assert res.status_code == 403
    assert "revoked" in res.json()["detail"]


def test_challenge_revoked_device_404(client, auth_headers):
    """吊销设备不再进入验签链路：不给 nonce，避免确认其存在。"""
    _claim(client, auth_headers, Key())
    assert client.delete(f"/devices/{MAC}", headers=auth_headers).status_code == 200
    assert client.get("/devices/challenge", params={"mac": MAC}).status_code == 404


def test_rotate_unknown_device_404(client):
    new = Key()
    res = _rotate(client, mac=MAC_B, pubkey=new.pub, nonce="0" * 64, sig="AAAA")
    assert res.status_code == 404


def test_rotate_rejects_invalid_pubkey(client):
    res = _rotate(client, mac=MAC, pubkey="not-a-key", nonce="0" * 64, sig="AAAA")
    assert res.status_code == 400
    assert "pubkey" in res.json()["detail"]


def test_rotate_rate_limited(client, auth_headers, monkeypatch):
    monkeypatch.setenv("KURRENT_CP_RATE_LIMIT_ROTATE", "1")
    from control_plane.app import ratelimit
    ratelimit.reset()
    old = Key()
    _claim(client, auth_headers, old)
    headers = {"X-Real-IP": "10.0.0.9"}
    new = Key()
    nonce = _challenge(client)
    res = client.post("/devices/rotate", params={
        "mac": MAC, "pubkey": new.pub, "nonce": nonce,
        "sig": old.sign(f"rotate{MAC}{new.pub}{nonce}"),
    }, headers=headers)
    assert res.status_code == 200, res.text
    assert client.post("/devices/rotate", params={
        "mac": MAC, "pubkey": new.pub, "nonce": nonce, "sig": "AAAA",
    }, headers=headers).status_code == 429
