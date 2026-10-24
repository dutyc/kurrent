# WebUI 设计文档

## 设计理念

WebUI 是 Control Plane 的纯前端界面，通过 Control Plane HTTP API 实现对所有管理操作的可视化。它不直接调用 Agent、dnsmasq 或其他底层组件——一切经由 Control Plane 代理。

设计遵循 **极简黑白工业风**：克制色彩、细边框、等宽字体、零装饰。目标是让运维人员一眼看懂集群状态，而非被视觉噪音干扰。

---

## 色彩系统

```
--black:       #000000    主文字
--gray-900:    #1a1a1a    次标题
--gray-800:    #2a2a2a    
--gray-700:    #3a3a3a    
--gray-600:    #555555    辅助文字
--gray-500:    #777777    
--gray-400:    #999999    占位符
--gray-300:    #bbbbbb    禁用的边框
--gray-200:    #dddddd    
--gray-100:    #ebebeb    
--gray-50:     #f4f4f4    
--gray-25:     #fafafa    卡片底色
--white:       #ffffff    页面底色
--border:      #e5e5e5    卡片/输入框边框
--border-light:#f0f0f0    微妙的分割线
```

**交互规则**：hover 时边框从 `--border` 翻转为 `--black`；按钮 hover 时背景填充 `--black`、文字变为 `--white`。不使用任何彩色。

---

## 排版

| 用途 | 字体 | 字号 |
|---|---|---|
| 全局正文 | 等宽（`ui-monospace` / `SFMono` / `Menlo` / `Consolas`） | 14px |
| 导航栏品牌标识 | 等宽 | 13px / 600 weight / 2px letter-spacing |
| 页面标题 | 等宽 | 继承 / 通过 CSS 放大 |
| Badge 标签 | 等宽 | 11px |
| 代码块 | 等宽 | 13px |
| 按钮 | 等宽 | 13px |

所有圆角为 0（`--radius: 0`），过渡动画统一 150ms ease。

---

## 组件库

所有组件为自研纯 CSS 实现，**零第三方 UI 库依赖**。

### Card
- 底色 `--gray-25`，边框 1px `--border`
- 可选 `hover` 模式：hover 时边框变 `--black`
- 支持 onClick，可作可点击卡片

### Button
- `variant`: `primary`（黑底白字）、`secondary`（边框高亮）、`ghost`（透明底灰字）
- 统一等宽字体、1px 黑边框、hover 反转

### Input
- 等宽字体、透明底、1px `--border` 边框
- focus 时边框变黑
- 支持 label / placeholder / name / required

### Select
- 继承 Input 风格
- options 为 `{ value, label }[]` 数组

### Badge
- 用于状态标签（health、state、status）
- 等宽 11px、内边距紧凑

### Divider
- 水平分割线，带可选文字（children）
- 1px `--border` 线

### EmptyState
- 空数据占位提示
- 居中展示，灰色文字

### CodeBlock
- 深色底（`--gray-900`）、等宽字体
- 代码高亮或 IPXE 启动脚本展示

### ConfirmAction
- 内联确认对话框
- 用于危险操作（删除 Worker 等）
- 支持额外 checkbox 选项（如"同时删除 .img"）

### Modal
- 全局弹窗容器（遮罩 + 居中卡片）
- 承载次级表单：创建 Worker、Worker 详情的 MAC 换绑、Agent 编辑、Agent 母盘操作
- 与页面内联展开的交互分工：主流程就地展开（如 Workers 的建盘计划），次级/危险操作走弹窗

### Toast（结果通知）
- **操作结果不进页面流**：成功/失败用固定右下角的通知条（`position: fixed`，不进列表流、不挤布局），
  页面里那行"成功 2"会被当成内容的一部分读（用户 2026-10-05 指出）
- 成功/部分成功**自动消失**（6s，无人需要处理）；**失败常驻**，须手动关闭——错误不能被时间吞掉
- 变体只用单色语言：`ok` 细灰边；`error` 深色描边 + 左侧重墨条（沿用 `badge-error` 的反色强调）
- 带明细时逐条列（失败原因 / 跳过原因），超过若干条内部滚动；成功项不复述（列表本身就是明细）
- 同一页可堆叠多条（建盘 / 删除 / 创建各一条）；`role=status`（非错误）/ `role=alert`（错误）
- 自动消失的计时器不得随重渲染重置（`onClose` 用 ref 持有，effect 只依赖 tone/duration）

---

## 页面结构

### 路由（HashRouter）

