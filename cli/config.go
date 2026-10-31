// config 子命令：声明模板输出、全量校验/展示与运行态状态。
//
// 模板（print）用于起草；validate/show 在组件镜像内执行与运行时同一份 pydantic
// schema（python -m app.config_tool，stdin 传入候选文件），CLI 不维护第二套字段
// 白名单；status 经已认证的 GET /config 读取运行态生效声明与派生件漂移。
//
//	kurrent config print init-defaults > control_plane/kurrent.yaml
//	kurrent config print node-defaults  > storager/kurrent.yaml
//	kurrent config validate [--node] [--config PATH] [--dir REPO]
//	kurrent config show     [--node] [--config PATH] [--dir REPO]
//	kurrent config status
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

const configUsageText = `用法: kurrent config print init-defaults|node-defaults
      kurrent config validate [--node] [--config PATH] [--dir REPO]
      kurrent config show     [--node] [--config PATH] [--dir REPO]
      kurrent config status

  print     输出声明模板（重定向为 control_plane/kurrent.yaml 或
            storager/kurrent.yaml 后编辑）
  validate  在组件镜像内按运行时 schema 全量校验（默认控制面声明；
            --node 或配置 kind=NodeConfiguration 时校验节点声明）
  show      输出生效声明（默认值已注入）
  status    读取运行态：已加载声明、configHash、dnsmasq.conf 与服务器证书漂移`

func cmdConfig(args []string) {
	if len(args) == 0 || args[0] == "help" || args[0] == "-h" {
		fmt.Print(configUsageText + "\n")
		return
	}
	switch args[0] {
	case "print":
		if len(args) < 2 {
			fatal("用法: kurrent config print init-defaults|node-defaults")
		}
		switch args[1] {
		case "init-defaults":
			fmt.Print(cpDefaultsTemplate)
		case "node-defaults":
			fmt.Print(nodeDefaultsTemplate)
		default:
			fatal("未知模板: %s（支持 init-defaults|node-defaults）", args[1])
		}
	case "validate", "show":
		p := parseArgs(args[1:])
		if len(p.pos) > 0 {
			fatal("多余位置参数: %s（%s）", strings.Join(p.pos, " "), configUsageText)
		}
		if err := configInspect(args[0], p); err != nil {
			fatal("%v", err)
		}
	case "status":
		p := parseArgs(args[1:])
		if len(p.pos) > 0 {
			fatal("多余位置参数: %s（%s）", strings.Join(p.pos, " "), configUsageText)
		}
		printPrettyJSON(api("GET", "/config", nil))
	default:
		fatal("未知 config 子命令: %s\n%s", args[0], configUsageText)
	}
}

func printPrettyJSON(data []byte) {
	var buf bytes.Buffer
	if err := json.Indent(&buf, data, "", "  "); err != nil {
		os.Stdout.Write(data)
		if len(data) == 0 || data[len(data)-1] != '\n' {
			fmt.Println()
		}
		return
	}
	fmt.Println(buf.String())
}

// configInspect validate/show 共用入口：解析候选声明 → 选定组件镜像 → 容器内校验/展示。
func configInspect(cmd string, p parsedArgs) error {
	root, err := resolveRepoRoot(p.opt("dir", ""))
	if err != nil {
		return err
	}
	configFile, err := resolveConfigFile(root, p.opt("config", ""), p.has("node"))
	if err != nil {
		return err
	}
	data, err := os.ReadFile(configFile)
	if err != nil {
		return fmt.Errorf("读取 %s: %w", configFile, err)
	}
	kind, err := configKind(data)
	if err != nil {
		return fmt.Errorf("%s: %w", configFile, err)
	}
	var composeDir, image, label string
	switch kind {
	case "ControlPlaneConfiguration":
		composeDir = root
		image = "kurrent-control-plane"
		label = "control_plane/kurrent.yaml"
	case "NodeConfiguration":
		backend := nodeValueFromYAML(data, "spec", "agent", "backend")
		sub := "nvmeof"
		if backend == "stgt" || backend == "lio" {
			sub = "iscsi"
		}
		composeDir = filepath.Join(root, "storager", sub)
		image = "storager-agent"
		label = "storager/kurrent.yaml"
	default:
		return fmt.Errorf("%s: kind = %q（支持 ControlPlaneConfiguration|NodeConfiguration）",
			configFile, kind)
	}

	fmt.Fprintf(os.Stderr, "==> kurrent config %s: building %s validation image\n", cmd, image)
	if err := composeRunStderr(composeDir, "build", image); err != nil {
		return err
	}
	out, err := runConfigTool(image, cmd, data)
	if err != nil {
		return fmt.Errorf("%s 校验失败（%s）:\n%s", configFile, label, err)
	}
	if cmd == "show" {
		fmt.Print(out)
		return nil
	}
	fmt.Fprintf(os.Stderr, "%s: valid (%s)\n", configFile, strings.TrimSpace(out))
	return nil
}

// resolveRepoRoot 定位仓库根：--dir 显式；否则从 cwd 逐级上溯（含 control_plane/ 与 storager/）。
// --dir 也接受 control_plane/ 或 storager/ 本身。
func resolveRepoRoot(explicit string) (string, error) {
	start := explicit
	if start == "" {
		cwd, err := os.Getwd()
		if err != nil {
			return "", fmt.Errorf("获取当前目录: %w", err)
		}
		start = cwd
	}
	abs, err := filepath.Abs(start)
	if err != nil {
		return "", fmt.Errorf("解析 --dir: %w", err)
	}
	if fi, err := os.Stat(abs); err != nil || !fi.IsDir() {
		return "", fmt.Errorf("--dir 不是目录: %s", abs)
	}
	base := filepath.Base(abs)
	if base == "control_plane" || base == "storager" {
		abs = filepath.Dir(abs)
	}
	for dir := abs; ; dir = filepath.Dir(dir) {
		if isDir(filepath.Join(dir, "control_plane")) && isDir(filepath.Join(dir, "storager")) {
			return dir, nil
		}
		if parent := filepath.Dir(dir); parent == dir {
			break
		}
	}
	return "", fmt.Errorf("未找到仓库根（需同时存在 control_plane/ 与 storager/；请在仓库内运行或 --dir 指定）")
}

