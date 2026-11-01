# 参与贡献

Kurrent 由少量维护者与大量 AI 辅助共同开发。贡献的评价标准不在代码量，而在于是否理解系统的承重结构。

## 仓库结构

- **`control_plane/`**：FastAPI 控制面：声明校验、路由、PKI、台账、审计与后台任务。
- **`storager/agent/`**：存储节点宿主 Agent：证书引导、母盘扫描、凭据下发与后端编排。
- **`storager/nvmeof/nvmet-host/`**：NVMe/TCP 宿主管理服务：configfs、子系统、端口与 DH-HMAC-CHAP。
- **`cli/`**：Go 编写的单二进制 CLI。
- **`webui/`**：React 管理界面与 nginx 部署。
- **`tftp/`、`dnsmasq/`**：引导脚本、固件首跳与 DHCP 配置。
- **`docs/`**：双语文档站（VitePress）。
- **`tests/`**：Python 测试、部署探针与测试工具。
- **`api/`**：控制面 API 双语文档。
- **`about/`**：架构、路线图与项目说明。
- **`thirdparty/`**：固定版本的上游参考实现与补丁。

固件位于独立仓库 `dutyc/kurrent-firmware`，只维护 iPXE 差异补丁与构建资产。

## 构建与测试

| 目标 | 命令 |
| --- | --- |
| 文档 | `npm ci && npm run docs:build` |
| 文档（Pages 形态） | `VITEPRESS_BASE=/kurrent/ VITEPRESS_NO_CLEAN=1 npm run docs:build` |
| CLI | `cd cli && go vet ./... && go test ./... && go build -o kurrent .` |
| 控制面与节点 | `.venv-linux/bin/python -m pytest tests/ -q` |
| WebUI | `cd webui/app && npm ci && npm test && npm run build` |
| 部署探针 | 运行中的部署上执行 `bash tests/deploy/probe_edge.sh` |

Python 测试必须限定在 `tests/` 目录；直接在仓库根运行会误收集 `thirdparty/` 中的上游测试。

涉及引导链、凭据或数据面的改动，除单元测试外还需要在真实设备或 QEMU 上完成一次端到端验证，并在 PR 中给出证据。

## 文档规则

- 中文为默认站，`/en/` 为同构英文站；新增或修改页面必须成对提交，页面集合保持一致。
- 站内只给 API、CLI 与固件契约的概览和权威链接，完整定义保留在各自仓库，避免第二份参考漂移。
- 不写内部风险编号、信任等级代号或测试数量；用描述性名称表达事实。
- 不把未实现的能力写成已实现；路线图与“当前边界”必须与代码一致。
- 站内链接使用相对 `.md` 路径；首页手动链接需兼容 GitHub Pages 的 `.html` 产物。
- 使用正式工程语域，不写宣言式表达、命令句或占位符。

## AI 辅助

AI 在本项目中用于生成实现、测试与重构。分工原则：

- **语法与实现可以交给 AI**；架构判断、边界划分与状态所有权必须由人负责。
- 提交 PR 的人必须能解释修改涉及的控制面/数据面边界、引导时序、状态与派生物的归属。
- 实现层的瑕疵可以接受并迭代；破坏承重结构的改动不接受。
- 若只能说明“改了什么”而无法说明“为什么这样改”，应先提交 Issue 讨论；其价值高于直接提交 PR。

## Issue 与 PR

Issue 适合：

- 架构与边界问题；
- 需求与使用场景；
- 可复现的缺陷；
- 文档错误。

PR 适合：

- 边界清晰、可验证的改动；
- 已达成方向共识的实现；
- 测试与文档同步更新。

PR 描述应包含：

1. 改动要解决的问题；
2. 涉及的边界与状态所有权；
3. 验证方式与结果；
4. 对现有部署或升级的影响。

密钥、token、私钥、PSK 与未脱敏审计日志不得贴入 Issue 或 PR。安全敏感问题应先行私下联系维护者，再确定公开范围。

## 提交约定

- 提交信息使用 Conventional Commits，标题与正文用英文；
- 按主题拆分提交，不把无关改动混在一起；
- 文档、测试与实现同主题提交；
- 不提交运行时状态、凭据、构建产物或本地配置。

## 许可证

项目使用 Apache License 2.0。提交贡献即表示同意以同一许可证发布。

## 相关页面

- [项目概述](../guide/overview.md)
- [架构总览](../architecture/index.md)
- [路线图](./roadmap.md)
- [配置参考](../reference/configuration.md)