认证门（`App.jsx` 的 `Gate` + `auth.jsx`）：启动探测 `GET /whoami` → `probing` 显示等待页、
`unauthed` 显示登录页、`authed` 才渲染下列路由。

```
/                     Dashboard
/workers              Worker 列表 + 勾选浮出操作条（创建 Worker 走弹窗）
/workers/:id          Worker 详情（Tab：Identity / Disks / Boot / CD / NVMe-oF 凭据（有盘时）/ Status）
/devices              设备池（注册 / 导入 / 绑定向导 / 解绑 / 注册窗口）
/boot                 引导链状态板（UEFI 固件 / 引导服务 / 数据面 / Worker / 上报）
/agents               Agent 监控（自适应网格卡片）
/agents/:id           Agent 详情（Tab：Targets / Masters）
/identity             身份与访问（任务入口 + 用户 / 存储节点 / 控制面 / 自动化四个页签）
/operations           操作审计日志（增量加载）
*                     未匹配路由回退 `/`（旧书签、已删除页面不留空白树）
```

启动前清理畸形百分号编码的 hash；路由内容由 `ErrorBoundary` 包住：单页渲染异常只替换内容区，
侧边栏保持可点，路由切换即重挂载恢复。

### 布局（Layout）

可折叠侧边栏 + 主内容区：

```
┌────────────┬──────────────────────────┐
│ 品牌    ‹  │                          │
│ ──────     │                          │
│ 概览       │                          │
│ Workers    │        <Outlet />        │
│ Devices    │                          │
│ 引导链     │                          │
│ Agents     │                          │
│ 身份       │                          │
│ Operations │                          │
│ ──────     │                          │
│ 登出 / 中EN│                          │
└────────────┴──────────────────────────┘
```

侧边栏（`<aside class="sidebar">`，可折叠）：品牌标识 + 折叠按钮（`‹` / `›`）+ 7 个导航项
（Dashboard / Workers / Devices / Boot chain / Agents / Identity / Operations，各带内联 SVG 图标）+ 底部登出按钮
与 中/EN 语言切换。`#/credentials` 与 `#/certificates` 两个旧入口重定向到 `#/identity`。

### Dashboard

- 统计卡片区：Workers 数量 / Devices 数量 / Agents 健康比例（`健康/总数`）
- 最近操作：最新 10 条操作日志摘要；点击"查看全部"跳转 /operations
- 数据并行加载：`Promise.all([getWorkers, getAgents, getOperations])`

### Workers

交互无「模式」：勾选列常驻，选中即浮出操作条（黑条），操作条上就地展开计划与参数。

- 列表列：Worker / 主机 / 绑定设备 / 系统盘 / 就绪度，支持文本筛选（worker_id、主机名、
  绑定 MAC、系统盘系统名）；已挂盘显示 `系统[ 版本] → 存储节点`
- 行交互：**没有选中时点整行 = 进详情；一旦有选中，点整行 = 勾选/取消**（不用瞄准方块）。
  左侧方块单独勾选、Shift+点击标定区间终点（中间自动勾选）；表头全选/清空的作用域 =
  当前筛选后的可见行（所见即所选），半选态用 `indeterminate`
- 创建…（弹窗）：数量 + 名称——数量 = 1 时该名称即完整 Worker ID，> 1 时是名称前缀
  （`worker-01…`）；MAC 列表可选（每行一个，行数须等于数量；填了逐项校验设备池并直接绑定）。
  Windows ISO 收进「高级选项」（折叠）；批量不支持该字段，故数量 > 1 时整块隐藏
- 操作条（`已选 N 台`）：`创建系统盘` / `删除` / `清空`
  - **创建系统盘 = 就地展开放置计划**：同一行变成参数（磁盘类型 / 操作系统 / 母盘或大小 / 高级），
    右侧是结果摘要 + `确认创建` + `取消`。不弹窗、不在别处重列这些行（页面已是唯一视图）
  - 参数缺档时摘要位直说缺什么（如「未发现可用母盘」），不假装「计算中」
  - 「高级」面板 = 对系统决策的约束 + 低频盘参数：限定可用节点（按当前母盘本地存在性标注
    「缺该母盘」）、系统版本、盘备注、已排除节点及原因
- **位置由控制面算（放置是策略，不是逐行输入）**：候选 = 磁盘角色 ∧ 启用（非排空）∧
  有 API 地址 ∧ 数据面就绪 ∧ 在限定节点范围内 ∧（克隆时）母盘在该节点本地存在；
  逐个 Worker 取「现有盘数最少」的候选均摊，同负载按登记顺序。逐行下拉是**显式覆盖**
  （pin，标「手动」，可 `×` 恢复自动），不要求落在限定范围内但必须自身可用
