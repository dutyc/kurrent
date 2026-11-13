FROM nginx:1.31-alpine@sha256:a3554c0d0b48177b1413c26e01f695334428020d9df01243fbae3e11cfd17417

# Copy nginx config
COPY nginx/nginx.conf /etc/nginx/nginx.conf

# Identity assertion snippets: every proxying location includes exactly one.
COPY nginx/snippets/ /etc/nginx/snippets/

# CRL 变化 / 控制面 reload 请求的容器内 watcher（不再经 docker.sock 跨容器操作）
COPY nginx/kurrent-watch.sh /etc/nginx/kurrent-watch.sh

# 等待控制面启动时自动生成的 TOFU 证书与 CA 信任包/CRL 就绪后再起 nginx：
# compose 不保证容器启动顺序，缺文件时 nginx 会立即退出；等待循环保证首次 up 也能成功。
# watcher 在后台运行，nginx 保持 PID 1（`nginx -s reload` 依赖同一容器内的 pid 文件）。
CMD ["sh", "-c", "while [ ! -f /etc/nginx/certs/server.crt ] || [ ! -f /etc/nginx/certs/boot-server.crt ] || [ ! -f /etc/nginx/pki/ca-bundle.pem ] || [ ! -f /etc/nginx/pki/clients.crl ]; do sleep 1; done; sh /etc/nginx/kurrent-watch.sh & exec /docker-entrypoint.sh nginx -g 'daemon off;'"]
