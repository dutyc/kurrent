#!/usr/bin/env bash
# kurrent 宿主基线检查（认证体系 P4 / 蓝图 §11.4）。
#
# 认证体系只保证"远程与局域网攻击者必须持有密码学凭据"；宿主上一旦有旁路监听、
# Docker 端口直通或世界可读的私钥，这个保证就不成立。默认只读检查，发现项由运维
# 按提示处理；唯一的自动修复是 --fix-4841（显式 opt-in，幂等 iptables 规则）。
#
# 用法：bash control_plane/host-baseline.sh
#       sudo bash control_plane/host-baseline.sh --fix-4841
set -u

fix_4841() {
    if [ "$(id -u)" -ne 0 ]; then
        printf '%s\n' "--fix-4841 需要 root：sudo bash $0 --fix-4841" >&2
        exit 2
    fi
    if ! command -v iptables >/dev/null 2>&1; then
        printf '%s\n' '未找到 iptables：请改用 nftables/firewalld 等价规则' >&2
        exit 1
    fi
    rule() {
        if iptables -C INPUT -p tcp --dport 4841 "$@" 2>/dev/null; then
            printf '  ok   已存在: iptables INPUT -p tcp --dport 4841 %s\n' "$*"
        else
            iptables -I INPUT 1 -p tcp --dport 4841 "$@" \
                && printf '  add  iptables INPUT -p tcp --dport 4841 %s\n' "$*"
        fi
    }
    printf '== 4841 收口（按插入顺序保证 ACCEPT 在 DROP 之前）\n'
    rule -m comment --comment kurrent-4841-default -j DROP
    rule -s 172.16.0.0/12 -m comment --comment kurrent-4841-docker -j ACCEPT
    rule -i lo -m comment --comment kurrent-4841-loopback -j ACCEPT
    printf '规则已就绪；4841 仍监听非回环地址时，规则只遮蔽端口，建议同时把服务改绑 127.0.0.1\n'
    printf '校验：ss -lntp | grep 4841 ; 然后重跑 bash %s\n' "$0"
}

if [ "${1:-}" = "--fix-4841" ]; then
    fix_4841
    exit 0
fi

failed=0
warned=0
ok()   { printf '  ok   %s\n' "$*"; }
warn() { printf '  warn %s\n' "$*"; warned=$((warned + 1)); }
fail() { printf '  FAIL %s\n' "$*"; failed=$((failed + 1)); }

# ── 1. 对外监听面 ──
# 控制面宿主只应有：22（运维）、53/67/69（dnsmasq 同宿主）、80/443（nginx）。
# 存储节点另有 4840（Agent）与 4421（NVMe/TCP），4841 必须只绑回环。
# 只看 TCP：UDP 的 mDNS/浏览器/mihomo 等临时端口是桌面常态，不构成本项目的暴露面。
printf '== 1/4 对外监听（TCP）\n'
allowed=" 22 53 67 69 80 443 4840 4421 "
listeners="$(ss -H -lnt 2>/dev/null || true)"
if [ -z "$listeners" ]; then
    warn "无法读取监听表（缺少 ss？）"
else
    while read -r _state _ _ local _; do
        [ -n "${local:-}" ] || continue
        addr="${local%:*}"
        port="${local##*:}"
        case "$addr" in
            127.*|"[::1]"|"::1") continue ;;
        esac
        if [ "$port" = "4841" ]; then
            fail "4841 绑定在非回环地址（nvmet-host 管理口只能宿主本地访问）: $local（--fix-4841 收口或改绑 127.0.0.1）"
            continue
        fi
        case "$allowed" in
            *" $port "*) ;;
            *)
                hint=""
                case "$port" in
                    3306) hint="（MySQL）" ;;
                    2375|2376) hint="（Docker TCP，等同宿主 root）" ;;
                    7897) hint="（mihomo/代理）" ;;
                    53317) hint="（LocalSend）" ;;
                    22000) hint="（Syncthing）" ;;
                esac
                fail "非预期公共监听 tcp $local$hint（关闭该服务或收口到回环）"
                ;;
        esac
    done <<< "$listeners"