- **预览即执行**：同一次请求先以 `dry_run` 拿计划（只读探测，不建盘），确认后原样执行；
  已有同 (系统, 版本) 系统盘的 Worker 显示「已有系统盘 · 跳过」且不占确认名额
- **建盘即签发**：盘落在 nvmet 后端节点时，控制面自动为尚无密钥的 Worker 生成并推送 DHHC-1
  （单盘/批量同一语义，见后端 7.1 步骤 7）；前端无需凭据输入项，批量也不多一步
- 结果：**成功/失败走通知条**（右下角，不进页面流）——成功自动消失、失败常驻并逐条列失败原因；
  创建 Worker 也一样（单条也发通知，1 台与 N 台反馈一致）
- 失败后**保留弹窗里的表单值**（改掉个别 MAC 直接重提，已建成的按幂等跳过），全部成功才清空
- 删除：ConfirmAction 二次确认（不可逆 → 二次确认），弹窗可附带「同时删除 .img」
  「忽略已不存在的 Target」
- Esc 分层退出：页面介绍贴层 → 创建弹窗 → 退出计划（清空选中）
- **节点列表与母盘清单独立取数**：`/masters` 是跨节点聚合端点，任一节点不可达即整批 502
  （body 带逐节点 error）——若与节点列表耦合成 `Promise.all`，连「建空白盘」（本不需要母盘）
  也会拿不到节点。母盘清单失败只降级下拉，并在母盘类型下显式提示原因（不把「取不到」
  伪装成「没有母盘」）；放置失败的 503 同样携带逐节点排除原因，在「高级」里展示
- 列表下方单行行为提示（灰字）随状态切换：无选中 = 点行进详情 / 有选中 = 点行即勾选 /
  计划中 = 位置由系统定、可改手动

### Worker Detail

Tab 切换：Identity / Disks / Boot / CD（条件展示）/ NVMe-oF Credential（条件展示）/ Status。

- Identity：Worker ID、Hostname、MAC（支持就地换绑编辑）、OS、Arch、State
- Disks：Agent、盘标识、Filename、Backing、Source
- Boot：默认启动系统与菜单默认项设置、`/boot-vars` 投影预览
- CD（条件展示）：Agent、盘标识、ISO、Backing
- NVMe-oF Credential（条件展示：该 Worker 有 NVMe-oF 盘时出现，即盘记录带 NQN——与后端推送目标同口径，
  否则整个页签隐藏）：状态徽章 + secret_hash 前缀 + 逐 Agent 推送状态。**密钥通常在 nvmet 建盘时已由
  控制面自动签发**（故这里多数情况下是「已设置」）；按钮语义 = 轮换：「生成并设置密钥」在浏览器就地生成
  DHHC-1（明文不落浏览器存储，只随请求发出）、「吊销密钥」二次确认；推送失败/待同步由控制面对账自动收敛
- Status：实时探测（dnsmasq 绑定、Disk Target 存在性、CD Target 存在性）
- 删除操作：内联二次确认（ConfirmAction），可选同时删除 .img 文件和忽略已不存在的 Target

### Devices（设备池）

- 设备台账列表：MAC、状态（`pooled` / `bound` / `revoked`）过滤、绑定 Worker、指纹摘要
- 工具栏：`+ 注册设备`（手工登记 MAC 与指纹字段）、`导入清单`（批量）、`绑定向导`、`解绑模式`
- 绑定向导：图形化配对或粘贴清单两种模式，先预览（分配）再执行；支持导出
- 注册窗口：开关 + TTL 选择 + 放行台数（留空 = 不限；决定未知 MAC 能否在上报时入池）；开启中显示剩余时间与「已放行 n/N」，额度用尽后 Badge 转为「额度已满」（密钥认领与手动登记不占额度）

### Boot chain（引导链）

状态板，不是配置查看器：页头给整体结论（正常 / 降级 / 阻塞 / 无法判定）与检查时间，下面固定五项检查，
只有异常项就地展开原因与处理位置。

- **UEFI 固件**：`dhcp-boot=tag:efi64` 声明的固件是否就位，附版本串与六项能力
  （`HTTPS` / `efi_nvs` / `keygen` / `pubkey` / `sign` / `TOFU`）计数；缺任一项为降级，
  文件缺失 / 为空 / 路径越界为阻塞（stock iPXE 会在这里被拦住）
