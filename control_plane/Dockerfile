FROM python:3.11-slim@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377

# apt 走阿里云源（国内网络环境更新更快）。bookworm 源在 deb822 格式的
# debian.sources（URIs 字段），老镜像才在 sources.list；只替换域名，
# 协议和路径不动，两种格式都覆盖。
RUN set -ex; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list; \
    fi
# 时区数据：日志时间戳跟随 TZ 环境变量（/etc/localtime 挂载失效时的兜底）
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt .
# 运行期不需要 pip/setuptools/wheel：它们内置的依赖同样构成漏洞面
RUN pip install --no-cache-dir \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        --trusted-host mirrors.aliyun.com \
        -r requirements.txt \
    && python -m pip uninstall -y pip setuptools wheel

COPY app ./app
COPY healthcheck.py /app/healthcheck.py

# 管理面只暴露 unix socket（与 nginx 共享卷），容器内不监听 TCP：
# 同网容器与 host 网络进程都无法直连，身份头只可能由 nginx 写入。
CMD ["sh", "-c", "mkdir -p /run/kurrent && exec uvicorn app.main:app --uds /run/kurrent/cp.sock"]

