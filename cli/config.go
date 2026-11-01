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

var (
	configPrintSpec   = cmdSpec{usage: configPrintUsage, minPos: 1, maxPos: 1, posNames: []string{"template"}}
	configInspectSpec = cmdSpec{
		usage: configInspectUsage,
		flags: []flagSpec{
			{name: "node", kind: flagBool},
			{name: "config", kind: flagString},
			{name: "dir", kind: flagString},
		},
		maxPos: 0,
	}
	configStatusSpec = cmdSpec{usage: configStatusUsage, maxPos: 0}
)

func cmdConfig(args []string) {
	if groupHelp("config", args, configGroupUsage) {
		return
	}
	switch args[0] {
	case "print":
		configPrint(args[1:])
	case "validate", "show":
		configInspectCmd(args[0], args[1:])
	case "status":
		configStatus(args[1:])
	default:
		usageError(configGroupUsage, fmt.Errorf("unknown config subcommand: %s", args[0]))
	}
}

func configPrint(args []string) {
	p, help := mustParse(args, configPrintSpec)
	if help {
		return
	}
	switch p.pos[0] {
	case "init-defaults":
		fmt.Print(cpDefaultsTemplate)
	case "node-defaults":
		fmt.Print(nodeDefaultsTemplate)
	default:
		usageError(configPrintUsage, fmt.Errorf("unknown template %q: supported values are init-defaults and node-defaults", p.pos[0]))
	}
}

func configInspectCmd(cmd string, args []string) {
	p, help := mustParse(args, configInspectSpec)
	if help {
		return
	}
	for _, flag := range []string{"config", "dir"} {
		if p.has(flag) && strings.TrimSpace(p.opt(flag, "")) == "" {
			usageError(configInspectUsage, fmt.Errorf("--%s must not be empty", flag))
		}
	}
	if err := configInspect(cmd, p); err != nil {
		fatal("%v", err)
	}
}

func configStatus(args []string) {
	if _, help := mustParse(args, configStatusSpec); help {
		return
	}
	printPrettyJSON(api("GET", "/config", nil))
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
	configFile, err := resolveConfigFile(root, p.opt("config", ""), p.bool("node"))
	if err != nil {
		return err
	}
	data, err := os.ReadFile(configFile)
	if err != nil {
		return fmt.Errorf("read %s: %w", configFile, err)
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
		return fmt.Errorf("%s: kind = %q (expected ControlPlaneConfiguration|NodeConfiguration)",
			configFile, kind)
	}

	fmt.Fprintf(os.Stderr, "==> kurrent config %s: building the %s validation image\n", cmd, image)
	if err := composeRunStderr(composeDir, "build", image); err != nil {
		return err
	}
	out, err := runConfigTool(image, cmd, data)
	if err != nil {
		return fmt.Errorf("%s validation failed (%s):\n%s", configFile, label, err)
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
			return "", fmt.Errorf("get current directory: %w", err)
		}
		start = cwd
	}
	abs, err := filepath.Abs(start)
	if err != nil {
		return "", fmt.Errorf("resolve --dir: %w", err)
	}
	if fi, err := os.Stat(abs); err != nil || !fi.IsDir() {
		return "", fmt.Errorf("--dir is not a directory: %s", abs)
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
	return "", fmt.Errorf("repository root not found (control_plane/ and storager/ must both exist; run inside the repository or pass --dir)")
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
				return "", fmt.Errorf("resolve --config: %w", err)
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
		return "", fmt.Errorf("declaration file does not exist: %s\nGenerate a template first and edit it: %s", path, template)
	}
	return path, nil
}

