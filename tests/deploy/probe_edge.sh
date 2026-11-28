#!/usr/bin/env bash
# P1/P2/P3 部署形态验收：在真实容器上验证 I1/I2/I9/I11、主体类隔离、会话吊销、
# token scope、CRL 落盘与审计字段。前置：docker compose up -d 已运行；宿主机有 curl/python3。
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# 运行时状态在仓库外的状态根；旧布局仍在仓库内时回退到 control_plane/state。
STATE="${KURRENT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/kurrent}"
if [ -f "$STATE/control-plane/state/admin.conf" ]; then
    ADMIN_STATE="$STATE/control-plane/state"
else
    ADMIN_STATE="$ROOT/control_plane/state"
fi
CERT="$ADMIN_STATE/pki/users/admin/client.crt"
KEY="$ADMIN_STATE/pki/users/admin/client.key"
BASE="https://127.0.0.1"
FAILED=0

pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILED=1; }
code() { curl -sk --max-time 5 -o /dev/null -w '%{http_code}' "$@"; }

check_code() { # 期望码 描述 实际码
    if [ "$3" = "$1" ]; then pass "$2 ($3)"; else fail "$2 (want $1, got $3)"; fi
}

# ── I1：管理面没有 TCP 监听 ──
if docker exec kurrent-control-plane test -S /run/kurrent/cp.sock; then
    pass "control-plane unix socket exists"
else
    fail "control-plane unix socket missing"
fi
check_code 000 "host 127.0.0.1:4839 unreachable" "$(code http://127.0.0.1:4839/healthz)"
if docker exec kurrent-dnsmasq wget -q -T 2 -O- http://127.0.0.1:4839/healthz >/dev/null 2>&1; then
    fail "dnsmasq container can still reach 127.0.0.1:4839"
else
    pass "dnsmasq container cannot reach 127.0.0.1:4839"
fi
if docker exec kurrent-webui wget -q -T 2 -O- http://kurrent-control-plane:8080/healthz >/dev/null 2>&1; then
    fail "container on the same network can still reach control-plane:8080"
else
    pass "container on the same network cannot reach control-plane:8080"
fi

# ── I11：PEP 不持有 CA 私钥 ──
if docker exec kurrent-webui test -e /etc/nginx/pki/ca.key; then
    fail "ca.key is present in the nginx container"
else
    pass "ca.key is not present in the nginx container"
fi

# ── 唯一入口与身份断言 ──
check_code 200 "443 /healthz" "$(code "$BASE/healthz")"
check_code 200 "WebUI index" "$(code "$BASE/")"
check_code 401 "forged X-Client-Cert-DN rejected" \
    "$(code -H 'X-Client-Cert-DN: CN=admin,O=admin' "$BASE/api/cp/devices")"
check_code 200 "admin certificate accesses the management plane" \
    "$(code --cert "$CERT" --key "$KEY" "$BASE/api/cp/devices")"
check_code 401 "/config rejects forged identity headers" \
    "$(code -H 'X-Client-Cert-DN: CN=admin,O=admin' "$BASE/api/cp/config")"
CONFIG_JSON=$(curl -sk --max-time 5 --cert "$CERT" --key "$KEY" "$BASE/api/cp/config")
if printf '%s' "$CONFIG_JSON" | grep -q '"configHash"'; then
    pass "GET /config returns the effective declaration and configHash"
else
    fail "GET /config did not return configHash"
fi
WINDOW_JSON=$(curl -sk --max-time 5 --cert "$CERT" --key "$KEY" \
    "$BASE/api/cp/settings/registration-window")
if printf '%s' "$WINDOW_JSON" | grep -q '"limits"'; then
    pass "registration window returns limit declarations"
else
    fail "registration window did not return limits"
fi
check_code 404 "/devices/import is not exposed on 443" "$(code -X POST -d '{}' "$BASE/devices/import")"
check_code 404 "OpenAPI disabled by default" "$(code "$BASE/api/cp/openapi.json")"
check_code 401 "renew requires a client certificate" "$(code -X POST "$BASE/api/cp/enroll/renew")"

