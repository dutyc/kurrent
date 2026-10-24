#!/usr/bin/env bash
# P1/P2/P3 部署形态验收：在真实容器上验证 I1/I2/I9/I11、主体类隔离、会话吊销、
# token scope、CRL 落盘与审计字段。前置：docker compose up -d 已运行；宿主机有 curl/python3。
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CERT="$ROOT/control_plane/state/pki/users/admin/client.crt"
KEY="$ROOT/control_plane/state/pki/users/admin/client.key"
BASE="https://127.0.0.1"
FAILED=0

pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILED=1; }
code() { curl -sk --max-time 5 -o /dev/null -w '%{http_code}' "$@"; }

check_code() { # 期望码 描述 实际码
    if [ "$3" = "$1" ]; then pass "$2 ($3)"; else fail "$2（期望 $1，实际 $3）"; fi
}

# ── I1：管理面没有 TCP 监听 ──
if docker exec kurrent-control-plane test -S /run/kurrent/cp.sock; then
    pass "控制面 unix socket 存在"
else
    fail "控制面 unix socket 不存在"
fi
check_code 000 "宿主 127.0.0.1:4839 不可达" "$(code http://127.0.0.1:4839/healthz)"
if docker exec kurrent-dnsmasq wget -q -T 2 -O- http://127.0.0.1:4839/healthz >/dev/null 2>&1; then
    fail "dnsmasq 容器仍可访问 127.0.0.1:4839"
else
    pass "dnsmasq 容器无法访问 127.0.0.1:4839"
fi
if docker exec kurrent-webui wget -q -T 2 -O- http://kurrent-control-plane:8080/healthz >/dev/null 2>&1; then
    fail "同网容器仍可访问 control-plane:8080"
else
    pass "同网容器无法访问 control-plane:8080"
fi

# ── I11：PEP 不持有 CA 私钥 ──
if docker exec kurrent-webui test -e /etc/nginx/pki/ca.key; then
    fail "nginx 容器内存在 ca.key"
else
    pass "nginx 容器内没有 ca.key"
fi

# ── 唯一入口与身份断言 ──
check_code 200 "443 /healthz" "$(code "$BASE/healthz")"
check_code 200 "WebUI 首页" "$(code "$BASE/")"
check_code 401 "伪造 X-Client-Cert-DN 被拒" \
    "$(code -H 'X-Client-Cert-DN: CN=admin,O=admin' "$BASE/api/cp/devices")"
check_code 200 "admin 证书访问管理面" \
    "$(code --cert "$CERT" --key "$KEY" "$BASE/api/cp/devices")"
check_code 404 "/devices/import 不对 443 暴露" "$(code -X POST -d '{}' "$BASE/devices/import")"
check_code 404 "OpenAPI 默认关闭" "$(code "$BASE/api/cp/openapi.json")"
check_code 401 "renew 强制客户端证书" "$(code -X POST "$BASE/api/cp/enroll/renew")"

# ── 主体类隔离（P2）：issuer → class，跨类请求默认拒绝 ──
# 组件私钥在宿主机上是 root 0600；从容器内拷贝到临时目录（docker cp 归当前用户），
# 不要求执行探针的人有 root。
COMP_TMP=$(mktemp -d)
trap 'rm -rf "$COMP_TMP"' EXIT
COMP_CERT="" COMP_KEY=""
# 任意一个在控制面容器内真实存在的 agent 组件证书即可（宿主遗留目录可能不存在）
for dir in "$ROOT"/control_plane/state/pki/components/agent-*; do
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
    check_code 403 "节点证书被管理面拒绝（class=component）" \
        "$(code --cert "$COMP_CERT" --key "$COMP_KEY" "$BASE/api/cp/devices")"
else
    printf 'SKIP  class=component 断言（未找到可用组件证书）\n'
fi
check_code 403 "人类证书不能 renew（class=human）" \
    "$(code -X POST --cert "$CERT" --key "$KEY" -H 'Content-Type: application/json' -d '{}' \
        "$BASE/api/cp/enroll/renew")"

# ── 信任锚：nginx 只拿到 users+nodes 公钥包，无任何私钥 ──
if docker exec kurrent-webui test -s /etc/nginx/pki/ca-bundle.pem; then
    BUNDLE=$(docker exec kurrent-webui sh -c \
        'grep -c "BEGIN CERTIFICATE" /etc/nginx/pki/ca-bundle.pem' 2>/dev/null)
    if [ "$BUNDLE" = 2 ]; then
        pass "nginx 信任包含 users + nodes 两个根"
    else
        fail "nginx 信任包证书数异常（期望 2，实际 ${BUNDLE:-?}）"
    fi
