# 从母盘创建系统盘

本任务介绍如何把定制好的母盘部署到存储节点，并为 Worker 创建系统盘。

系统盘由母盘克隆生成。母盘在虚拟机或离线环境制作一次，放入存储节点磁盘目录；控制面按 Worker 克隆系统盘，并在建盘时签发该盘凭据。母盘只作为克隆源，不参与启动。

## 开始之前

- 控制面已完成部署，至少一个存储节点已接入。参见[部署](../setup/deployment.md)。
- 母盘已按[定制母盘](./system-disk.md)的要求制作并验收：4K 原生 GPT、GRUB 2.14、内核启用 NVMe 认证与 TLS、initramfs 能消费 NBFT/NBCT。母盘使用[已验证发行版](../reference/distributions.md)。
- 目标存储节点的声明已配置 `spec.agent.diskDir`，且该目录所在文件系统支持 reflink。
- 目标 Worker 已存在。

## 部署母盘到存储节点

1. 关闭母盘虚拟机，确保镜像已落盘。
2. 把整盘镜像（GPT、ESP 与 root 分区）复制到目标存储节点的 `spec.agent.diskDir`，文件名包含 `_tpl_`（大小写不敏感）：

   ```text
   /pool1/disk_dir/_tpl_debian12.img
   ```

3. 等待 Agent 后台扫描。母盘出现在 `/masters` 或 WebUI 母盘列表，`gpt` 应为 `4k`。

## 创建系统盘

1. 在 WebUI Worker 详情页创建系统盘，或调用创建接口：

   ```bash
   curl -s -X POST "$BASE_URL/workers/worker-01/luns/disk" \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "type": "master",
       "os": "ubuntu",
       "os_version": "22.04",
       "name": "_tpl_ubuntu_2204.img"
     }'
   ```

2. 控制面选择放置节点，生成盘级标识 `os_tag` 与盘 NQN；Agent 用 reflink 克隆母盘，并签发该盘的 DHHC-1 凭据。
3. 首块系统盘自动成为默认盘。多盘场景下，默认启动哪个盘由 `PUT /workers/{worker_id}/default-disk` 的 `disk`（`os_tag`）决定；第二块及以后的盘不会自动改写默认盘。

> **说明：** 母盘不得直接导出给设备启动。控制面也支持 `empty` 空白盘用于手工安装；生产路径以母盘克隆为主，空白盘安装完成后仍须通过相同的验收。

## 验收

- 绑定设备重启后，启动串口应看到固件段与内核段各一次认证成功，随后 rootfs 挂载并出现 `login:`。
- 不出现 `0x7f22208e`、`ret=401`、keep-alive 断链或 emergency mode。
- 抓包时数据面只应有 `4421` 的 TLS；盘内没有身份与密钥，克隆出的盘 NQN、`os_tag` 与凭据各不相同。

## 接下来

- 母盘定制的完整流程与脚本：[定制母盘](./system-disk.md)
- 数据面端口与就绪条件：[数据面与就绪](../concepts/storage/data-plane.md)
- 建盘后的凭据运维：[日常运维](./operations.md)
- 创建接口的完整参数与错误码：[系统盘 API](../reference/api/disks.md)