# ── 主体类隔离（P2）：issuer → class，跨类请求默认拒绝 ──
# 组件私钥在宿主机上是 root 0600；从容器内拷贝到临时目录（docker cp 归当前用户），
# 不要求执行探针的人有 root。
COMP_TMP=$(mktemp -d)
trap 'rm -rf "$COMP_TMP"' EXIT
COMP_CERT="" COMP_KEY=""
# 任意一个在控制面容器内真实存在的 agent 组件证书即可（宿主遗留目录可能不存在）
for dir in "$ADMIN_STATE"/pki/components/agent-*; do
    [ -d "$dir" ] || continue
    name=$(basename "$dir")
    docker cp "kurrent-control-plane:/app/state/pki/components/$name/client.crt" \
        "$COMP_TMP/client.crt" >/dev/null 2>&1
    docker cp "kurrent-control-plane:/app/state/pki/components/$name/client.key" \
        "$COMP_TMP/client.key" >/dev/null 2>&1
    if [ -s "$COMP_TMP/client.crt" ] && [ -s "$COMP_TMP/client.key" ]; then
        COMP_CERT="$COMP_TMP/client.crt"
        COMP_KEY="$COMP_TMP/client.key"
        break
    fi
done
if [ -n "$COMP_CERT" ]; then
    check_code 403 "node certificate rejected by the management plane (class=component)" \
        "$(code --cert "$COMP_CERT" --key "$COMP_KEY" "$BASE/api/cp/devices")"
else
    printf 'SKIP  class=component assertion (no usable component certificate)\n'
fi
check_code 403 "human certificate cannot renew (class=human)" \
    "$(code -X POST --cert "$CERT" --key "$KEY" -H 'Content-Type: application/json' -d '{}' \
        "$BASE/api/cp/enroll/renew")"

# ── 信任锚：nginx 只拿到 users+nodes 公钥包，无任何私钥 ──
if docker exec kurrent-webui test -s /etc/nginx/pki/ca-bundle.pem; then
    BUNDLE=$(docker exec kurrent-webui sh -c \
        'grep -c "BEGIN CERTIFICATE" /etc/nginx/pki/ca-bundle.pem' 2>/dev/null)
    if [ "$BUNDLE" = 2 ]; then
        pass "nginx trust bundle contains both users and nodes roots"
    else
        fail "nginx trust bundle certificate count mismatch (want 2, got ${BUNDLE:-?})"
    fi
else
    fail "nginx container is missing /etc/nginx/pki/ca-bundle.pem"
fi
if docker exec kurrent-webui sh -c 'ls /etc/nginx/pki/*.key 2>/dev/null' | grep -q .; then
    fail "a PKI private key is present in the nginx container"
else
    pass "no PKI private key in the nginx container"
fi

# ── P3：CRL 与审计（I9/I10）──
if docker exec kurrent-webui test -s /etc/nginx/pki/clients.crl; then
    CRL_COUNT=$(docker exec kurrent-webui sh -c \
        'grep -c "BEGIN X509 CRL" /etc/nginx/pki/clients.crl' 2>/dev/null)
    if [ "$CRL_COUNT" = 2 ]; then
        pass "nginx client CRL contains both users and nodes lists"
    else
        fail "client CRL count mismatch (want 2, got ${CRL_COUNT:-?})"
    fi
else
    fail "nginx container is missing /etc/nginx/pki/clients.crl"
fi
if docker exec kurrent-control-plane test -s /app/state/audit.jsonl; then
    if docker exec kurrent-control-plane grep -q '"request_id"' /app/state/audit.jsonl; then
        pass "audit events contain request_id"
    else
        fail "audit events are missing request_id"
    fi
else
    fail "the control plane did not write /app/state/audit.jsonl"
fi

# ── F-11：组件面 CRL 分发（nodes/control 双表，fail-closed 的原料在盘上）──
for crl in users.crl clients.crl nodes.crl control.crl; do
    if docker exec kurrent-control-plane test -s "/app/state/pki/nginx/$crl"; then
        pass "the control plane generated $crl"
    else
        fail "the control plane is missing /app/state/pki/nginx/$crl"
    fi