fi

# ── 2. Docker 端口与运行时权限 ──
printf '== 2/4 Docker 暴露面\n'
if ! command -v docker >/dev/null 2>&1; then
    warn "未安装 docker：跳过容器检查"
else
    if docker info 2>/dev/null | grep -q 'tcp://'; then
        fail "Docker 守护进程暴露 TCP（2375/2376）：等同宿主 root"
    else
        ok "Docker 守护进程未暴露 TCP"
    fi
    ports="$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null || true)"
    if printf '%s\n' "$ports" | grep -q '^kurrent-control-plane '; then
        cp_ports="$(printf '%s\n' "$ports" | awk '$1=="kurrent-control-plane" {$1=""; print}')"
        if printf '%s' "$cp_ports" | grep -q '0.0.0.0:\|\[::\]:'; then
            fail "控制面发布了宿主端口（管理面必须只经 unix socket）: $cp_ports"
        else
            ok "控制面未发布宿主端口"
        fi
    fi
    if docker inspect kurrent-control-plane --format '{{range .Mounts}}{{.Destination}} {{end}}' \
        2>/dev/null | grep -q '/var/run/docker.sock'; then
        fail "控制面挂载了 docker.sock（应已删除：dnsmasq 自监听、nginx 自 reload）"
    else
        ok "控制面未挂载 docker.sock"
    fi
    if docker inspect kurrent-webui --format '{{range .Mounts}}{{.Source}} {{end}}' 2>/dev/null \
        | grep -q 'ca.key'; then
        fail "nginx 挂载了 PKI 私钥目录（只能挂 nginx/ 信任包）"
    else
        ok "nginx 未挂载 PKI 私钥"
    fi
fi

# ufw 不过滤 Docker 发布端口：必须在 DOCKER-USER 链收口
printf '== 3/4 防火墙收口\n'
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q 'Status: active'; then
    rules="$(iptables -S DOCKER-USER 2>/dev/null | grep -v '^-N' | grep -v '^-P' || true)"
    if [ -z "$rules" ]; then
        warn "ufw 已启用但 DOCKER-USER 为空：Docker 发布端口绕过 ufw，需在此链收口"
    else
        ok "DOCKER-USER 已有规则"
    fi
else
    warn "ufw 未启用或不可读：确认 Docker 端口由上游防火墙/DOCKER-USER 收口"
fi

# ── 4. 凭据文件权限 ──
# 控制面容器以 root 写 state/ 卷；这里检查宿主侧不可被普通用户读取。
printf '== 4/4 凭据文件权限\n'
state_dir="$(cd "$(dirname "$0")" && pwd)/state"
for path in "$state_dir/pki/ca" "$state_dir/users.yml" "$state_dir/sessions.yml" \
            "$state_dir/credentials.yml" "$state_dir/admin.conf" "$state_dir/audit.jsonl"; do
    [ -e "$path" ] || continue
    mode="$(stat -c '%a' "$path" 2>/dev/null || echo '?')"
    case "$mode" in
        600|700|750|755|640|644) ok "$(basename "$path") mode=$mode" ;;
        *) warn "$(basename "$path") mode=$mode（期望 600/700）" ;;
    esac
done
for key in "$state_dir"/pki/ca/*/ca.key; do
    [ -e "$key" ] || continue
    mode="$(stat -c '%a' "$key" 2>/dev/null || echo '?')"
    if [ "$mode" = "600" ]; then
        ok "$(basename "$(dirname "$key")")/ca.key mode=600"
    else
        fail "$(basename "$(dirname "$key")")/ca.key mode=$mode（私钥必须 600）"
    fi
done

printf '\n'
if [ "$failed" -ne 0 ]; then
    printf '宿主基线未通过：%d 项 FAIL、%d 项 warn（见上方）\n' "$failed" "$warned" >&2
    exit 1
fi
printf '宿主基线通过（%d 项 warn；默认只读，仅 --fix-4841 会修改 iptables）\n' "$warned"
