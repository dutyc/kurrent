# Kurrent (周流)

![Cloud Native](https://img.shields.io/badge/Cloud%20Native-True%20Cloud%20Native-18181b) [![NVMe-oF](https://img.shields.io/badge/Data%20Plane-NVMe--oF%20First-00D4FF)](https://dutyc.github.io/kurrent/guide/deployment.html) [![Stars](https://img.shields.io/github/stars/dutyc/kurrent)](https://github.com/dutyc/kurrent/stargazers) [![Release](https://img.shields.io/github/v/release/dutyc/kurrent)](https://github.com/dutyc/kurrent/releases) [![License](https://img.shields.io/github/license/dutyc/kurrent)](LICENSE) [![Docs](https://img.shields.io/badge/Docs-GitHub%20Pages-2563eb)](https://dutyc.github.io/kurrent/)

[中文版](./README.zh-CN.md) | [English](./README.md)

**Make bare metal flow.**
*周流六虚，上下无常。*

K8s 让应用成为云。Kurrent 让算力成为云——*K8s orchestrates containers. Kurrent orchestrates compute.*

**Kurrent** 是一套云原生无状态裸金属交付范式。它将“无状态”贯彻到物理算力层本身：计算节点（Device）自身不持有任何持久状态，身份（Worker）、系统与数据均由网络和控制面外部授予。插上网线即活，算力脱离硬件束缚，如电流般在裸金属节点间自由周流。

阅读我们的宣言：**[about/zh/Manifesto.md](./about/zh/Manifesto.md)**——*我们对云原生的定义*（English: [about/en/Manifesto.md](./about/en/Manifesto.md)）。

## NVMe-oF：主数据面

Kurrent 的存储面 NVMe-oF 优先：存储节点以 `backend=nvmet`（缺省）加入集群，经**内核态 NVMe-oF target**（NVMe over TCP，4420 端口）导出母盘。单一 NQN 命名域（缺省 `nqn.2026-07.com.kurrent`）、DHHC-1 认证按 Worker 跟盘（**建系统盘时自动签发密钥**；存储节点宿主服务凭据 enroll 时派生）、引导链直连带密钥的 `nvme://` 根路径——iSCSI（stgt/lio）保留为兼容后端。

## 快速开始

数分钟内拉起控制面并接入存储节点——见[部署指南](https://dutyc.github.io/kurrent/guide/deployment.html)（英文：[en](https://dutyc.github.io/kurrent/en/guide/deployment.html)）。`kurrent` CLI 以预编译单二进制随 [Releases](https://github.com/dutyc/kurrent/releases) 发布（Linux amd64/arm64、Windows amd64），无需本地编译。

- **文档站（双语）**——https://dutyc.github.io/kurrent/
- **CLI 参考**——[cli/README.md](./cli/README.md)
- **控制面 API 参考**——[api/control-plane-api.zh-CN.md](./api/control-plane-api.zh-CN.md) / [api/control-plane-api.en.md](./api/control-plane-api.en.md)
- **架构**——[about/zh/ARCHITECTURE.md](./about/zh/ARCHITECTURE.md)（English: [about/en/ARCHITECTURE.md](./about/en/ARCHITECTURE.md)）

## 核心特性: 算力周流，即插即用

一条无状态裸金属交付流：新机器首启自动入设备池，WebUI 点几下即可绑定 Worker、分配系统盘与默认系统。一台 Worker 可挂载多块系统盘并随时在线切换。无盘冷启动（NVMe-oF）已在 **Debian 12/13、Ubuntu 24.04/26.04 与 Fedora 44** 上通过整机验收。

## 固件仓库

*The firmware engine for Kurrent. Make bare metal flow at the boot layer.*
*Kurrent 的固件引擎：在引导层让裸金属流动。*

引导链底层的固件由配套仓库 **[Kurrent Firmware](https://github.com/dutyc/kurrent-firmware)** 构建——与 Kurrent 同一理念的一体两面：Kurrent 让算力无状态，固件仓库让引导固件无状态。

## 路线图

跨平台、跨架构的云原生元协议：一套无状态语义，在裸机与虚拟化层自相似地嵌套——这一愿景写在**[宣言](./about/zh/Manifesto.md)**里，具体推进方向见 **[about/zh/ROADMAP.md](./about/zh/ROADMAP.md)**（English: [about/en/ROADMAP.md](./about/en/ROADMAP.md)）。

## 社区与贡献

欢迎 Star / Watch / Discussions / Pull Requests。本项目拥抱 AI 辅助开发，但有一条硬性要求：**AI 可以写语法，架构必须由人脑理解**。提交 PR 前请阅读 [AI_POLICY.md](./about/zh/AI_POLICY.md)。

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