done
check_code 401 "component CRL endpoint requires a client certificate" "$(code "$BASE/api/cp/enroll/crl")"
check_code 403 "human certificate cannot fetch the component CRL (class=human)" \
    "$(code --cert "$CERT" --key "$KEY" "$BASE/api/cp/enroll/crl")"
if [ -n "$COMP_CERT" ]; then
    CRL_JSON=$(curl -sk --max-time 5 --cert "$COMP_CERT" --key "$COMP_KEY" "$BASE/api/cp/enroll/crl")
    if printf '%s' "$CRL_JSON" | python3 -c '
import json, sys
data = json.load(sys.stdin)
for key in ("nodes_crl", "control_crl"):
    assert "BEGIN X509 CRL" in data[key], key
assert data["next_update"], "no next_update"
' 2>/dev/null; then
        pass "component certificate can fetch the nodes and control CRLs"
    else
        fail "component CRL response has an unexpected shape or is missing PEM"
    fi
else
    printf 'SKIP  component CRL response assertion (no usable component certificate)\n'
fi
check_code 200 "public nodes.crl is anonymously available" "$(code "$BASE/crl/nodes.crl")"
if curl -sk --max-time 5 "$BASE/crl/nodes.crl" | head -1 | grep -q 'BEGIN X509 CRL'; then
    pass "public nodes.crl returns PEM"
else
    fail "public nodes.crl is not a PEM CRL"
fi
check_code 405 "public nodes.crl rejects POST" "$(code -X POST "$BASE/crl/nodes.crl")"
check_code 401 "the control CA ledger requires authentication" "$(code "$BASE/api/cp/pki/control")"
check_code 200 "admin can read the control CA ledger" \
    "$(code --cert "$CERT" --key "$KEY" "$BASE/api/cp/pki/control")"
check_code 404 "revoking a missing control certificate returns 404 (admin)" \
    "$(code -X DELETE --cert "$CERT" --key "$KEY" "$BASE/api/cp/pki/control/probe-none")"

# ── P3：会话登记与逐个吊销（I8）──
SESSION_JAR=$(mktemp)
SESSION_HDR=$(mktemp)
trap 'rm -rf "$COMP_TMP" "$SESSION_JAR" "$SESSION_HDR"' EXIT
LOGIN_CODE=$(curl -sk --max-time 5 --cert "$CERT" --key "$KEY" -D "$SESSION_HDR" \
    -c "$SESSION_JAR" -o /dev/null -w '%{http_code}' -X POST "$BASE/api/cp/session")
check_code 200 "management certificate exchanges a session" "$LOGIN_CODE"
check_code 200 "session cookie accesses the management plane" \
    "$(code -b "$SESSION_JAR" "$BASE/api/cp/whoami")"
SESS_ID=$(curl -sk --max-time 5 --cert "$CERT" --key "$KEY" "$BASE/api/cp/sessions" | \
    python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[-1]["session_id"] if d else "")')
if [ -n "$SESS_ID" ]; then
    check_code 200 "admin revokes a single session" \
        "$(code -X DELETE --cert "$CERT" --key "$KEY" "$BASE/api/cp/sessions/$SESS_ID")"
    check_code 401 "the same cookie is invalidated immediately after revocation" \
        "$(code -b "$SESSION_JAR" "$BASE/api/cp/whoami")"
else
    fail "session ledger is empty (cannot verify per-session revocation)"
fi

# ── P3：自动化 token scope（I8）──
SCOPE_JSON=$(curl -sk --max-time 5 --cert "$CERT" --key "$KEY" -H 'Content-Type: application/json' \
    -d '{"usage":"admin","scopes":["readonly"],"ttl_hours":1}' "$BASE/api/cp/pki/tokens")
