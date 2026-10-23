"""部署形态回归：管理面只经 unix socket，身份头默认拒绝，PEP 不持有 CA 私钥。

这些属性在 TestClient 里看不到，只能对 nginx/compose/Dockerfile 做静态断言，
再由 tests/deploy/probe_edge.sh 在真实容器上复核。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONF = ROOT / "webui" / "deploy" / "nginx" / "nginx.conf"
SNIPPETS = ROOT / "webui" / "deploy" / "nginx" / "snippets"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "control_plane" / "Dockerfile"
NGINX_DOCKERFILE = ROOT / "webui" / "deploy" / "Dockerfile"


def _location_blocks(text: str) -> dict[str, str]:
    """提取 location 块（本配置没有嵌套 location，按花括号配对截取）。"""
    blocks: dict[str, str] = {}
    for match in re.finditer(r"location\s+(?:=|~\s*|\^~)?\s*([^\s{]+)\s*\{", text):
        depth, index = 1, match.end()
        while depth and index < len(text):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        blocks[match.group(1)] = text[match.end():index - 1]
    return blocks


def _service_block(compose: str, name: str) -> str:
    start = compose.index(f"  {name}:")
    try:
        end = compose.index("\n  kurrent-", start + 1)
    except ValueError:
        end = compose.index("\nvolumes:", start)
    return compose[start:end]


def test_management_plane_is_unix_socket_only():
    text = CONF.read_text(encoding="utf-8")
    assert "server unix:/run/kurrent/cp.sock;" in text
    assert "kurrent-control-plane:8080" not in text

    compose = COMPOSE.read_text(encoding="utf-8")
    control = _service_block(compose, "kurrent-control-plane")
    assert "ports:" not in control
    assert "127.0.0.1:4839" not in compose
    assert compose.count("kurrent-run:/run/kurrent") == 2

    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "--uds /run/kurrent/cp.sock" in dockerfile
    assert "--host" not in dockerfile
    assert "healthcheck.py" in dockerfile


def test_identity_headers_default_to_none_and_opt_in():
    text = CONF.read_text(encoding="utf-8")

    # server 级默认清空（每个反代 location 仍须显式 include，见下一个断言）
    for header in ("DN", "Issuer", "Serial", "Fingerprint", "Class"):
        assert f'proxy_set_header X-Client-{header} "";' in text or \
               f'proxy_set_header X-Client-Cert-{header} "";' in text

    blocks = _location_blocks(text)
    proxying = {path: body for path, body in blocks.items() if "proxy_pass" in body}
    assert set(proxying) == {
        "/healthz", "/boot-vars", "/devices/report", "/devices/challenge",
        "/devices/rotate", "/api/cp/", "/api/cp/enroll/renew",
    }

    for path, body in proxying.items():
        includes = re.findall(r"include\s+snippets/proxy-identity-(\w+)\.conf;", body)
        assert len(includes) == 1, f"{path} 必须恰好 include 一个身份头片段，实际 {includes}"
        expected = "client" if path in {"/api/cp/", "/api/cp/enroll/renew"} else "none"
        assert includes[0] == expected, f"{path} 身份头片段应为 {expected}"

    # 客户端自带头不得被原样转发：none 片段必须清空全部五个头
    none = (SNIPPETS / "proxy-identity-none.conf").read_text(encoding="utf-8")
    for header in ("DN", "Issuer", "Serial", "Fingerprint", "Class"):
        prefix = "X-Client-" if header == "Class" else "X-Client-Cert-"
        assert f'proxy_set_header {prefix}{header} "";' in none
    client = (SNIPPETS / "proxy-identity-client.conf").read_text(encoding="utf-8")
    for var in ("$valid_client_dn", "$valid_client_issuer",
                "$valid_client_serial", "$valid_client_fp", "$valid_client_class"):
        assert var in client
    # 审计取证：验证结果与失败时的原始 issuer（不参与身份判断）
    assert "X-Client-Cert-Verify $ssl_client_verify;" in client
    assert "X-Client-Cert-Raw-Issuer $ssl_client_i_dn;" in client
    assert 'proxy_set_header X-Client-Cert-Raw-Issuer "";' in none

    # issuer → class：只有 users/nodes 两个域进 443；组件证书不得被当作 human
    assert "map $ssl_client_i_dn $client_class_raw" in text
    assert '"~*CN=kurrent-ca-users" human;' in text
    assert '"~*CN=kurrent-ca-nodes" component;' in text
    assert "ssl_client_certificate /etc/nginx/pki/ca-bundle.pem;" in text
    # 客户端证书链的 CRL 校验：users + nodes 拼接（控制面吊销后 reload 生效）
    assert "ssl_crl /etc/nginx/pki/clients.crl;" in text

    blocks = _location_blocks(text)
    assert 'if ($valid_client_class = "component") { return 403; }' in blocks["/api/cp/"]
    assert 'add_header Cache-Control "no-store" always;' in blocks["/api/cp/"]
    # location 级 add_header 会取消 server 级继承：安全头必须在本地重列
    for header in ("X-Content-Type-Options nosniff", "X-Frame-Options SAMEORIGIN",
                   "Referrer-Policy no-referrer"):
        assert f"add_header {header} always;" in blocks["/api/cp/"]
    renew = blocks["/api/cp/enroll/renew"]
    assert 'if ($valid_client_class != "component") { return 403; }' in renew
    # 审计来源：renew 也必须带上 nginx 覆盖写入的真实客户端地址
    assert "proxy_set_header X-Real-IP $remote_addr;" in renew


def test_component_class_covers_legacy_promoted_ca():
    """遗留单 CA（CN=kurrent-ca）提升为 nodes 域后，存量组件证书仍判为 component：
    通用模式必须带 DN 边界，不能把 kurrent-ca-users 吞掉。"""
    text = CONF.read_text(encoding="utf-8")
    assert '"~*CN=kurrent-ca(,|/|$)" component;' in text
    assert '"~*CN=kurrent-ca-users" human;' in text


def test_control_plane_has_no_docker_access():
    """控制面不再经 docker.sock 操作 nginx/dnsmasq：容器不挂 socket，代码不依赖 SDK。"""
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "docker.sock" not in _service_block(compose, "kurrent-control-plane")
    requirements = (ROOT / "control_plane" / "requirements.txt").read_text(encoding="utf-8")
    assert "docker==" not in requirements
    for path in (ROOT / "control_plane" / "app").rglob("*.py"):
        code = path.read_text(encoding="utf-8")
        assert "import docker" not in code, path
        assert "from docker" not in code, path


def test_nginx_reloads_itself_on_crl_change():
    """CRL 变化 / 控制面哨兵 → 容器内 watcher 自己 `nginx -s reload` 并回写 ack。"""
    dockerfile = NGINX_DOCKERFILE.read_text(encoding="utf-8")
    assert "kurrent-watch.sh" in dockerfile
    script = (ROOT / "webui" / "deploy" / "nginx" / "kurrent-watch.sh").read_text(encoding="utf-8")
    assert "nginx -s reload" in script
    assert "nginx.reload" in script and "nginx.ack" in script
    # 控制面与 nginx 共享 kurrent-run 卷（哨兵文件载体）
    compose = COMPOSE.read_text(encoding="utf-8")
    nginx = _service_block(compose, "kurrent-nginx")
    assert "kurrent-run:/run/kurrent" in nginx


def test_pep_never_mounts_ca_private_key():
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "./control_plane/state/pki:/etc/nginx/pki:ro" not in compose
    # 只挂公开信任包目录（控制面写入 users + nodes 的 bundle），不挂任何私钥
    assert "./control_plane/state/pki/nginx:/etc/nginx/pki:ro" in compose

    dockerfile = NGINX_DOCKERFILE.read_text(encoding="utf-8")
    assert "nginx/snippets/ /etc/nginx/snippets/" in dockerfile
    assert "ca-bundle.pem" in dockerfile  # 启动前等待信任包就绪（控制面先生成）
    assert "clients.crl" in dockerfile    # 启动前等待 CRL 就绪（ssl_crl 缺文件会启动失败）


def test_boot_routes_are_exact_and_never_inject_identity():
    text = CONF.read_text(encoding="utf-8")
    assert "location ~ ^/(devices/|boot-vars)" not in text
    assert "location ^~ /devices/ {" in text

    for path in ("/boot-vars", "/devices/report", "/devices/challenge"):
        assert f"location = {path} {{" in text


def test_edge_hardening_headers_and_tls():
    """不回显版本；TLS 1.2（iPXE 上限）+ 1.3 并存；基础安全响应头。"""
    text = CONF.read_text(encoding="utf-8")
    assert "server_tokens off;" in text
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in text
    for header in ("X-Content-Type-Options nosniff",
                   "X-Frame-Options SAMEORIGIN",
                   "Referrer-Policy no-referrer"):
        assert f"add_header {header} always;" in text



def test_no_directory_listing_in_nginx():
    """引导链清单由控制面解析脚本生成：nginx 不提供目录列表，也没有 njs 执行面。"""
    text = CONF.read_text(encoding="utf-8")
    assert "location /browse/" not in text
    assert "location /api/browse/" not in text
    assert "js_content" not in text
    assert not (ROOT / "webui" / "deploy" / "nginx" / "njs").exists()


def test_static_roots_do_not_follow_symlinks():
    """public/ 与 tftp/ 由宿主机维护，解包可能带入符号链接：不得借此读出容器内
    其它路径（nginx 容器内还挂着 TOFU 服务端私钥）。"""
    blocks = _location_blocks(CONF.read_text(encoding="utf-8"))
    assert "disable_symlinks on from=/data;" in blocks["/file/"]
    assert "disable_symlinks on from=/tftp;" in blocks["/tftp/"]