- **引导服务**：`boot.ipxe → boot.ipxe.cfg → menu.ipxe` 是否齐全、菜单是否可解析；再对脚本做
  浏览器 HEAD 探测，确认它们确实能经 HTTPS 取回，而不只是落在盘上
- **数据面**：启用且 `backend=nvmet` 的节点中，端口 `ready` 且健康为 ok 的比例，附 TLS 版本与端口
- **Worker 就绪**：`readiness=ready`（已绑定设备且有系统盘）的比例
- **最近上报**：设备台账中最新的 `last_seen`，附 MAC、绑定 Worker 与相对时间；只作信息，不判过期
- **重新检查**：页头按钮重新取四份台账并重做 HEAD 探测
- 不展开菜单项、不做目录浏览；工具菜单（Memtest/WinPE）与 iSCSI 安装器不属于 NVMe-oF 主路径
- 数据来源：`GET /api/cp/boot-assets`（固件 / 脚本 / 菜单）＋ `/agents`、`/workers`、`/devices`；
  控制面只读挂载 `./tftp`

### Agents

- 自适应网格布局（`grid-template-columns: repeat(auto-fill, minmax(320px, 1fr))`）
- 每张卡片展示：
  - Agent ID + 健康状态 Badge（ok / degraded / error 三态；degraded = 节点在线但数据面不可用——
    宿主服务不可达或 configfs 未就绪，徽章 hover 显示原因）
  - 后端类型（stgt / lio / nvmet）
  - 文件系统、API 地址（base_url）
  - BASE NQN（盘标识命名空间）
  - 磁盘角色 / 光驱支持（是/否）
  - 克隆方式 / 新建磁盘方式（中文翻译映射）
  - 标签（tags）、备注（remark）
- 工具栏：`+ 加入节点`（节点接入入口）、`在线探测` 开关（开启时实时访问 Agent /healthz）、`页面介绍`
- 卡片上的 `编辑` 弹窗可改备注 / 角色 / 启用（排空：停用后不再接新增盘）等台账字段；
  自动登记节点（tags 含 `auto`）的 API 地址由节点声明管理，编辑弹窗中只读并附灰字提示
- 点击卡片进入 `/agents/:id`（Agent 详情：Targets / Masters 两个 Tab）

### Identity（身份与访问）

按「能做什么」组织，而不是按 PKI 结构或接口铺开：

- 页头三个任务入口：**接入存储节点**（生成入网令牌与 join 命令）、**生成管理令牌**
  （脚本与 CI 调用管理 API）、**收回访问**（滚动到台账并聚焦页签）
- 状态行：可登录账户数、可管理节点数（仅统计已登记节点）、可用令牌数、已吊销证书数
- 四个页签按访问主体划分：
  - **用户**（users CA）：按 CN 聚合，展示权限组（`GET /pki/users` 的 `groups`，组来自
    `users.yml`）、访问范围（admin = 全部资源，其余只读）、到期、状态；当前请求自身使用的
    证书标记为「当前证书」且不可吊销。下方为用户会话（`GET /sessions` 中 `cert_fp` 非空），
    逐行吊销
  - **存储节点**（nodes CA + 节点令牌）：证书按 `agent_id` 聚合 agent 与 nvmet-host 各自的
    client / serving 当前证书（按 CN + kind 各取最新，共 2~4 张）；状态 = 有效 / 部分可用 /
    已吊销 / 已过期，部分可用时给出已吊销张数与原因；历史吊销证书只计入状态行总数。证书
    对应节点未登记时标注「未登记」。下方为节点令牌（bootstrap / 派生），一次性已用尽的令牌
    不再列出，逐行吊销
  - **控制面**（control CA）：只读，吊销仅经 CLI/API
  - **自动化**：管理 token 台账（用途、权限、到期、最近使用）与令牌会话（`cert_fp` 为空），
    均可逐行吊销
- 节点名称显示：`metadata.remark` 为主，未设置时以机器身份（12 位 hex）为主并改用等宽字体；
  机器身份不重复显示。备注是唯一的人类标签，页面不要求也不允许手填节点标识
- 接入弹窗：控制面地址一个输入项；节点标识由前端 `crypto.getRandomValues` 生成 12 位 hex
  并直接写入 join 命令，接入后可在 Agent 台账设置备注；签发的一次性引导 token 明文只显示
  本次
