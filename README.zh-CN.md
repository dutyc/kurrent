# Kurrent (周流)

![Cloud Native](https://img.shields.io/badge/Cloud%20Native-True%20Cloud%20Native-18181b) [![NVMe-oF](https://img.shields.io/badge/Data%20Plane-NVMe--oF%20First-00D4FF)](https://dutyc.github.io/kurrent/guide/deployment.html) [![Stars](https://img.shields.io/github/stars/dutyc/kurrent)](https://github.com/dutyc/kurrent/stargazers) [![Release](https://img.shields.io/github/v/release/dutyc/kurrent)](https://github.com/dutyc/kurrent/releases) [![License](https://img.shields.io/github/license/dutyc/kurrent)](LICENSE) [![Docs](https://img.shields.io/badge/Docs-GitHub%20Pages-2563eb)](https://dutyc.github.io/kurrent/)

[中文版](./README.zh-CN.md) | [English](./README.md)

**Make bare metal flow.**
*周流六虚，上下无常。*

K8s 让应用成为云，Kurrent 让算力成为云。

Kurrent 是一套云原生无状态裸金属交付范式。它将“无状态”贯彻到物理算力层：计算节点（Device）不持有持久状态，身份（Worker）、系统与数据均由网络和控制面授予。调度单元不是应用，而是系统本身——系统归属于身份而非机器。设备接入网络即可启动，算力如电流般在裸金属节点间周流。详见[宣言](./about/zh/Manifesto.md)。

## NVMe-oF：主数据面

数据面以 NVMe-oF 为主：内核态 NVMe/TCP target 导出系统盘，单端口 `4421` 使用 TLS 1.3，固件段与内核段共用。DHHC-1 凭据按 Worker 跟盘、nvmet 后端建盘时自动签发，root-path 使用盘记录中的完整 NQN；iSCSI（stgt/lio）保留为兼容后端。

## 快速开始

数分钟内拉起控制面并接入存储节点，见文档站的[部署](https://dutyc.github.io/kurrent/guide/deployment.html)章节。`kurrent` CLI 随 [Releases](https://github.com/dutyc/kurrent/releases) 提供 Linux amd64/arm64 与 Windows amd64 预编译二进制。

## 文档

[文档站](https://dutyc.github.io/kurrent/)覆盖部署、系统盘定制、日常运维、故障排查与参考手册。[CLI](./cli/README.md)、[控制面 API](./api/control-plane-api.zh-CN.md) 与[架构](./about/zh/ARCHITECTURE.md)文档在仓库内维护。

## 交付流程

设备首启入池；在 WebUI 完成绑定 Worker、创建系统盘、设置默认盘后即可开机。一台 Worker 可挂多块系统盘并在菜单中切换。母盘与系统盘制备、首次启动见[系统盘定制](https://dutyc.github.io/kurrent/guide/system-disk.html)与[首次交付](https://dutyc.github.io/kurrent/guide/usage.html)。整机冷启动已在 **Debian 12/13、Ubuntu 24.04/26.04、Fedora 44、AlmaLinux 10.2 与 openSUSE Leap 16.0** 验证，详见[发行版矩阵](https://dutyc.github.io/kurrent/reference/distributions.html)，验证范围不构成发行版限制。

## 固件仓库

引导固件由配套仓库 **[Kurrent Firmware](https://github.com/dutyc/kurrent-firmware)** 构建：Kurrent 让算力无状态，固件仓库让引导固件无状态。构建与部署步骤见[固件构建与部署](https://dutyc.github.io/kurrent/firmware/build-deploy.html)。

## 路线图

方向是跨平台、跨架构的云原生元协议：同一套无状态语义覆盖裸机与虚拟化层。愿景见**[宣言](./about/zh/Manifesto.md)**，具体方向见 **[ROADMAP](./about/zh/ROADMAP.md)**（English: [about/en/ROADMAP.md](./about/en/ROADMAP.md)）。

## 社区与贡献

欢迎 Issue、Discussions 与 PR。AI 可以写语法，架构必须由人脑理解；提交前请阅读 [AI_POLICY.md](./about/zh/AI_POLICY.md)。

## 许可证

[Apache License 2.0](./LICENSE)

## Star History

<a href="https://www.star-history.com/?repos=dutyc%2Fkurrent&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=dutyc/kurrent&type=date&theme=dark&legend=top-left&sealed_token=zkQGmbPm0yH3EjnLTKc1DTe9hOaLnAeUdZlSlo92lycj2vyTy9VkyIW-uvH3P09ByCS5CiBI8QnhVbVNFyM211tGwSJ1yp7qE6ZsukdPCxJWkopIpVQepMXjrDwOAVENpL87Tr8qmmIYxQy6DawB8PaqrlfuVmGZPdnh9fPfJ8GtvnCIwkENEeVPSVp7" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=dutyc/kurrent&type=date&legend=top-left&sealed_token=zkQGmbPm0yH3EjnLTKc1DTe9hOaLnAeUdZlSlo92lycj2vyTy9VkyIW-uvH3P09ByCS5CiBI8QnhVbVNFyM211tGwSJ1yp7qE6ZsukdPCxJWkopIpVQepMXjrDwOAVENpL87Tr8qmmIYxQy6DawB8PaqrlfuVmGZPdnh9fPfJ8GtvnCIwkENEeVPSVp7" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=dutyc/kurrent&type=date&legend=top-left&sealed_token=zkQGmbPm0yH3EjnLTKc1DTe9hOaLnAeUdZlSlo92lycj2vyTy9VkyIW-uvH3P09ByCS5CiBI8QnhVbVNFyM211tGwSJ1yp7qE6ZsukdPCxJWkopIpVQepMXjrDwOAVENpL87Tr8qmmIYxQy6DawB8PaqrlfuVmGZPdnh9fPfJ8GtvnCIwkENEeVPSVp7" />
 </picture>
</a>
