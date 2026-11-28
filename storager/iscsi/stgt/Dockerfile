FROM debian:bookworm-slim@sha256:e67eeead736ff7bbe258ef088bff683635c561b1fcfefad0806c1df4ab49ce0e

# apt 走阿里云源
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

# tgt = stgt（tgtd + tgtadm）；显式装 libgcc-s1，确保 pthread_cancel 栈展开有库可用
RUN apt-get update && apt-get install -y --no-install-recommends tgt libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*

# tgtd 前台运行（容器里不用 systemd）
CMD ["tgtd", "-f"]