// configKind 读取声明根 kind（不做完整校验——完整校验在组件镜像内完成）。
func configKind(data []byte) (string, error) {
	var root yaml.Node
	if err := yaml.Unmarshal(data, &root); err != nil {
		return "", fmt.Errorf("parse YAML: %w", err)
	}
	kind := nodeValue(&root, "kind")
	if kind == "" {
		return "", fmt.Errorf("missing kind (ControlPlaneConfiguration|NodeConfiguration)")
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
const cpDefaultsTemplate = `# Control-plane declarative configuration: all business policy lives in this file.
# Container paths and container names belong to the deployment manifest (docker-compose.yml);
# runtime state (registration window, etc.) lives in state/settings.json and is not declarative.
# Runtime files are not committed: this template matches
# ` + "`" + `kurrent config print init-defaults` + "`" + `. Generate or copy it as kurrent.yaml, edit it,
# then run kurrent init to validate and start the control plane (.gitignore ignores kurrent.yaml).
apiVersion: kurrent.io/v1
kind: ControlPlaneConfiguration
metadata:
  name: kurrent-cp
spec:
  # PXE deployment network declaration:
  # the control plane renders dnsmasq.conf from this block at startup (the YAML is authoritative;
  # the conf is derived). Do not edit dnsmasq.conf by hand.
  networking:
    interface: enp3s0                  # Bind interface (dnsmasq interface= + bind-interfaces)
    subnet: 192.168.80.0/24            # Service subnet (CIDR; the netmask is derived)
    dhcpRange: 192.168.80.50,192.168.80.100   # DHCP pool start/end
    gateway: 192.168.80.2              # dhcp-option=3
    dns: 223.5.5.5                     # dhcp-option=6
  # Component PKI policy (three root CAs users/nodes/control, agent_id-bound bootstrap
  # tokens, and certificate rotation)
  pki:
    bootstrapTokenTtlMinutes: 60
    bootstrapTokenMaxTtlMinutes: 1440
    bootstrapTokenMaxUses: 100
    componentCertDays: 90
    renewThreshold: 0.2
    crlRefreshHours: 12
  # TOFU boot-chain server certificate (self-signed, generated idempotently at startup;
  # nginx mounts state/certs read-only)
  serverCert:
    san: "IP:127.0.0.1,DNS:localhost"
    days: 3650
    # Re-issue when the declaration changes (default false: report drift only; re-issuing
    # changes the TOFU fingerprint)
    rotateOnChange: false
  # iPXE boot behavior
  boot:
    defaultArch: x86_64
    menuTimeoutMs: 5000
    autoBootTimeoutMs: 1
  # Management policy: session TTL, certificate lifetimes, automation token TTL cap
  auth:
    sessionTtlHours: 12
    adminCertDays: 365
    userCertDaysMax: 3650
    tokenTtlHours: 24
    tokenMaxTtlHours: 720
  # Device registration window limits (the window is opened briefly during deployment;
  # it cannot be configured as permanent)
  registration:
    windowMaxTtlMinutes: 60
    windowMaxDevices: 1000
  # DHHC-1 periodic rotation and push reconciliation (0 disables the corresponding background task)
  credentials:
    rotationDays: 90
    reconcileIntervalSeconds: 60
  # Storage agent call timeout (seconds)
  agentTimeoutSec: 10
`

// nodeDefaultsTemplate 节点声明模板（与 storager/kurrent.yaml.example 同源）。
const nodeDefaultsTemplate = `# Node declarative configuration
#
# Authoritative source: this template matches ` + "`" + `kurrent config print node-defaults` + "`" + `.
# Generate or copy it as storager/kurrent.yaml, then edit it (kurrent.yaml is not committed).
# kurrent join <cp-url> --token <token> --agent-id <id> validates it and converges the agent
# (the address and node id are carried by the printed command; join generates kurrent.yaml
# when missing). The agent and nvmet-host containers mount the same file
# (/etc/kurrent/kurrent.yaml, read-only) and each loads its own spec subtree.
#
# Layering: this file declares node-level business configuration (identity/data-plane parameters) only.
# - Container paths (pki/cp-ca/logs/cache/disk mounts) and listen/internal addresses belong
#   to the deployment manifest (docker-compose.yml); the application fixes mount targets in constants.
# - Bootstrap credentials live in separate files under storager/bootstrap/: agent.token (bound to the
#   node identity; written by join, single-use, 60 minutes) and nvmet-host.token (derived by the
#   control plane when an agent enrolls with backend=nvmet and returned in the response; bound to
#   agent_id, without knowing the backend in advance).
# - diskDir = host storage path (data directory declaration; join syncs it to the .env
#   KURRENT_DISK_DIR interpolation key used as the compose mount source).
#
# Validation semantics (pydantic v2): unknown fields are rejected (extra="forbid"), missing
# required fields fail, defaults are injected, and identity/path/NQN semantics are validated.
# kurrent join and kurrent config validate use the same schema inside the agent image before
# writing, so configuration errors do not wait for container startup.
# The container path is fixed at /etc/kurrent/kurrent.yaml (KURRENT_CONFIG_FILE can override
# it for test injection).
apiVersion: kurrent.io/v1
kind: NodeConfiguration
metadata:
  # Node identity = agent_id (control-plane agents.yml key, component certificate CN): a machine
  # identity that accepts only 12 lowercase hex characters (same shape as disk os_tag and short
  # Docker container IDs), bound by the bootstrap token (kurrent token create --agent-id;
  # generated when omitted). join writes and persists it; do not edit it by hand. The identity
  # must stay stable and is naturally unique per node. The template leaves it empty: join must write it.
  name: ""
  # Remark (optional, for human recognition): the only place for a descriptive name
  # (e.g. production storage / rack A). It is registered on first enroll (shown on WebUI agent
  # cards and editable in the form) and is not used in any identity or data-plane computation.
  remark: ""
spec:
  agent:
    # Data-plane backend (required): nvmet (preferred/default) | stgt | lio
    backend: nvmet
    # Control-plane reachable address (enroll registers it as agents.yml base_url; kurrent join
    # derives https://<cp-host>:4840 by default; edit this key for NAT and similar cases)
    # advertiseUrl: https://host.docker.internal:4840
    # Host storage path (required, absolute; data directory declaration; compose mount source
    # synced through the .env interpolation key)
    diskDir: /storager_img
    # Disk identifier namespace (required; NQN is authoritative; the iSCSI data-plane IQN is derived from it)
    # The control plane does not declare an NQN domain: both host NQN and disk NQN bases use
    # this field (reported through capabilities.base_nqn during enroll; the control plane
    # reassembles it from the disk NQN prefix)
    nqnBase: nqn.2026-07.com.kurrent
  controlPlane:
    # Control-plane API endpoint (the mTLS target for agent/nvmet-host enroll/renew; points at
    # the control-plane service, unrelated to the WebUI/nginx entry; kurrent join <cp-url> syncs
    # the command address into this key, so a pre-filled value is allowed but the command wins;
    # the nvmet-host container uses host networking and compose maps host.docker.internal to the
    # host loopback through extra_hosts, so both components interpret this field the same way)
    url: https://host.docker.internal
`
