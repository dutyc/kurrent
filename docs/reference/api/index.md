---
title: "API 参考"
---

# 控制面 API 参考

**控制面**当前已实现的 HTTP 接口、请求参数与返回结构如下，并附 `curl` 示例。

> **说明：** 控制面的全部能力都以 REST API 为第一接口。Web 管理界面（WebUI）只是这套 API 的一个客户端，与第三方系统、自动化脚本地位相同。第三方集成应始终调用控制面 API，不绕过控制面直接操作 Agent 或数据面。

控制面是运行在控制面宿主上的常驻 HTTP 服务，负责：

- 设备台账与注册窗口：设备上报、挑战-应答、TPM 证明与绑定
- Worker 身份、系统盘与默认启动盘台账
- Agent 登记、探测、排空与存储编排调度
- 引导链投影（`/boot-vars`、`/boot-assets`）
- 管理面身份与 PKI：会话、token、人员与组件证书、CRL
- 声明配置状态、dnsmasq 派生配置与操作审计

它不直接配置存储后端，也不托管引导静态文件：

- `menu.ipxe` 与引导脚本存放在 `tftp/`，由 nginx 经 HTTPS 分发；TFTP 只提供首跳固件（`snponly.efi` / `ipxe.efi`）与 `boot.ipxe` 指针。
- 菜单的默认项、超时与系统标签由控制面经 `/boot-vars` 投影。
- 存储后端（nvmet configfs 或 iSCSI 的 `tgtadm`/`targetcli`）由节点 Agent 在宿主上配置。

## 调用约定

- 认证：管理请求依次尝试 mTLS 客户端证书、管理 token 与会话 Cookie，首个成功即止；匿名端点只有健康检查、公开 CRL 与引导链端点。
- 错误：校验失败、不存在与冲突返回 4xx，响应体为 `{"detail": ...}`；认证失败返回 401，主体类不符返回 403；引导端点超限返回 429 并带 `Retry-After`；审计写入或启动期派生失败返回 503。
- 数据：请求与响应默认 JSON；`/boot-vars` 默认输出 iPXE 脚本，`format=json` 时返回结构化变量；密钥、token 与 PSK 从不以明文返回。
- 集成：第三方系统调用控制面 API，不绕过控制面直接操作 Agent 或数据面。
- 授权：写请求需要 `admin` 组；会话写请求额外校验同源。

---

## 章节

| 页面 | 内容 |
| --- | --- |
| [认证与授权](./auth.md) | 凭据链、主体类、授权与审计 |
| [基础与状态](./foundation.md) | 入口、环境变量、状态文件与健康检查 |
| [引导与设备准入](./boot.md) | 引导链状态、启动变量、注册窗口与挑战-证明 |
| [设备池](./devices.md) | 设备台账、绑定、注销与证明基线 |
| [Worker](./workers.md) | Worker 身份、查询、换绑与批量操作 |
| [系统盘与凭据](./disks.md) | 建盘、默认盘、删盘与 NVMe-oF 凭据 |
| [Agent 与存储](./agents.md) | Agent 台账、母盘标签、LUN 与母盘清单 |
| [身份与 PKI](./identity.md) | token、一键加入、会话与证书吊销 |
| [运维与验证](./operations.md) | 操作日志与典型调用顺序 |