else
    fail "nginx 容器缺少 /etc/nginx/pki/ca-bundle.pem"
fi
if docker exec kurrent-webui sh -c 'ls /etc/nginx/pki/*.key 2>/dev/null' | grep -q .; then
    fail "nginx 容器内存在 PKI 私钥"
else
    pass "nginx 容器内没有 PKI 私钥"
fi

# ── P3：CRL 与审计（I9/I10）──
if docker exec kurrent-webui test -s /etc/nginx/pki/clients.crl; then
    CRL_COUNT=$(docker exec kurrent-webui sh -c \
        'grep -c "BEGIN X509 CRL" /etc/nginx/pki/clients.crl' 2>/dev/null)
    if [ "$CRL_COUNT" = 2 ]; then
        pass "nginx 客户端 CRL 包含 users + nodes 两张表"
    else
        fail "客户端 CRL 数量异常（期望 2，实际 ${CRL_COUNT:-?}）"
    fi
else
    fail "nginx 容器缺少 /etc/nginx/pki/clients.crl"
fi
if docker exec kurrent-control-plane test -s /app/state/audit.jsonl; then
    if docker exec kurrent-control-plane grep -q '"request_id"' /app/state/audit.jsonl; then
        pass "审计事件含 request_id"
    else
        fail "审计事件缺少 request_id"
    fi
else
    fail "控制面未写 /app/state/audit.jsonl"
fi

# ── F-11：组件面 CRL 分发（nodes/control 双表，fail-closed 的原料在盘上）──
for crl in users.crl clients.crl nodes.crl control.crl; do
    if docker exec kurrent-control-plane test -s "/app/state/pki/nginx/$crl"; then
        pass "控制面已生成 $crl"
    else
        fail "控制面缺少 /app/state/pki/nginx/$crl"
    fi
done
check_code 401 "组件 CRL 端点强制客户端证书" "$(code "$BASE/api/cp/enroll/crl")"
check_code 403 "人类证书不能取组件 CRL（class=human）" \
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
        pass "组件证书可取 nodes + control CRL"
    else
        fail "组件 CRL 响应结构不符或缺少 PEM"
    fi
else
    printf 'SKIP  组件 CRL 响应断言（未找到可用组件证书）\n'
fi
check_code 200 "公开 nodes.crl 可匿名获取" "$(code "$BASE/crl/nodes.crl")"
if curl -sk --max-time 5 "$BASE/crl/nodes.crl" | head -1 | grep -q 'BEGIN X509 CRL'; then
    pass "公开 nodes.crl 返回 PEM"
else
    fail "公开 nodes.crl 不是 PEM CRL"
fi
check_code 405 "公开 nodes.crl 拒绝 POST" "$(code -X POST "$BASE/crl/nodes.crl")"
check_code 401 "control CA 台账需认证" "$(code "$BASE/api/cp/pki/control")"
check_code 200 "admin 可读 control CA 台账" \
    "$(code --cert "$CERT" --key "$KEY" "$BASE/api/cp/pki/control")"
check_code 404 "吊销不存在的 control 证书 404（admin）" \
    "$(code -X DELETE --cert "$CERT" --key "$KEY" "$BASE/api/cp/pki/control/probe-none")"

# ── P3：会话登记与逐个吊销（I8）──
SESSION_JAR=$(mktemp)
SESSION_HDR=$(mktemp)
trap 'rm -rf "$COMP_TMP" "$SESSION_JAR" "$SESSION_HDR"' EXIT
LOGIN_CODE=$(curl -sk --max-time 5 --cert "$CERT" --key "$KEY" -D "$SESSION_HDR" \
    -c "$SESSION_JAR" -o /dev/null -w '%{http_code}' -X POST "$BASE/api/cp/session")
check_code 200 "管理证书换会话" "$LOGIN_CODE"
check_code 200 "会话 Cookie 访问管理面" \
    "$(code -b "$SESSION_JAR" "$BASE/api/cp/whoami")"
SESS_ID=$(curl -sk --max-time 5 --cert "$CERT" --key "$KEY" "$BASE/api/cp/sessions" | \
    python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[-1]["session_id"] if d else "")')
if [ -n "$SESS_ID" ]; then
    check_code 200 "管理员吊销单个会话" \
        "$(code -X DELETE --cert "$CERT" --key "$KEY" "$BASE/api/cp/sessions/$SESS_ID")"
    check_code 401 "吊销后同一 Cookie 立即失效" \
        "$(code -b "$SESSION_JAR" "$BASE/api/cp/whoami")"
