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
# pip 走阿里云镜像加速（国内网络环境安装依赖更快）；运行期移除构建工具
RUN pip install --no-cache-dir \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        --trusted-host mirrors.aliyun.com \
        -r requirements.txt \
    && python -m pip uninstall -y pip setuptools wheel

COPY app ./app

# 前台跑 HTTPS 服务（mTLS），监听 8080：
# serving cert = 本组件 TLS 服务端证书；ssl-ca-certs = control CA（只信控制面客户端）；
# ssl-cert-reqs=2（CERT_REQUIRED）：控制面必须出示其 client cert 才能访问。
# 注：协议版本不限（默认 TLS1.2/1.3 皆可）——docker-proxy 对 TLS1.3 客户端证书握手
# 有兼容问题，由客户端（控制面 AgentClient）强制 TLS1.2 规避，见 agent_client.py
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--ssl-keyfile", "/etc/kurrent/pki/serving.key", \
     "--ssl-certfile", "/etc/kurrent/pki/serving.crt", \
     "--ssl-ca-certs", "/etc/kurrent/pki/ca-control.crt", \
     "--ssl-cert-reqs", "2"]