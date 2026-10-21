# nvmet-host：NVMe-oF 配置面管理服务（容器形态）
#
# 内核 nvmet target 始终在宿主（modprobe nvmet nvmet-tcp + 挂载 configfs），
# 本镜像只承载 Python 管理进程：bind mount /sys/kernel/config 后，容器内 root
# 写 configfs 是普通文件操作，无需 privileged（与 LIO 的 targetclid/dbus 依赖无关）。
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
# pip 走阿里云镜像加速（与 storager/agent、control_plane 保持一致，国内网络环境安装依赖更快）
RUN pip install --no-cache-dir \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        --trusted-host mirrors.aliyun.com \
        -r requirements.txt

COPY app ./app

# 仅供文档提示；实际为 host 网络模式，服务直接监听宿主 4841
EXPOSE 4841

# 监听地址为模块常量（0.0.0.0:4841，host 网络即宿主栈），compose 不注入
CMD ["python", "-m", "app.main"]