func isDir(path string) bool {
	fi, err := os.Stat(path)
	return err == nil && fi.IsDir()
}

// resolveConfigFile 候选声明路径：--config 优先；否则按 --node 取节点/控制面默认位，
// 默认位不存在时给出模板生成指引。
func resolveConfigFile(root, explicit string, node bool) (string, error) {
	if explicit != "" {
		if !filepath.IsAbs(explicit) {
			abs, err := filepath.Abs(explicit)
			if err != nil {
				return "", fmt.Errorf("解析 --config: %w", err)
			}
			return abs, nil
		}
		return explicit, nil
	}
	var path, template string
	if node {
		path = filepath.Join(root, "storager", "kurrent.yaml")
		template = "kurrent config print node-defaults > storager/kurrent.yaml"
	} else {
		path = filepath.Join(root, "control_plane", "kurrent.yaml")
		template = "kurrent config print init-defaults > control_plane/kurrent.yaml"
	}
	if _, err := os.Stat(path); err != nil {
		return "", fmt.Errorf("声明文件不存在: %s\n先生成模板并编辑：%s", path, template)
	}
	return path, nil
}

// configKind 读取声明根 kind（不做完整校验——完整校验在组件镜像内完成）。
func configKind(data []byte) (string, error) {
	var root yaml.Node
	if err := yaml.Unmarshal(data, &root); err != nil {
		return "", fmt.Errorf("解析 YAML: %w", err)
	}
	kind := nodeValue(&root, "kind")
	if kind == "" {
		return "", fmt.Errorf("缺少 kind（ControlPlaneConfiguration|NodeConfiguration）")
	}
	return kind, nil
}

// nodeValueFromYAML 解析 YAML 后取路径标量（缺失返回空串）。
func nodeValueFromYAML(data []byte, path ...string) string {
	var root yaml.Node
	if err := yaml.Unmarshal(data, &root); err != nil {
		return ""
	}
	return nodeValue(&root, path...)
}

// runConfigTool 在组件镜像内执行 config_tool：候选配置经 stdin 传入，不落临时文件，
// 不挂载宿主路径；--network none 保证校验过程无外部访问。
func runConfigTool(image, cmd string, configData []byte) (string, error) {
	args := []string{
		"run", "--rm", "-i", "--network", "none",
		"--entrypoint", "python", image,
		"-m", "app.config_tool", cmd,
	}
	command := exec.Command("docker", args...)
	command.Stdin = bytes.NewReader(configData)
	var stdout, stderr bytes.Buffer
	command.Stdout, command.Stderr = &stdout, &stderr
	if err := command.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = strings.TrimSpace(stdout.String())
		}
		if msg == "" {
			msg = err.Error()
		}
		return "", fmt.Errorf("%s", msg)
	}
	return stdout.String(), nil
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
    bootstrapTokenMaxTtlMinutes: 1440
    bootstrapTokenMaxUses: 100
    componentCertDays: 90
    renewThreshold: 0.2
    crlRefreshHours: 12
  # TOFU 引导链服务器证书（自签，启动时幂等生成；nginx 只读挂载 state/certs）
  serverCert:
    san: "IP:127.0.0.1,DNS:localhost"
    days: 3650
    # 声明变化时是否重签（默认 false：仅报告漂移；重签会改变 TOFU 指纹）
    rotateOnChange: false
  # iPXE 引导行为
  boot:
    defaultArch: x86_64
    menuTimeoutMs: 5000
    autoBootTimeoutMs: 1
  # 管理面策略：会话 TTL、证书有效期、自动化 token TTL 上限
  auth:
    sessionTtlHours: 12
    adminCertDays: 365
    userCertDaysMax: 3650
    tokenTtlHours: 24
    tokenMaxTtlHours: 720
  # 设备注册窗口上限（窗口只在部署期短开，不可配置为永久）
  registration:
    windowMaxTtlMinutes: 60
    windowMaxDevices: 1000
  # DHHC-1 定期轮换与推送对账（0 = 关闭对应后台任务）
  credentials:
    rotationDays: 90
    reconcileIntervalSeconds: 60
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
# 默认值注入、身份/路径/NQN 语义校验——kurrent join 与 kurrent config validate
# 在写盘前用 agent 镜像内同一份 schema 校验，配置错误不会等到容器启动才暴露。
# 容器内路径固定 /etc/kurrent/kurrent.yaml（KURRENT_CONFIG_FILE 可覆盖，测试注入用）。
apiVersion: kurrent.io/v1
kind: NodeConfiguration
metadata:
  # 节点身份 = agent_id（控制面 agents.yml 登记键、组件证书 CN）：机器身份——由
  # bootstrap token 绑定（kurrent token create --agent-id；省略则生成 12 位 hex 随机
  # 标识，与盘 os_tag 同构），join 写入并持久化，勿手工改写；身份须稳定，多节点
  # 各自一个、天然不冲突。模板留空：join 必须写入（空值在 validate 时拒绝）
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
    # 宿主存储路径（必填，绝对路径；数据目录声明，compose 挂载源经 .env 插值同步）
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