else
    fail "会话台账为空（无法验证逐个吊销）"
fi

# ── P3：自动化 token scope（I8）──
SCOPE_JSON=$(curl -sk --max-time 5 --cert "$CERT" --key "$KEY" -H 'Content-Type: application/json' \
    -d '{"usage":"admin","scopes":["readonly"],"ttl_hours":1}' "$BASE/api/cp/pki/tokens")
TOKEN=$(printf '%s' "$SCOPE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
TOKEN_ID=$(printf '%s' "$TOKEN" | cut -d. -f1)
if [ -n "$TOKEN" ]; then
    check_code 200 "readonly token 可读" \
        "$(code -H "Authorization: Bearer $TOKEN" "$BASE/api/cp/agents")"
    check_code 403 "readonly token 不可写" \
        "$(code -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
            -d '{"mac":"00:11:22:33:44:ff"}' "$BASE/api/cp/devices")"
    check_code 200 "readonly token 可读组件证书台账" \
        "$(code -H "Authorization: Bearer $TOKEN" "$BASE/api/cp/pki/components")"
    check_code 403 "readonly token 不可吊销组件证书" \
        "$(code -X DELETE -H "Authorization: Bearer $TOKEN" \
            "$BASE/api/cp/pki/components/probe-none")"
    check_code 403 "readonly token 不可清除已吊销设备" \
        "$(code -X DELETE -H "Authorization: Bearer $TOKEN" "$BASE/api/cp/devices/revoked")"
    check_code 200 "吊销 readonly token" \
        "$(code -X DELETE --cert "$CERT" --key "$KEY" "$BASE/api/cp/pki/tokens/$TOKEN_ID")"
    if docker exec kurrent-control-plane grep -qF "$TOKEN" /app/state/audit.jsonl; then
        fail "审计文件包含 token 明文"
    else
        pass "审计文件不含 token 明文"
    fi
else
    fail "readonly token 签发失败"
fi

# ── enroll scope：只允许签发 bootstrap token（读接口与换会话同样拒绝）──
ENROLL_JSON=$(curl -sk --max-time 5 --cert "$CERT" --key "$KEY" -H 'Content-Type: application/json' \
    -d '{"usage":"admin","scopes":["enroll"],"ttl_hours":1}' "$BASE/api/cp/pki/tokens")