TOKEN=$(printf '%s' "$SCOPE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
TOKEN_ID=$(printf '%s' "$TOKEN" | cut -d. -f1)
if [ -n "$TOKEN" ]; then
    check_code 200 "readonly token can read" \
        "$(code -H "Authorization: Bearer $TOKEN" "$BASE/api/cp/agents")"
    check_code 403 "readonly token cannot write" \
        "$(code -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
            -d '{"mac":"00:11:22:33:44:ff"}' "$BASE/api/cp/devices")"
    check_code 200 "readonly token can read the component certificate ledger" \
        "$(code -H "Authorization: Bearer $TOKEN" "$BASE/api/cp/pki/components")"
    check_code 403 "readonly token cannot revoke component certificates" \
        "$(code -X DELETE -H "Authorization: Bearer $TOKEN" \
            "$BASE/api/cp/pki/components/probe-none")"
    check_code 403 "readonly token cannot purge revoked devices" \
        "$(code -X DELETE -H "Authorization: Bearer $TOKEN" "$BASE/api/cp/devices/revoked")"
    check_code 200 "revoke the readonly token" \
        "$(code -X DELETE --cert "$CERT" --key "$KEY" "$BASE/api/cp/pki/tokens/$TOKEN_ID")"
    if docker exec kurrent-control-plane grep -qF "$TOKEN" /app/state/audit.jsonl; then
        fail "the audit file contains a plaintext token"
    else
        pass "the audit file does not contain a plaintext token"
    fi
else
    fail "failed to issue the readonly token"
fi

# ── enroll scope：只允许签发 bootstrap token（读接口与换会话同样拒绝）──
ENROLL_JSON=$(curl -sk --max-time 5 --cert "$CERT" --key "$KEY" -H 'Content-Type: application/json' \
    -d '{"usage":"admin","scopes":["enroll"],"ttl_hours":1}' "$BASE/api/cp/pki/tokens")
