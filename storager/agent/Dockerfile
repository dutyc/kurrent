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
# serving cert = 本组件 TLS 服务端证书；control CA 只信控制面客户端（CERT_REQUIRED）；
# 应用层再从 peer cert 校 issuer/CN 与 control CRL（可热更新），见 app/security.py。
# 入口用 python -m app.serve：uvicorn CLI 无法注入自定义 SSLContext 与 peer cert hook。
CMD ["python", "-m", "app.serve"]