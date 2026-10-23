"""容器健康检查：经 unix socket 请求 /healthz（管理面不再监听 TCP）。"""

import socket
import sys

SOCKET_PATH = "/run/kurrent/cp.sock"
REQUEST = b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"


def main() -> int:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(SOCKET_PATH)
            sock.sendall(REQUEST)
            head = sock.recv(128)
    except OSError:
        return 1
    status_line = head.split(b"\r\n", 1)[0]
    return 0 if b" 200 " in status_line else 1


if __name__ == "__main__":
    sys.exit(main())