ENROLL_TOKEN=$(printf '%s' "$ENROLL_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
ENROLL_ID=$(printf '%s' "$ENROLL_TOKEN" | cut -d. -f1)
if [ -n "$ENROLL_TOKEN" ]; then
    check_code 403 "enroll token 不能读设备台账" \
        "$(code -H "Authorization: Bearer $ENROLL_TOKEN" "$BASE/api/cp/devices")"
    check_code 403 "enroll token 不能读凭据台账" \
        "$(code -H "Authorization: Bearer $ENROLL_TOKEN" "$BASE/api/cp/pki/tokens")"
    check_code 403 "enroll token 不能换会话" \
        "$(code -X POST -H "Authorization: Bearer $ENROLL_TOKEN" "$BASE/api/cp/session")"
    BOOT_JSON=$(curl -sk --max-time 5 -H "Authorization: Bearer $ENROLL_TOKEN" \
        -H 'Content-Type: application/json' -d '{"usage":"bootstrap","agent_id":"probe-enroll"}' \
        "$BASE/api/cp/pki/tokens")
    BOOT_ID=$(printf '%s' "$BOOT_JSON" | python3 -c \
        'import json,sys; d=json.load(sys.stdin); print(d.get("token","").split(".")[0])' 2>/dev/null)
    if [ -n "$BOOT_ID" ]; then
        pass "enroll token 可签发 bootstrap token"
        check_code 200 "吊销 probe bootstrap token" \
            "$(code -X DELETE --cert "$CERT" --key "$KEY" "$BASE/api/cp/pki/tokens/$BOOT_ID")"
    else
        fail "enroll token 无法签发 bootstrap token"
    fi
    check_code 200 "吊销 enroll token" \
        "$(code -X DELETE --cert "$CERT" --key "$KEY" "$BASE/api/cp/pki/tokens/$ENROLL_ID")"
else
    fail "enroll token 签发失败"
fi

# ── nvmet 部署最小权限：Agent 不需要 docker.sock ──
if docker inspect storager-agent >/dev/null 2>&1; then
    if docker inspect storager-agent --format '{{range .Mounts}}{{.Destination}} {{end}}' \
        | grep -q '/var/run/docker.sock'; then
        fail "storager-agent 仍挂载 docker.sock"
    else
        pass "storager-agent 未挂载 docker.sock"
    fi
fi

# ── 引导链状态与静态文件 ──
check_code 401 "引导链状态需认证" "$(code "$BASE/api/cp/boot-assets")"
BOOT_ASSETS=$(curl -sk --max-time 5 --cert "$CERT" --key "$KEY" "$BASE/api/cp/boot-assets")
if printf '%s' "$BOOT_ASSETS" | python3 -c '
import json, sys
data = json.load(sys.stdin)
assert data["firmware"], "no firmware"
assert data["stage2"], "no boot chain"
assert data["menu"]["status"] in ("ok", "unavailable")
' 2>/dev/null; then
    pass "引导链状态可读（固件 + 脚本链 + 菜单）"
else
    fail "引导链状态不可读或结构不符"
fi
check_code 200 "iPXE 菜单可下载" "$(code "$BASE/tftp/boot.ipxe.cfg")"

# ── 静态根不跟随符号链接：public/ 或 tftp/ 里的链接不得读到容器内其它路径 ──
PUB_LINK="$ROOT/webui/deploy/public/.probe-symlink"
TFTP_LINK="$ROOT/tftp/.probe-symlink"
ln -sf /etc/nginx/certs/server.key "$PUB_LINK" 2>/dev/null
ln -sf /etc/nginx/certs/server.key "$TFTP_LINK" 2>/dev/null
if [ -L "$PUB_LINK" ] && [ -L "$TFTP_LINK" ]; then
    check_code 403 "/file/ 不跟随符号链接" "$(code "$BASE/file/.probe-symlink")"
    check_code 403 "/tftp/ 不跟随符号链接" "$(code "$BASE/tftp/.probe-symlink")"
else
    printf 'SKIP  符号链接守卫断言（public/ 或 tftp/ 不可写）\n'
fi
rm -f "$PUB_LINK" "$TFTP_LINK"

# ── 管理面响应不可缓存 ──
if curl -sk --max-time 5 --cert "$CERT" --key "$KEY" -D - -o /dev/null \
    "$BASE/api/cp/devices" | grep -qi '^cache-control: no-store'; then
    pass "管理面响应带 Cache-Control: no-store"
else
    fail "管理面响应缺少 Cache-Control: no-store"
fi
BOOT_VARS=$(curl -sk --max-time 5 "$BASE/boot-vars?hostname=worker-01&format=json")
if printf '%s' "$BOOT_VARS" | grep -q 'nbft_secret\|nbft_tls_psk'; then
    fail "无签名 /boot-vars 响应包含数据面凭据"
else
    pass "无签名 /boot-vars 不含数据面凭据"
fi

# ── P4 收尾：组件证书 / 设备清除 / 密钥轮换端点（无凭据与越权路径）──
check_code 401 "组件证书台账需认证" "$(code "$BASE/api/cp/pki/components")"
check_code 404 "吊销不存在的组件证书 404（admin）" \
    "$(code -X DELETE --cert "$CERT" --key "$KEY" "$BASE/api/cp/pki/components/probe-none")"
check_code 400 "非法序列号的组件证书吊销 400" \
    "$(code -X DELETE --cert "$CERT" --key "$KEY" \
        "$BASE/api/cp/pki/components/probe-none?serial=not-hex")"
check_code 401 "设备清除需认证" "$(code -X DELETE "$BASE/api/cp/devices/revoked")"
PURGE_EMPTY=$(curl -sk --max-time 5 -X DELETE --cert "$CERT" --key "$KEY" \
    "$BASE/api/cp/devices/revoked?dry_run=true&mac=00:11:22:33:44:ff" | \
    python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("count"))' 2>/dev/null)
if [ "$PURGE_EMPTY" = "0" ]; then
    pass "设备清除 dry_run 不改台账（count=0）"
else
    fail "设备清除 dry_run 行为异常（count=${PURGE_EMPTY:-?}）"
fi
check_code 400 "轮换请求的非法公钥被拒" \
    "$(code -X POST "$BASE/devices/rotate?mac=02:00:00:00:00:ff&pubkey=04$(printf 'ab%.0s' $(seq 64))&nonce=n&sig=s")"

echo
if [ "$FAILED" -ne 0 ]; then
    echo "结果：存在失败项"
    exit 1
fi
echo "结果：全部通过"
