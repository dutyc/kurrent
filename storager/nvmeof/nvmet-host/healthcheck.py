#!/usr/bin/env python3
"""容器健康检查：经共享卷 UDS + mTLS 调 /healthz（configfs 就绪才健康）。"""

import datetime as dt
import http.client
import json
import os
import socket
import ssl
import sys

from cryptography import x509

SOCKET_PATH = "/run/kurrent/nvmet/nvmet-host.sock"
PKI_DIR = "/etc/kurrent/pki"


class UnixHTTPSConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, context: ssl.SSLContext, timeout: float):
        super().__init__("127.0.0.1", timeout=timeout)
        self._socket_path = socket_path
        self._context = context

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
            self.sock = self._context.wrap_socket(sock, server_hostname="127.0.0.1")
        except BaseException:
            sock.close()
            raise


def get(conn: UnixHTTPSConnection, path: str) -> tuple[int, bytes]:
    conn.request("GET", path)
    resp = conn.getresponse()
    return resp.status, resp.read()


def main() -> int:
    crl_path = os.path.join(PKI_DIR, "nodes.crl")
    try:
        with open(crl_path, "rb") as fh:
            crl = x509.load_pem_x509_crl(fh.read())
    except (OSError, ValueError):
        return 1
    if dt.datetime.now(dt.timezone.utc) >= crl.next_update_utc:
        return 1
    ctx = ssl.create_default_context(cafile=os.path.join(PKI_DIR, "ca-nodes.crt"))
    ctx.load_verify_locations(crl_path)
    ctx.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
    ctx.load_cert_chain(os.path.join(PKI_DIR, "client.crt"), os.path.join(PKI_DIR, "client.key"))
    conn = UnixHTTPSConnection(SOCKET_PATH, ctx, 3.0)
    try:
        status, body = get(conn, "/healthz")
        if status != 200 or not json.loads(body).get("configfs"):
            return 1
        # 受保护端点：同时验证 mTLS 客户端证书被接受（/healthz 本身不鉴权）
        status, _ = get(conn, "/capabilities")
        return 0 if status == 200 else 1
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError):
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