- 吊销弹窗：目标身份、信任域、待吊销序列号、生效后果、原因
  （`unspecified` / `key_compromise` / `superseded` / `cessation_of_operation`）；写 CRL 后
  节点 ≤60 秒生效，用户证书即时生效并踢掉关联会话
- 恢复弹窗（仅节点）：可编辑控制面地址 → 签发一次性 bootstrap token → 展示 `kurrent join`
  命令与复制按钮；关闭即清空明文，「刷新台账」重新取数。节点恢复不需要手工删除本地证书
- 数据来源：`GET /pki/users`、`GET /pki/components`、`GET /pki/control`、`GET /pki/tokens`、
  `GET /sessions`、`GET /agents`、`DELETE /pki/users/{cn}`、`DELETE /pki/components/{agent_id}`、
  `DELETE /pki/tokens/{token_id}`、`DELETE /sessions/{session_id}`、`POST /pki/tokens`；
  页面不展示私钥，token 明文只在签发当次响应与恢复弹窗中出现

### Operations

- 操作日志列表，每行：ID、时间戳、操作类型（op）、状态 Badge、客户端 IP
- 展开详情：额外的 JSON 键值对
- 增量加载："加载更多"按钮，使用 `since` cursor 分页
- 最新日志排在最前面（entries 数组 reverse）

---

## i18n 国际化

### 架构

```
I18nProvider (React Context)
├── useI18n() hook → { t, locale, setLocale }
├── zh-CN.js     中文翻译
└── en-US.js     英文翻译（fallback）
```

### 语言检测

优先级：`localStorage('cp_locale')` → `navigator.language` → `en-US` fallback

### 翻译策略（zh-CN）

- **保留英文**：品牌名、技术名词（Worker、Agent、iSCSI、IQN、MAC、ISO）、导航链接
- **翻译中文**：按钮动词（创建/删除/取消/确认/刷新/加载更多）、表单标签（主机名/操作系统/磁盘类型/存储节点）、状态说明、提示信息、确认对话框
- **能力值翻译**：Agent 的技术描述（如 `reflink (FICLONE)` → `秒级快照 (reflink)`）通过 `capLabels` 映射表翻译

### t() 函数

```js
t('workers.count', { count: 5 })  // → "5 个"
t('agents.capLabels')['reflink (FICLONE) -> shutil.copy fallback']  // → "秒级快照..."
```

---

## API 集成

### 请求封装

```js
// api/client.js
const BASE = '/api/cp'

async function request(path, options) {
  // 自动拼接 BASE + path；credentials: 'same-origin' 携带会话 Cookie
  // 非 2xx 响应解析 error detail 抛出；401 触发全局回登录页（raw 选项可跳过）
}
```

### 鉴权（会话 Cookie）

前端**不落任何凭据**在 localStorage：

1. 启动探测 `GET /whoami`（`raw: true`，不触发 401 跳转）→ 已认证（浏览器导入了管理证书时走 mTLS 自动通过）直接进入
2. 未认证 → 登录页粘贴**管理 token**（`kurrent token create --usage admin`）→ `POST /session` 换取 HttpOnly 服务端会话 Cookie（12 小时 TTL，剩余不足一半才滑动续期；登记在控制面，删登记即失效）
3. 任意请求 401 → `setUnauthorizedHandler` 把状态置回 `unauthed`（回登录页）
4. 登出 = `DELETE /session`（无状态签名会话，服务端无吊销态）

### 代理（Vite 开发 vs Nginx 生产）

| 环境 | `/api/cp/*` 代理 | 配置位置 |
|---|---|---|
| `npm run dev` | Vite proxy → `https://127.0.0.1`（nginx 443，`secure:false`；保留 `/api/cp` 前缀，`VITE_CP_PROXY` 可覆盖） | `vite.config.js` |
| 生产部署 | nginx `proxy_pass` → unix socket `/run/kurrent/cp.sock`（剥离 `/api/cp/` 前缀） | `deploy/nginx/nginx.conf` |

---

## 部署架构

### 编排位置

WebUI 不再独立编排：`deploy/` 只提供 nginx 镜像，服务定义在**仓库根 `docker-compose.yml`** 的
`kurrent-nginx` 服务中（与 dnsmasq / control-plane 同一 compose 网络与入口）。

### 引导链与安装介质

同一 nginx 容器还提供引导链的数据面：

| 端点 | 用途 |
|---|---|
| `/` | WebUI SPA（`try_files → index.html`） |
| `/api/cp/*` | Control Plane API 代理（含 `GET /boot-assets` 引导链状态） |
| `/file/*` | iPXE 文件下载（ISO、kernel、initrd） |
| `/tftp/*` | 引导脚本与固件（HTTPS 取回，映射 tftp 根） |