## 接口概览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/healthz` | 健康检查（见[基础与状态](./foundation.md)） |
| `GET` | `/boot-assets` | 引导链状态：UEFI 固件、脚本链与菜单是否可用（见[引导链](./boot.md)） |
| `GET` | `/whoami` | 当前请求身份（认证器链结果，见[认证与授权](./auth.md)） |
| `POST` | `/session` | 凭据换会话 Cookie（登录，登记在服务端，见[身份与 PKI](./identity.md)） |
| `DELETE` | `/session` | 登出（删除会话登记并清除 Cookie，见[身份与 PKI](./identity.md)） |
| `GET` | `/sessions` | 列出未过期会话（管理员，见[身份与 PKI](./identity.md)） |
| `DELETE` | `/sessions/{session_id}` | 逐个吊销会话（管理员，见[身份与 PKI](./identity.md)） |
| `DELETE` | `/sessions` | 全部下线：清空登记并递增 epoch（管理员，见[身份与 PKI](./identity.md)） |
| `GET` | `/boot-vars` | iPXE 启动变量动态注入，不鉴权（见[引导与设备准入](./boot.md)） |
| `GET` | `/devices/report` | iPXE 设备信息上报（不鉴权，11 字段指纹 + TPM AK，见[设备池](./devices.md)） |
| `GET` | `/devices/challenge` | 一次性 nonce 签发（不鉴权，挑战-响应，见[引导链](./boot.md)） |
| `POST` | `/devices/{mac}/approve-baseline` | 批准设备最近一次上报的 PCR 基线（管理员，见[设备池](./devices.md)） |
| `GET` | `/devices` | 设备池列表（state 过滤，见[设备池](./devices.md)） |
| `GET` | `/devices/{mac}` | 单设备详情（见[设备池](./devices.md)） |
| `POST` | `/devices` | 手动注册设备入池（见[设备池](./devices.md)） |
| `POST` | `/devices/import` | 批量导入设备清单（见[设备池](./devices.md)） |
| `POST` | `/devices/{mac}/bind` | 绑定设备到 Worker（`force` 原子换绑，见[设备池](./devices.md)） |
| `DELETE` | `/devices/{mac}/bind` | 解绑设备并回池（见[设备池](./devices.md)） |
| `POST` | `/devices/bind/batch/preview` | 批量绑定预览：配对表与冲突分类（见[设备池](./devices.md)） |
| `POST` | `/devices/bind/batch` | 批量绑定执行（逐项独立，见[设备池](./devices.md)） |
| `DELETE` | `/devices/{mac}` | 注销设备（吊销，见[设备池](./devices.md)） |
| `DELETE` | `/devices/revoked` | 清除全部已吊销设备（可选 `dry_run` 预览，见[设备池](./devices.md)） |
| `GET` | `/settings/registration-window` | 查询注册窗口状态（见[引导链](./boot.md)） |
| `POST` | `/settings/registration-window` | 开启注册窗口（TTL/额度上限来自声明 `spec.registration`，见[引导链](./boot.md)） |
| `DELETE` | `/settings/registration-window` | 提前关闭注册窗口（见[引导链](./boot.md)） |
| `GET` | `/settings/enforcement` | 查询 TPM 证明强制开关（见[引导链](./boot.md)） |
| `PUT` | `/settings/enforcement` | 切换 TPM 证明强制开关（见[引导链](./boot.md)） |
| `GET` | `/config` | 运行态声明配置与派生件一致性（见[基础与状态](./foundation.md)） |
| `GET` | `/agents` | 查询 Agent 列表与能力（见[Agent 与存储](./agents.md)） |
| `POST` | `/agents` | 注册新 Agent（写入 agents.yml，重复 id 返回 409，见[Agent 与存储](./agents.md)） |
| `POST` | `/agents/probe` | 探测 Agent 并自动推导注册参数（预览，不写文件，见[Agent 与存储](./agents.md)） |
| `PUT` | `/agents/{agent_id}` | 更新 Agent 配置（id 不可改，见[Agent 与存储](./agents.md)） |
| `POST` | `/pki/tokens` | 签发 bootstrap token（绑 agent_id、默认一次性、60 分钟）或管理 token（scope/TTL，见[身份与 PKI](./identity.md)） |
| `GET` | `/pki/tokens` | 列出未过期 token 台账（无明文/hash，见[身份与 PKI](./identity.md)） |
| `DELETE` | `/pki/tokens/{token_id}` | 吊销 token：删条目即失效（见[身份与 PKI](./identity.md)） |
| `POST` | `/pki/users/{cn}` | 按 CSR 为已登记用户签发证书（私钥不上送，见[身份与 PKI](./identity.md)） |
| `GET` | `/pki/users` | 列出 users CA 签发的证书（含已吊销/已过期，见[身份与 PKI](./identity.md)） |
| `DELETE` | `/pki/users/{cn}` | 吊销证书（可选 `serial` 精确到一张）：重签 CRL + reload nginx + 踢会话（见[身份与 PKI](./identity.md)） |
| `GET` | `/pki/components` | 列出 nodes 域组件证书（含已吊销/已过期，见[身份与 PKI](./identity.md)） |
| `DELETE` | `/pki/components/{agent_id}` | 吊销组件证书（可选 `serial`）：写 nodes CRL + reload nginx + 删 bootstrap token（见[身份与 PKI](./identity.md)） |
| `GET` | `/pki/control` | 列出 control 域证书（控制面客户端，含已吊销/已过期，见[身份与 PKI](./identity.md)） |
| `DELETE` | `/pki/control/{cn}` | 吊销 control 域证书（可选 `serial`）：写 control CRL（见[身份与 PKI](./identity.md)） |
| `GET` | `/enroll/crl` | 组件取 nodes + control CRL（组件 mTLS，见[身份与 PKI](./identity.md)） |
| `POST` | `/enroll` | 组件首次登记：bootstrap token 换取证书与信任锚（见[身份与 PKI](./identity.md)） |
| `POST` | `/enroll/renew` | 组件证书续签（仅 `component` 主体，见[身份与 PKI](./identity.md)） |
| `GET` | `/crl/nodes.crl` | 公开只读的 nodes CRL（无认证、仅 GET/HEAD，供被吊销组件启动自检，见[身份与 PKI](./identity.md)） |
| `DELETE` | `/agents/{agent_id}` | 删除 Agent 台账（含母盘标签清理，见[Agent 与存储](./agents.md)） |
| `GET` | `/agents/{agent_id}/luns` | 列出指定 Agent 上的 target/LUN（NVMe-oF 主路径与 iSCSI 兼容后端，见[Agent 与存储](./agents.md)） |
| `GET` | `/masters` | 聚合列出全部存储节点上的母盘清单（合并登记标签，供克隆选盘，见[Agent 与存储](./agents.md)） |
| `PUT` | `/agents/{agent_id}/masters/{master_name}/tag` | 登记母盘标签（os / os_version 备注，见[Agent 与存储](./agents.md)） |
| `DELETE` | `/agents/{agent_id}/masters/{master_name}/tag` | 清除母盘标签（见[Agent 与存储](./agents.md)） |
| `POST` | `/agents/{agent_id}/luns/disk` | 在指定 Agent 上创建磁盘 LUN（母盘克隆/空白盘，见[Agent 与存储](./agents.md)） |
| `POST` | `/agents/{agent_id}/luns/cd` | 在指定 Agent 上创建 CD（ISO 虚拟光驱）LUN（见[Agent 与存储](./agents.md)） |
| `DELETE` | `/agents/{agent_id}/luns` | 删除指定 Agent 上的 LUN/target（见[Agent 与存储](./agents.md)） |
| `POST` | `/agents/{agent_id}/luns/scan` | 触发指定 Agent 扫描镜像目录重建 target（见[Agent 与存储](./agents.md)） |
| `POST` | `/workers` | 注册 Worker 身份（hostname 绑定；`mac` 可选，传了直接绑定设备，见[Worker](./workers.md)） |
| `POST` | `/workers/batch` | 批量创建 Worker（数量 + 命名规则，逐项独立，`macs` 可选直接绑定，见[Worker](./workers.md)） |
| `POST` | `/workers/{worker_id}/luns/disk` | 给指定 Worker 创建系统盘 LUN（见[系统盘与凭据](./disks.md)） |
| `POST` | `/workers/luns/disk/batch` | 批量给多个 Worker 创建系统盘（逐项点名节点，或 `workers` 交给控制面放置；`dry_run` 出计划，见[系统盘与凭据](./disks.md)） |
| `DELETE` | `/workers/{worker_id}/luns/disk/{os_tag}` | 删除 Worker 单个系统盘（保留/删除 .img 文件，见[系统盘与凭据](./disks.md)） |
| `PUT` | `/workers/{worker_id}/default-disk` | 设置 Worker 默认启动配置（默认盘 os_tag / 菜单项 / 超时，见[系统盘与凭据](./disks.md)） |
| `PUT` | `/workers/{worker_id}/mac` | 修改 Worker 的 MAC 绑定（设备台账权威，dnsmasq 投影缺失补写；审计旧/新 MAC，见[Worker](./workers.md)） |
| `GET` | `/workers` | 列出 Worker（见[Worker](./workers.md)） |
| `GET` | `/workers/{worker_id}` | 查询单个 Worker（见[Worker](./workers.md)） |
| `GET` | `/workers/{worker_id}/status` | 查询 Worker 台账与实时状态（见[Worker](./workers.md)） |
| `DELETE` | `/workers/{worker_id}` | 删除 Worker（见[Worker](./workers.md)） |
| `POST` | `/workers/delete/batch` | 批量删除 Worker（逐项独立，成功/失败汇总，见[Worker](./workers.md)） |
| `PUT` | `/workers/{worker_id}/credential` | 设置/轮换 NVMe-oF 认证密钥（DHHC-1 自检；nvmet 建盘已自动签发，见[系统盘](./disks.md)） |
| `DELETE` | `/workers/{worker_id}/credential` | 删除 NVMe-oF 认证密钥（吊销认证，见[系统盘](./disks.md)） |
| `GET` | `/workers/{worker_id}/credential` | 查询凭据元数据（不返回明文，见[系统盘](./disks.md)） |
| `GET` | `/operations` | 读取操作日志（见[运维与验证](./operations.md)） |

---