ENROLL_TOKEN=$(printf '%s' "$ENROLL_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
ENROLL_ID=$(printf '%s' "$ENROLL_TOKEN" | cut -d. -f1)
if [ -n "$ENROLL_TOKEN" ]; then
    check_code 403 "enroll token cannot read the device ledger" \
        "$(code -H "Authorization: Bearer $ENROLL_TOKEN" "$BASE/api/cp/devices")"
    check_code 403 "enroll token cannot read the credential ledger" \
        "$(code -H "Authorization: Bearer $ENROLL_TOKEN" "$BASE/api/cp/pki/tokens")"
    check_code 403 "enroll token cannot exchange a session" \
        "$(code -X POST -H "Authorization: Bearer $ENROLL_TOKEN" "$BASE/api/cp/session")"
    BOOT_JSON=$(curl -sk --max-time 5 -H "Authorization: Bearer $ENROLL_TOKEN" \
        -H 'Content-Type: application/json' -d '{"usage":"bootstrap","agent_id":"abcdef123456"}' \
        "$BASE/api/cp/pki/tokens")
    BOOT_ID=$(printf '%s' "$BOOT_JSON" | python3 -c \
        'import json,sys; d=json.load(sys.stdin); print(d.get("token","").split(".")[0])' 2>/dev/null)
    if [ -n "$BOOT_ID" ]; then
        pass "enroll token can issue bootstrap tokens"
        check_code 200 "revoke the probe bootstrap token" \
            "$(code -X DELETE --cert "$CERT" --key "$KEY" "$BASE/api/cp/pki/tokens/$BOOT_ID")"
    else
        fail "enroll token could not issue a bootstrap token"
    fi
    check_code 200 "revoke the enroll token" \
        "$(code -X DELETE --cert "$CERT" --key "$KEY" "$BASE/api/cp/pki/tokens/$ENROLL_ID")"
else
    fail "failed to issue the enroll token"
fi

# ── nvmet 部署最小权限：Agent 不需要 docker.sock ──
if docker inspect storager-agent >/dev/null 2>&1; then
    if docker inspect storager-agent --format '{{range .Mounts}}{{.Destination}} {{end}}' \
        | grep -q '/var/run/docker.sock'; then
        fail "storager-agent still mounts docker.sock"
    else
        pass "storager-agent does not mount docker.sock"
    fi
fi

# ── nvmet-host 管理面收口：共享卷 UDS，无 TCP 监听 ──
if ss -H -lnt 2>/dev/null | grep -q ':4841 '; then
    fail "the host still listens on TCP 4841"
else
    pass "the host does not listen on TCP 4841"
fi
if docker exec kurrent-nvmet-host test -S /run/kurrent/nvmet/nvmet-host.sock; then
    pass "the nvmet-host UDS exists"
else
    fail "the nvmet-host container is missing /run/kurrent/nvmet/nvmet-host.sock"
fi
if docker exec storager-agent test -S /run/kurrent/nvmet/nvmet-host.sock; then
    pass "the agent sees the same UDS"
else
    fail "the agent container cannot see the nvmet-host UDS"
fi
UDS_MODE=$(docker exec kurrent-nvmet-host stat -c '%a' /run/kurrent/nvmet 2>/dev/null)
if [ "$UDS_MODE" = "700" ]; then
    pass "the UDS directory is root-only (0700)"
else
    fail "unexpected UDS directory mode (want 700, got ${UDS_MODE:-?})"
fi

# ── 引导链状态与静态文件 ──
check_code 401 "boot chain status requires authentication" "$(code "$BASE/api/cp/boot-assets")"
BOOT_ASSETS=$(curl -sk --max-time 5 --cert "$CERT" --key "$KEY" "$BASE/api/cp/boot-assets")
if printf '%s' "$BOOT_ASSETS" | python3 -c '
import json, sys
data = json.load(sys.stdin)
assert data["firmware"], "no firmware"
assert data["stage2"], "no boot chain"
assert data["menu"]["status"] in ("ok", "unavailable")
' 2>/dev/null; then
    pass "boot chain status is readable (firmware + script chain + menu)"
else
    fail "boot chain status is unreadable or has an unexpected shape"
fi
check_code 200 "the iPXE menu is downloadable" "$(code "$BASE/tftp/boot.ipxe.cfg")"

# ── 静态根不跟随符号链接：tftp/ 里的链接不得读到容器内其它路径 ──
TFTP_LINK="$ROOT/tftp/.probe-symlink"
ln -sf /etc/nginx/certs/server.key "$TFTP_LINK" 2>/dev/null
if [ -L "$TFTP_LINK" ]; then
    check_code 403 "/tftp/ does not follow symlinks" "$(code "$BASE/tftp/.probe-symlink")"
else
    printf 'SKIP  symlink guard assertion (tftp/ is not writable)\n'
fi
rm -f "$TFTP_LINK"

# ── 管理面响应不可缓存 ──
if curl -sk --max-time 5 --cert "$CERT" --key "$KEY" -D - -o /dev/null \
    "$BASE/api/cp/devices" | grep -qi '^cache-control: no-store'; then
    pass "management responses carry Cache-Control: no-store"
else
    fail "management responses are missing Cache-Control: no-store"
fi
BOOT_VARS=$(curl -sk --max-time 5 "$BASE/boot-vars?hostname=worker-01&format=json")
if printf '%s' "$BOOT_VARS" | grep -q 'nbft_secret\|nbft_tls_psk'; then
    fail "an unsigned /boot-vars response contains data-plane credentials"
else
    pass "an unsigned /boot-vars response has no data-plane credentials"
fi

# ── P4 收尾：组件证书 / 设备清除 / 密钥轮换端点（无凭据与越权路径）──
check_code 401 "the component certificate ledger requires authentication" "$(code "$BASE/api/cp/pki/components")"
check_code 404 "revoking a missing component certificate returns 404 (admin)" \
    "$(code -X DELETE --cert "$CERT" --key "$KEY" "$BASE/api/cp/pki/components/probe-none")"
check_code 400 "revoking a component certificate with an invalid serial returns 400" \
    "$(code -X DELETE --cert "$CERT" --key "$KEY" \
        "$BASE/api/cp/pki/components/probe-none?serial=not-hex")"
check_code 401 "purging devices requires authentication" "$(code -X DELETE "$BASE/api/cp/devices/revoked")"
PURGE_EMPTY=$(curl -sk --max-time 5 -X DELETE --cert "$CERT" --key "$KEY" \
    "$BASE/api/cp/devices/revoked?dry_run=true&mac=00:11:22:33:44:ff" | \
    python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("count"))' 2>/dev/null)
if [ "$PURGE_EMPTY" = "0" ]; then
    pass "device purge dry_run leaves the ledger unchanged (count=0)"
else
    fail "device purge dry_run behaved unexpectedly (count=${PURGE_EMPTY:-?})"
fi

echo
if [ "$FAILED" -ne 0 ]; then
    echo "Result: failures found"
    exit 1
fi
echo "Result: all checks passed"
