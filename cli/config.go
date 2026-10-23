// config 子命令：声明配置模板输出。
// 输出带注释的完整模板到 stdout，重定向即生成声明文件：
//
//	kurrent config print init-defaults > control_plane/kurrent.yaml   （控制面声明）
//	kurrent config print node-defaults  > storager/kurrent.yaml       （节点声明）
//
// 模板与仓库内 control_plane/kurrent.yaml.example / storager/kurrent.yaml.example
// 同源（同步维护：改注释先改 example，再同步本文件常量），networking 五键为示例值，
// 按部署环境修改后由 kurrent init / kurrent join 校验并收敛启动。
package main

import "fmt"

func cmdConfig(args []string) {
	if len(args) < 2 || args[0] != "print" {
		fatal("用法: kurrent config print init-defaults|node-defaults\n" +
			"      init-defaults  控制面声明模板（重定向为 control_plane/kurrent.yaml 后编辑）\n" +
			"      node-defaults  节点声明模板（重定向为 storager/kurrent.yaml 后编辑）")
	}
	switch args[1] {
	case "init-defaults":
		fmt.Print(cpDefaultsTemplate)
	case "node-defaults":
		fmt.Print(nodeDefaultsTemplate)
	default:
		fatal("未知模板: %s（支持 init-defaults|node-defaults）", args[1])
	}
}

// cpDefaultsTemplate 控制面声明模板（与 control_plane/kurrent.yaml.example 同源）。
const cpDefaultsTemplate = `# 控制面声明式配置：业务策略全部收敛到此文件，
# 容器内路径/容器名属部署清单 docker-compose.yml 职责；运行时状态（注册窗口等）在
# state/settings.json，不属声明配置。运行时文件不入库：模板与
# ` + "`" + `kurrent config print init-defaults` + "`" + ` 输出同源——生成或复制本文件为 kurrent.yaml
# 后编辑，kurrent init 校验并启动控制面（.gitignore 已忽略 kurrent.yaml）。
apiVersion: kurrent.io/v1
kind: ControlPlaneConfiguration
metadata:
  name: kurrent-cp
spec:
  # PXE 部署网络声明：
  # dnsmasq.conf 由控制面启动时按本块生成（yml 是权威，conf 为派生物），勿手工编辑 dnsmasq.conf
  networking:
    interface: enp3s0                  # 绑定网卡（dnsmasq interface= + bind-interfaces）
    subnet: 192.168.80.0/24            # 服务网段（CIDR，掩码自动推导）
    dhcpRange: 192.168.80.50,192.168.80.100   # DHCP 池起止
    gateway: 192.168.80.2              # dhcp-option=3
    dns: 223.5.5.5                     # dhcp-option=6
  # 组件 PKI 策略（三分根 CA users/nodes/control + 绑 agent_id 的 bootstrap token 引导 + 证书轮换）
  pki:
    bootstrapTokenTtlMinutes: 60
    componentCertDays: 90
    renewThreshold: 0.2
  # TOFU 引导链服务器证书（自签，启动时幂等生成；nginx 只读挂载 state/certs）
  serverCert:
    san: "IP:127.0.0.1,DNS:localhost"
    days: 3650
  # iPXE 引导行为
  boot:
    defaultArch: x86_64
    menuTimeoutMs: 5000
    autoBootTimeoutMs: 1
  # 调用存储 Agent 超时（秒）
  agentTimeoutSec: 10
`

// nodeDefaultsTemplate 节点声明模板（与 storager/kurrent.yaml.example 同源）。
const nodeDefaultsTemplate = `# 节点声明式配置
#
# 权威配置来源：模板与 ` + "`" + `kurrent config print node-defaults` + "`" + ` 输出同源——生成或复制
# 本文件为 storager/kurrent.yaml 后编辑（kurrent.yaml 不入库）；kurrent join
# <cp-url> --token <token> --agent-id <id> 校验并收敛启动 agent（地址与节点 id 随
# 签发命令携带，kurrent.yaml 缺失时由 join 自动生成）。agent 与 nvmet-host 容器
# 挂载同一份文件（/etc/kurrent/kurrent.yaml，只读），各自加载自己的 spec 子块。
#
# 分层职责：本文件只声明节点级业务配置（身份/数据面参数）；
# - 容器内路径（pki/cp-ca/日志/缓存/盘目录挂载点）与监听/内部通讯地址属部署清单
#   docker-compose.yml 职责（compose 硬编码挂载目标，应用侧以模块常量固化）
# - 引导凭据在独立文件 storager/bootstrap/：agent.token（绑节点身份的 token：
#   join 写入、默认一次性、60 分钟）；nvmet-host.token 由 agent
#   enroll 上报 backend=nvmet 时控制面派生随响应下发并落盘（绑 agent_id、不预知后端）
# - diskDir = 宿主存储路径（数据目录声明；
#   join 同步为 .env 的 KURRENT_DISK_DIR 插值键，compose 挂载源）
#
# 校验语义（pydantic v2）：未知字段拒绝（extra="forbid"）、必填缺失报错、
# 默认值注入——配置错误在容器启动即失败，杜绝 .env 自由键值的静默偏差。
# 容器内路径固定 /etc/kurrent/kurrent.yaml（KURRENT_CONFIG_FILE 可覆盖，测试注入用）。
apiVersion: kurrent.io/v1
kind: NodeConfiguration
metadata:
  # 节点身份 = agent_id（控制面 agents.yml 登记键、组件证书 CN）：机器身份——由
  # bootstrap token 绑定（kurrent token create --agent-id；省略则生成 12 位 hex 随机
  # 标识，与盘 os_tag 同构），join 写入并持久化，勿手工改写；身份须稳定，多节点
  # 各自一个、天然不冲突
  name: ""
  # 备注（可选，人类识别用）：手工输入的唯一字段（如 storage-nvmet-01 / 生产存储）。
  # 随首次 enroll 登记进控制面台账（WebUI Agent 卡片展示、编辑表单可改），
  # 不参与任何身份/数据面运算
  remark: ""
spec:
  agent:
    # 数据面后端（必填）：nvmet（首选/默认）| stgt | lio
    backend: nvmet
    # 控制面可达地址（enroll 自动登记写入 agents.yml base_url；kurrent join 默认推导
    # https://<cp-host>:4840，特殊场景（NAT 等）手工编辑本键覆盖）
    # advertiseUrl: https://host.docker.internal:4840
    # 宿主存储路径（必填；数据目录声明，compose 挂载源经 .env 插值同步）
    diskDir: /storager_img
    # 盘标识命名空间（必填；权威：NQN，iSCSI 数据面 IQN 由此派生）
    # 控制面不声明 NQN 域：Host NQN 与盘 NQN 的 base 均以本字段为权威
    # （enroll 经 capabilities.base_nqn 上报，控制面由盘 NQN 前缀重拼）
    nqnBase: nqn.2026-07.com.kurrent
  controlPlane:
    # 控制面 API 端点（agent/nvmet-host 容器 enroll/renew 的 mTLS 连接目标——指向
    # 控制面服务，与 WebUI/nginx 访问入口无关；kurrent join
    # <cp-url> 会把命令地址同步写入本键，预填亦可，命令参数为准；nvmet-host 容器为
    # host 网络，compose 经 extra_hosts 把 host.docker.internal 映射到宿主 loopback，
    # 因此两组件使用本字段的语义一致）
    url: https://host.docker.internal
`
