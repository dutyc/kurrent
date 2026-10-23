#!/bin/sh
# CRL 变化或控制面请求 reload 时，nginx 自己重载配置。
# 控制面不再经 docker.sock 操作本容器：它把递增计数器写进 kurrent-run 卷的
# nginx.reload，这里执行 `nginx -s reload` 成功后把同一计数写回 nginx.ack，
# 控制面轮询 ack 确认吊销已生效（超时 → 500）。
set -u

CRL=/etc/nginx/pki/clients.crl
TRIGGER=/run/kurrent/nginx.reload
ACK=/run/kurrent/nginx.ack

crl_sig() {
  md5sum "$CRL" 2>/dev/null | cut -d' ' -f1
}

last_crl="$(crl_sig)"
# 从 0 起算：若启动前控制面已请求过 reload（计数器 >0），第一次循环即补一次
last_sentinel=0

while sleep 1; do
  cur_crl="$(crl_sig)"
  cur_sentinel="$(cat "$TRIGGER" 2>/dev/null || echo 0)"

  if [ "$cur_crl" = "$last_crl" ] && [ "$cur_sentinel" = "$last_sentinel" ]; then
    continue
  fi

  if nginx -t >/dev/null 2>&1 && nginx -s reload; then
    last_crl="$cur_crl"
    last_sentinel="$cur_sentinel"
    echo "$cur_sentinel" > "$ACK" 2>/dev/null || true
    echo "kurrent-nginx: reloaded (sentinel=$cur_sentinel)" >&2
  else
    # 不更新 last_*：下一轮重试；控制面会因 ack 超时返回 500
    echo "kurrent-nginx: reload failed (sentinel=$cur_sentinel); will retry" >&2
  fi
done