引导链状态由控制面解析 `dnsmasq.conf` 与 iPXE 脚本生成，nginx 只做代理与静态文件服务。

### Docker Compose（仓库根 `docker-compose.yml` 的 `kurrent-nginx`）

```yaml
services:
  kurrent-nginx:
    build: { context: ./webui/deploy, dockerfile: Dockerfile }
    ports:
      - "80:80"                                  # 仅 301 升级到 443
      - "${KURRENT_HTTPS_PORT:-443}:443"          # 唯一对外入口（WebUI + API + 引导链 + 文件）
    volumes:
      - ./webui/app/dist:/usr/share/nginx/html:ro                 # WebUI 静态文件
      - ./webui/deploy/public:/data:ro                            # iPXE 公开文件（ISO/kernel/initrd）
      - ./control_plane/state/certs:/etc/nginx/certs:ro            # TOFU 自签证书
      - ./control_plane/state/pki/nginx:/etc/nginx/pki:ro          # 信任包（users+nodes 公钥，无私钥）
      - ./tftp:/tftp:ro                                          # iPXE 菜单/覆盖脚本（HTTPS 同源）
```

### 构建命令

```bash
cd webui/app
npm install
npm run build              # 输出到 dist/（由仓库根 compose 只读挂载进 nginx）
```

---

## 目录结构

```
webui/
├── app/
│   ├── package.json
│   ├── vite.config.js           # Vite 构建配置 + 开发代理
│   ├── index.html
│   └── src/
│       ├── main.jsx             # 入口：I18nProvider + HashRouter
│       ├── App.jsx              # 路由定义 + 认证门（Gate）
│       ├── auth.jsx             # 认证上下文（whoami 探测 / 登录 / 登出 / 401 回登录页）
│       ├── api/
│       │   └── client.js        # API 请求封装（fetch + 会话 Cookie + 401 钩子）
│       ├── components/          # 通用组件
│       │   ├── Layout.jsx/css   # 可折叠侧边栏（导航 + 登出 + 语言切换）
│       │   ├── Login.jsx/css    # 登录页（管理 token 换会话）
│       │   ├── Modal.jsx/css    # 全局弹窗
│       │   ├── Card.jsx/css     # 卡片容器
│       │   ├── Button.jsx/css   # 按钮
│       │   ├── Input.jsx/css    # 输入框
│       │   ├── Select.jsx/css   # 下拉选择
│       │   ├── Badge.jsx/css    # 状态标签
│       │   ├── Divider.jsx/css  # 分割线
│       │   ├── EmptyState.jsx/css  # 空状态
│       │   ├── CodeBlock.jsx/css   # 代码块
│       │   └── ConfirmAction.jsx/css # 确认对话框
│       ├── pages/               # 页面组件
│       │   ├── Dashboard.jsx/css
│       │   ├── Workers.jsx/css
│       │   ├── WorkerDetail.jsx/css
│       │   ├── Devices.jsx/css
│       │   ├── BootChain.jsx/css
│       │   ├── Agents.jsx/css
│       │   ├── AgentLuns.jsx/css
│       │   └── Operations.jsx/css
│       ├── i18n/                # 国际化
│       │   ├── index.jsx        # I18nProvider + useI18n
│       │   ├── zh-CN.js         # 简体中文
│       │   └── en-US.js         # English
│       └── styles/
│           └── global.css       # CSS 变量 + Reset + 字体
│
└── deploy/
    ├── Dockerfile               # nginx:1.31-alpine
    ├── nginx/
    │   ├── nginx.conf           # location 块 + upstream（编排在仓库根 docker-compose.yml）
    │   └── snippets/            # 身份头断言片段
    ├── public/                  # iPXE 公开文件目录
    │   └── .gitkeep
```

---

## 技术栈

| 层 | 技术 |
|---|---|
| 框架 | React 18 |
| 构建 | Vite 5 |
| 路由 | React Router 6 (HashRouter) |
| 样式 | 纯 CSS（CSS 变量驱动，零第三方 UI 库） |
| i18n | 自研 React Context（零依赖） |
| API | 原生 fetch（零 HTTP 库） |
| 部署 | nginx:1.31-alpine（静态文件 + API 代理，无脚本执行面） |
| 构建产物 | 纯静态文件（HTML + JS + CSS）：JS 341.9 KB（gzip 107.0 KB）／CSS 45.9 KB（gzip 7.7 KB） |
