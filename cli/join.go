// join 子命令：存储节点加入控制面并收敛到运行态（配置即声明）。
// 命令携带控制面地址——签发命令输出即带地址，节点粘贴执行即获得地址，
// 无需预编辑任何文件：kurrent join <cp-url> [--token T] [--agent-id ID]。
// 节点声明 storager/kurrent.yaml 缺失时按模板自动生成（url=命令地址、name=12 位小写 hex、
// backend nvmet、advertiseUrl 推导、diskDir/nqnBase 默认），已存在则读入并把地址同步
// 进声明（非 forbid 式合并，未知字段与手工编辑保留；yml 即 kurrent.yaml，后续可编辑）。
// 身份分层：name = 机器身份（12 位小写 hex；bootstrap token 绑定它；--agent-id 写入，
// 未给则 join 自动生成）；人工输入归 metadata.remark（备注，人类识别用，随首次 enroll
// 带入控制面台账）。
// 引导凭据落盘（--token 或既有 bootstrap/agent.token）→ .env 插值键同步 → 收敛启动
// agent（docker compose 为内部编排细节，重跑幂等）。nvmet 后端先预检宿主前置
// （内核模块 nvmet/nvmet-tcp + configfs 挂载 + tlshd 版本），未就绪指路
// nvmeof/host-prepare.sh。
package main

import (
	"bufio"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

var joinSpec = cmdSpec{
	usage: joinUsage,
	flags: []flagSpec{
		{name: "agent-id", kind: flagString},
		{name: "config", kind: flagString},
		{name: "token", kind: flagString},
		{name: "dir", kind: flagString},
	},
	minPos: 1, maxPos: 1, posNames: []string{"cp-url"},
}

func cmdJoin(args []string) {
	p, help := mustParse(args, joinSpec)
	if help {
		return
	}
	cpURL, err := normalizeCpURL("<cp-url>", p.pos[0])
	if err != nil {
		usageError(joinUsage, err)
	}
	for _, flag := range []string{"agent-id", "config", "token", "dir"} {
		if p.has(flag) && strings.TrimSpace(p.opt(flag, "")) == "" {
			usageError(joinUsage, fmt.Errorf("--%s must not be empty", flag))
		}
	}
	agentID := strings.TrimSpace(p.opt("agent-id", ""))
	if agentID != "" && !validAgentID(agentID) {
		usageError(joinUsage, fmt.Errorf("--agent-id must be 12 lowercase hex characters (machine identity; put descriptive names in metadata.remark): %q", agentID))
	}
	dir, err := resolveStoragerDir(p.opt("dir", ""))
	if err != nil {
		fatal("%v", err)
	}
	configFile := p.opt("config", filepath.Join(dir, "kurrent.yaml"))
	if !filepath.IsAbs(configFile) {
		if configFile, err = filepath.Abs(configFile); err != nil {
			fatal("resolve --config: %v", err)
		}
	}
	if err := joinRun(joinOpts{
		dir: dir, configFile: configFile, cpURL: cpURL,
		token: p.opt("token", ""), agentID: agentID,
	}); err != nil {
		fatal("%v", err)
	}
}

type joinOpts struct {
	dir        string // storager 目录
	configFile string // 节点声明文件（缺省 <dir>/kurrent.yaml；其他路径 = 应用为节点配置）
	cpURL      string // 控制面 HTTPS 入口（必填）
	token      string // 引导凭据（可选：给则写入 agent.token；不给要求文件已就位）
	agentID    string // 节点身份（可选：写入声明 metadata.name；绑定 token 时必须与之一致）
}

var (
	nvmetTokenWaitTimeout  = 60 * time.Second
	nvmetTokenPollInterval = 1 * time.Second
	nvmetTokenWaiter       = waitForTokenFile
	agentStartupWatch      = 2 * time.Second
)

// waitForTokenFile 等待 agent 完成 enroll 并写出派生凭据。调用前 join 已删除旧文件，
// 因此文件重新出现即代表本次 enroll 产生了新 token。
func waitForTokenFile(path string, timeout, interval time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		if info, err := os.Stat(path); err == nil && info.Size() > 0 {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("timed out waiting for %s (%s)", path, timeout)
		}
		time.Sleep(interval)
	}
}

// resolveStoragerDir 定位 storager 目录：--dir 优先；否则 cwd 下 storager/；cwd 已是 storager 时用 cwd。
func resolveStoragerDir(explicit string) (string, error) {
	if explicit != "" {
		abs, err := filepath.Abs(explicit)
		if err != nil {
			return "", fmt.Errorf("resolve --dir: %w", err)
		}
		return abs, nil
	}
	cwd, err := os.Getwd()
	if err != nil {
		return "", fmt.Errorf("get current directory: %w", err)
	}
	if fi, err := os.Stat(filepath.Join(cwd, "storager")); err == nil && fi.IsDir() {
		return filepath.Join(cwd, "storager"), nil
	}
	if _, err := os.Stat(filepath.Join(cwd, "nvmeof")); err == nil {
		return cwd, nil // 已位于 storager 内
	}
	return "", fmt.Errorf("storager directory not found (run from the repository root or pass --dir)")
}

var errAgentIDRequired = fmt.Errorf("the bootstrap token is bound to a node identity: pass --agent-id <id>" +
	" (the command printed by kurrent token create --agent-id already includes it; the id must match the binding)")

var agentIDRe = regexp.MustCompile(`^[0-9a-f]{12}$`)

// validAgentID 节点身份（agent_id）只接受 12 位小写 hex：证书 CN、控制面登记键与
// 数据面随机标识同构；描述性名称不进入身份，写 metadata.remark。
func validAgentID(id string) bool {
	return agentIDRe.MatchString(id)
}

// joinRun 校验/补默认节点声明并收敛启动 agent（测试直接调用，不经 cmdJoin）。
func joinRun(o joinOpts) error {
	target := filepath.Join(o.dir, "kurrent.yaml")
	// 控制面入口来自命令位置参数（必填）：
	// 签发命令输出直接携带，节点执行即获得；同步为声明 spec.controlPlane.url
	if strings.TrimSpace(o.cpURL) == "" {
		return fmt.Errorf("cp-url is required (kurrent join <cp-url> [--token T])")
	}
	cpURL, err := normalizeCpURL("cp-url", o.cpURL)
	if err != nil {
		return err
	}
	cpParsed, err := url.Parse(cpURL)
	if err != nil || cpParsed.Hostname() == "" {
		return fmt.Errorf("cannot parse the control-plane address from cp-url: %s", o.cpURL)
	}

	// 节点声明：kurrent.yaml 缺失时按模板自动生成（无预置文件时用缺省模板：
	// 生成后即权威声明，可后续手工编辑；--config 指向其他路径 = 应用为节点配置）
	if o.agentID != "" && !validAgentID(o.agentID) {
		return fmt.Errorf("--agent-id must be 12 lowercase hex characters (machine identity; put descriptive names in metadata.remark): %s", o.agentID)
	}
	data, err := os.ReadFile(o.configFile)
	var root yaml.Node
	if err != nil {
		if !os.IsNotExist(err) {
			return fmt.Errorf("read %s: %w", o.configFile, err)
		}
		nodeID := o.agentID
		if nodeID == "" {
			if o.token != "" {
				return errAgentIDRequired
			}
			if nodeID, err = newNodeID(); err != nil {
				return err
			}
		}
		declaration, err := newNodeDeclaration(nodeID, cpURL,
			filepath.Join(filepath.Dir(o.dir), "storager_img"))
		if err != nil {
			return fmt.Errorf("generate config template: %w", err)
		}
		if err := yaml.Unmarshal(declaration, &root); err != nil {
			return fmt.Errorf("parse config template: %w", err)
		}
		fmt.Printf("==> kurrent join: %s not found; generated from the default template\n", o.configFile)
	} else if err := yaml.Unmarshal(data, &root); err != nil {
		return fmt.Errorf("parse %s: %w", o.configFile, err)
	}
	if kind := nodeValue(&root, "kind"); kind != "NodeConfiguration" {
		return fmt.Errorf("%s: kind = %q, must be NodeConfiguration", o.configFile, kind)
	}

	// agent_id：机器身份——bootstrap token 绑定的就是这个 id。--agent-id 写入声明
	// （与已声明值不一致即拒绝，身份须稳定）；声明留空且未给 --agent-id 时自动生成
	// 12 位小写 hex（与盘 os_tag 同构、docker 容器 ID 短形式）；给了 --token 却无身份
	// 则拒绝（随机 id 必然与 token 绑定不符，提前报错胜过 enroll 401）。
	// 身份只接受 12 位小写 hex；描述性名称写 metadata.remark。
	agentID := strings.TrimSpace(nodeValue(&root, "metadata", "name"))
	if o.agentID != "" {
		if agentID != "" && agentID != o.agentID {
			return fmt.Errorf("%s: metadata.name=%s does not match --agent-id=%s (the node identity must stay stable; do not rename it midway)",
				o.configFile, agentID, o.agentID)
		}
		if agentID == "" {
			agentID = o.agentID
			setPath(&root, agentID, "metadata", "name")
		}
	}
	if agentID == "" {
		if o.token != "" {
			return errAgentIDRequired
		}
		nodeID, err := newNodeID()
		if err != nil {
			return err
		}
		agentID = nodeID
		setPath(&root, agentID, "metadata", "name")
	} else if !validAgentID(agentID) {
		return fmt.Errorf("%s: metadata.name must be 12 lowercase hex characters (machine identity generated by join; do not enter it manually -- put descriptive names in metadata.remark)",
			o.configFile)
	}
	// 命令地址同步进声明（引导输入权威；agent/nvmet-host enroll/renew 的 mTLS 连接目标）
	setPath(&root, cpURL, "spec", "controlPlane", "url")
	// 后端：缺省 nvmet（pydantic 默认同）；显式声明校验取值
	backend := strings.TrimSpace(nodeValue(&root, "spec", "agent", "backend"))
	if backend == "" {
		backend = "nvmet"
		setPath(&root, backend, "spec", "agent", "backend")
	}
	switch backend {
	case "nvmet", "stgt", "lio":
	default:
		return fmt.Errorf("%s: invalid spec.agent.backend: %s (nvmet|stgt|lio)", o.configFile, backend)
	}
	var composeDir string
	switch backend {
	case "nvmet":
		composeDir = filepath.Join(o.dir, "nvmeof")
	case "stgt", "lio":
		composeDir = filepath.Join(o.dir, "iscsi")
	}
	// nvmet 后端：宿主前置预检（内核模块 + configfs；内核 nvmet target 无法容器化，
	// 未就绪则节点起服后数据面不可用）——fail fast 并给出一键修复命令。
	if backend == "nvmet" {
		if err := hostPreflightNvmet(o.dir); err != nil {
			return err
		}
	}
	// 数据目录 / NQN base：缺省注入（与模板示例一致），显式声明值原样保留
	if strings.TrimSpace(nodeValue(&root, "spec", "agent", "diskDir")) == "" {
		setPath(&root, filepath.Join(filepath.Dir(o.dir), "storager_img"), "spec", "agent", "diskDir")
	}
	// 盘目录是 compose 的 bind 挂载源：存在性兜底创建（非致命——docker 起服时也会建，
	// 但可能归 root 所有；无权限时告警，提示先手工建好）
	if diskDir := nodeValue(&root, "spec", "agent", "diskDir"); diskDir != "" {
		if err := os.MkdirAll(diskDir, 0o755); err != nil {
			fmt.Fprintf(os.Stderr, "kurrent: warning: cannot create disk directory (%s): %v -- create it first and make sure it is writable\n", diskDir, err)
		}
	}
	if strings.TrimSpace(nodeValue(&root, "spec", "agent", "nqnBase")) == "" {
		setPath(&root, "nqn.2026-07.com.kurrent", "spec", "agent", "nqnBase")
	}
	// advertise：声明留空 → 推导 https://<cp-host>:4840；特殊场景（NAT 等）编辑
	// kurrent.yaml 的 spec.agent.advertiseUrl 覆盖。Hostname() 去端口/方括号，
	// JoinHostPort 负责 IPv6 方括号。
	if strings.TrimSpace(nodeValue(&root, "spec", "agent", "advertiseUrl")) == "" {
		setPath(&root, "https://"+net.JoinHostPort(cpParsed.Hostname(), "4840"),
			"spec", "agent", "advertiseUrl")
	}

	// 写回节点配置（非 forbid 式合并：未知/手工编辑字段原样保留；kurrent.yaml 即声明）
	out, err := yaml.Marshal(&root)
	if err != nil {
		return fmt.Errorf("serialize %s: %w", target, err)
	}
	// 写盘前用 agent 镜像内同一份 schema 全量校验：未知字段/非法值当场失败，
	// 不会等容器启动才崩溃（合并保留的未知字段按 extra="forbid" 拒绝）。
	fmt.Println("==> kurrent join: building the validation image (storager-agent)")
	if err := composeRun(composeDir, "build", "storager-agent"); err != nil {
		return fmt.Errorf("build storager-agent image failed: %w", err)
	}
	summary, err := runConfigTool("storager-agent", "validate", out)
	if err != nil {
		return fmt.Errorf("%s declaration validation failed (not written):\n%s", target, err)
	}
	fmt.Printf("==> kurrent join: %s\n", strings.TrimSpace(summary))
	if err := writeFileAtomic(target, out, 0o644); err != nil {
		return fmt.Errorf("write %s: %w", target, err)
	}
	if o.configFile != target {
		fmt.Printf("==> kurrent join: applied %s -> %s\n", o.configFile, target)
	}

	// 引导凭据文件：--token 给则写入 agent.token（0600）；不给则要求文件已就位
	// （控制面侧默认一次性，重跑需重新签发 token 覆盖）。
	// nvmet-host.token 是派生凭据——agent enroll 上报 backend=nvmet 时控制面签发随
	// 响应下发（绑定 agent_id、不预知后端）。nvmet 后端重跑 join 时清除旧派生文件
	// （迁移清理，将由 agent 引导时重新派生；stgt/lio 后端不产生该文件）
	bootstrapDir := filepath.Join(o.dir, "bootstrap")
	if err := os.MkdirAll(bootstrapDir, 0o755); err != nil {
		return fmt.Errorf("create %s: %w", bootstrapDir, err)
	}
	tokenFile := filepath.Join(bootstrapDir, "agent.token")
	if strings.TrimSpace(o.token) != "" {
		// 原子写 + 恒 0600：已有 token 文件权限过宽时也会被收敛
		if err := writeFileAtomic(tokenFile, []byte(strings.TrimSpace(o.token)+"\n"), 0o600); err != nil {
			return fmt.Errorf("write %s: %w", tokenFile, err)
		}
	} else if data, err := os.ReadFile(tokenFile); err != nil || strings.TrimSpace(string(data)) == "" {
		return fmt.Errorf("missing bootstrap token: %s does not exist or is empty -- pass it with kurrent join --token <token> (issued by kurrent token create --agent-id %s)",
			tokenFile, agentID)
	}
	if backend == "nvmet" {
		if err := os.Remove(filepath.Join(bootstrapDir, "nvmet-host.token")); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("remove old %s: %w", filepath.Join(bootstrapDir, "nvmet-host.token"), err)
		}
	}
	// .env 仅保留 compose 插值键（业务键已收敛到 kurrent.yaml，旧键自动清空）
	projectRoot := filepath.Dir(o.dir)
	entries := []envEntry{
		{"KURRENT_AGENT_PKI_HOST", filepath.Join(projectRoot, "control_plane/state/pki/components/agent-"+agentID)},
		// 宿主存储路径（kurrent.yaml spec.agent.diskDir 权威，同步为 compose 挂载源插值键）
		{"KURRENT_DISK_DIR", nodeValue(&root, "spec", "agent", "diskDir")},
	}
	if backend == "nvmet" {
		entries = append(entries,
			envEntry{"KURRENT_NVMET_PKI_HOST", filepath.Join(projectRoot, "control_plane/state/pki/components/nvmet-"+agentID)},
		)
	}
	if err := upsertEnv(filepath.Join(o.dir, ".env"), entries); err != nil {
		return err
	}

	fmt.Printf("==> kurrent join: agent=%s backend=%s cp=%s\n", agentID, backend, cpURL)
	fmt.Printf("==> kurrent join: node configuration -> %s\n", target)
	fmt.Printf("==> kurrent join: bootstrap token -> %s (nvmet-host.token is derived automatically from agent enroll)\n", tokenFile)
	fmt.Printf("==> kurrent join: starting %s (compose)\n", composeDir)
	if err := composeRun(composeDir, "--env-file", "../.env", "up", "-d"); err != nil {
		return err
	}
	fmt.Println("==> kurrent join: restarting storager-agent (applying the declaration and token)")
	if err := composeRun(composeDir, "--env-file", "../.env", "restart", "storager-agent"); err != nil {
		return err
	}
	if !waitContainerRunning("storager-agent", agentStartupWatch) {
		return fmt.Errorf("storager-agent did not stay running after start; check docker logs storager-agent")
	}
	if backend == "nvmet" {
		nvmetTokenPath := filepath.Join(bootstrapDir, "nvmet-host.token")
		fmt.Println("==> kurrent join: waiting for agent enroll -> nvmet-host bootstrap token")
		if err := nvmetTokenWaiter(nvmetTokenPath, nvmetTokenWaitTimeout, nvmetTokenPollInterval); err != nil {
			return fmt.Errorf(
				"agent did not complete enroll: %v; check docker logs storager-agent, then after it writes %s run "+
					"docker compose --env-file ../.env restart nvmet-host", err, nvmetTokenPath)
		}
		fmt.Println("==> kurrent join: restarting nvmet-host (consuming the derived token)")
		if err := composeRun(composeDir, "--env-file", "../.env", "restart", "nvmet-host"); err != nil {
			return err
		}
	}
	fmt.Println("==> kurrent join: done. On first start the containers bootstrap their certificates automatically;")
	fmt.Println("    verify from the control plane: kurrent agents list (or the WebUI Agents page, health=ok)")
	return nil
}

// ── 宿主前置预检（nvmet 数据面）──

const tlshdMinVersion = "1.5.0"

// hostEnv 宿主前置检查的只读路径根（沙盒测试注入替换）。
type hostEnv struct {
	procMounts    string                 // /proc/mounts（configfs 挂载检测）
	sysModuleRoot string                 // /sys/module（内核模块加载检测）
	tlshdVersion  func() (string, error) // 宿主 tlshd 版本（含服务运行状态）；nil = 真实探测
}

var hostEnvPaths = hostEnv{procMounts: "/proc/mounts", sysModuleRoot: "/sys/module"}

// hostPreflightNvmet 检查宿主 nvmet 数据面前置：nvmet/nvmet-tcp 模块已加载、configfs 已挂载、
// tlshd 版本 >= 1.5.0 且服务运行中。内核 nvmet target 无法容器化（4421 数据面在宿主），
// 这些是 nvmet 后端节点的手工步骤——已由 nvmeof/host-prepare.sh 工具化；未就绪时
// fail fast 并指路该脚本。
func hostPreflightNvmet(storagerDir string) error {
	var missing []string
	for _, m := range []string{"nvmet", "nvmet_tcp"} { // /sys/module 用下划线名（modprobe 名 nvmet-tcp）
		if _, err := os.Stat(filepath.Join(hostEnvPaths.sysModuleRoot, m)); err != nil {
			missing = append(missing, "kernel module not loaded: "+strings.ReplaceAll(m, "_", "-"))
		}
	}
	if !configfsMounted() {
		missing = append(missing, "configfs not mounted: /sys/kernel/config")
	}
	tlshdBad := false
	ver, err := hostEnvPaths.tlshd()
	switch {
	case err != nil:
		tlshdBad = true
		missing = append(missing, "tlshd unavailable: "+err.Error())
	case !versionAtLeast(ver, tlshdMinVersion):
		tlshdBad = true
		missing = append(missing, fmt.Sprintf("tlshd version too old: %s (requires >= %s; 1.5.0+ prefers the process keyring, older versions mask new keys after a PSK rotation)",
			ver, tlshdMinVersion))
	}
	if len(missing) == 0 {
		return nil
	}
	hint := fmt.Sprintf("fix (root required, idempotent): sudo bash %s", filepath.Join(storagerDir, "nvmeof", "host-prepare.sh"))
	if tlshdBad {
		hint += fmt.Sprintf("\n  tlshd: install/upgrade ktls-utils >= %s and run sudo systemctl enable --now tlshd (build steps: %s)",
			tlshdMinVersion, filepath.Join(storagerDir, "nvmeof", "nvmet-host", "README.md"))
	}
	return fmt.Errorf("host nvmet prerequisites are not ready (the storage data plane cannot start):\n  %s\n  %s",
		strings.Join(missing, "\n  "), hint)
}

// tlshd 真实探测：优先取 systemd 单元实际执行的二进制（发行版包与源码安装并存时
// 以运行中的那份为准），版本从 `tlshd -v` 的 stderr 解析。
func (e hostEnv) tlshd() (string, error) {
	if e.tlshdVersion != nil {
		return e.tlshdVersion()
	}
	return detectTlshdVersion()
}

func detectTlshdVersion() (string, error) {
	bin := tlshdBinaryPath()
	if bin == "" {
		return "", fmt.Errorf("not installed (ktls-utils >= %s required)", tlshdMinVersion)
	}
	out, err := exec.Command(bin, "-v").CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("%s -v failed: %v", bin, err)
	}
	ver, err := parseTlshdVersion(string(out))
	if err != nil {
		return "", err
	}
	// 单元存在时服务必须在运行（tlshd 常驻承接内核 TLS 握手 upcall）；
	// 无单元的自定义部署不在此判定。
	if state, err := exec.Command("systemctl", "show", "-p", "LoadState", "--value", "tlshd").Output(); err == nil {
		switch strings.TrimSpace(string(state)) {
		case "loaded":
			if err := exec.Command("systemctl", "is-active", "--quiet", "tlshd").Run(); err != nil {
				return "", fmt.Errorf("service not running (run sudo systemctl enable --now tlshd)")
			}
		case "masked":
			return "", fmt.Errorf("tlshd.service is masked")
		}
	}
	return ver, nil
}

func tlshdBinaryPath() string {
	if out, err := exec.Command("systemctl", "show", "-p", "ExecStart", "--value", "tlshd").Output(); err == nil {
		if p := parseExecStartPath(string(out)); p != "" {
			if _, statErr := os.Stat(p); statErr == nil {
				return p
			}
		}
	}
	if p, err := exec.LookPath("tlshd"); err == nil {
		return p
	}
	return ""
}

var execStartPathRe = regexp.MustCompile(`path=([^ ;}]+)`)

func parseExecStartPath(out string) string {
	if m := execStartPathRe.FindStringSubmatch(out); m != nil {
		return m[1]
	}
	return ""
}

var tlshdVersionRe = regexp.MustCompile(`ktls-utils\s+([0-9]+(?:\.[0-9]+){1,3})`)

func parseTlshdVersion(out string) (string, error) {
	if m := tlshdVersionRe.FindStringSubmatch(out); m != nil {
		return m[1], nil
	}
	return "", fmt.Errorf("cannot parse tlshd version output: %q", strings.TrimSpace(out))
}

func versionAtLeast(v, min string) bool {
	a, b := strings.Split(v, "."), strings.Split(min, ".")
	for i := range b {
		x, y := 0, 0
		if i < len(a) {
			x, _ = strconv.Atoi(a[i])
		}
		y, _ = strconv.Atoi(b[i])
		if x != y {
			return x > y
		}
	}
	return true
}

// configfsMounted /proc/mounts 中查找 configfs 挂载点（挂载即内核模块已就位）。
func configfsMounted() bool {
	data, err := os.ReadFile(hostEnvPaths.procMounts)
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) >= 3 && fields[1] == "/sys/kernel/config" && fields[2] == "configfs" {
			return true
		}
	}
	return false
}

// newNodeID 生成节点身份（agent_id）：12 位 hex 随机标识（与盘 os_tag 同构，docker
// 容器 ID 短形式）——机器身份不承载人类语义，人类识别用 metadata.remark。
func newNodeID() (string, error) {
	b := make([]byte, 6)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("generate node identity: %w", err)
	}
	return hex.EncodeToString(b), nil
}

// newNodeDeclaration 缺省节点声明（kurrent.yaml 缺失时 join 自动生成，权威声明——
// 后续可手工编辑或 kurrent config print node-defaults 预置模板）。
// 分层职责：容器内路径/监听属 compose 职责；token 在独立凭据文件；
// diskDir = 宿主存储路径（默认仓库 storager_img，与 compose 一致）。
// 用 yaml.Marshal 而非字符串拼接：路径里的 #、:、引号不会破坏文档结构。
func newNodeDeclaration(agentID, cpURL, diskDir string) ([]byte, error) {
	decl := map[string]any{
		"apiVersion": "kurrent.io/v1",
		"kind":       "NodeConfiguration",
		"metadata": map[string]any{
			"name":   agentID,
			"remark": "",
		},
		"spec": map[string]any{
			"agent": map[string]any{
				"backend": "nvmet",
				"diskDir": diskDir,
				"nqnBase": "nqn.2026-07.com.kurrent",
			},
			"controlPlane": map[string]any{"url": cpURL},
		},
	}
	out, err := yaml.Marshal(decl)
	if err != nil {
		return nil, err
	}
	header := "# Node declaration (generated by kurrent join from the default template; remark is a human-readable note)\n"
	return append([]byte(header), out...), nil
}

// ── kurrent.yaml 读/改/写（yaml.v3 Node 树，保留未知字段）──

// rootMapping 返回文档根 mapping 节点。
func rootMapping(root *yaml.Node) *yaml.Node {
	if root.Kind == yaml.DocumentNode && len(root.Content) > 0 && root.Content[0].Kind == yaml.MappingNode {
		return root.Content[0]
	}
	return root
}

// mapValue 返回 mapping 中键对应的值节点（无则 nil）。
func mapValue(m *yaml.Node, key string) *yaml.Node {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			return m.Content[i+1]
		}
	}
	return nil
}

// setPath 更新 mapping 路径上的标量值；缺失节点自动创建，其余字段原样保留（非 forbid 式合并）。
func setPath(root *yaml.Node, value string, path ...string) {
	m := rootMapping(root)
	for _, key := range path[:len(path)-1] {
		m = childMapping(m, key)
	}
	last := path[len(path)-1]
	if v := mapValue(m, last); v != nil {
		v.Kind = yaml.ScalarNode
		v.Tag = "!!str"
		v.Value = value
		v.Content = nil
		return
	}
	m.Content = append(m.Content,
		&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: last},
		&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: value})
}

// childMapping 返回（或创建）mapping 键对应的子 mapping。
func childMapping(m *yaml.Node, key string) *yaml.Node {
	if v := mapValue(m, key); v != nil {
		if v.Kind != yaml.MappingNode {
			v.Kind = yaml.MappingNode
			v.Tag = "!!map"
			v.Content = nil
		}
		return v
	}
	kn := &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: key}
	vn := &yaml.Node{Kind: yaml.MappingNode, Tag: "!!map"}
	m.Content = append(m.Content, kn, vn)
	return vn
}

// nodeValue 取 mapping 路径的标量值（路径缺失返回空串）。
func nodeValue(root *yaml.Node, path ...string) string {
	cur := rootMapping(root)
	for i, key := range path {
		if cur.Kind != yaml.MappingNode {
			return ""
		}
		v := mapValue(cur, key)
		if v == nil {
			return ""
		}
		if i == len(path)-1 {
			return v.Value
		}
		cur = v
	}
	return ""
}

// ── .env 插值键维护 ──

type envEntry struct{ key, value string }

// migratedEnvKeys 已迁移到 kurrent.yaml 的业务键：.env 收敛时清空（compose 不再注入）。
// KURRENT_DISK_DIR 除外——它是 compose 挂载源插值键（yml spec.agent.diskDir 同步值），保留在 .env。
var migratedEnvKeys = []string{
	"KURRENT_AGENT_ID", "KURRENT_COMPONENT", "KURRENT_PKI_DIR", "KURRENT_CP_ENROLL_URL",
	"KURRENT_CP_CA", "KURRENT_BOOTSTRAP_TOKEN", "KURRENT_BOOTSTRAP_TOKEN_NVMET",
	"KURRENT_ADVERTISE_URL", "KURRENT_BACKEND", "KURRENT_NQN_BASE",
	"KURRENT_LOG_FILE", "KURRENT_NVMET_HOST_URL", "KURRENT_NVMET_CACHE_FILE",
	"KURRENT_ISCSI_CONTAINER",
}

// envValue 读 .env 中最后一个匹配键的值（无则空；迁移期业务键兜底）。
func envValue(path, key string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	prefix := key + "="
	val := ""
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if line := sc.Text(); strings.HasPrefix(line, prefix) {
			val = strings.TrimPrefix(line, prefix)
		}
	}
	return val
}

// upsertEnv 幂等更新 .env：删除已迁移业务键与本次条目后追加（键唯一，重复 join 不堆积）。
func upsertEnv(path string, entries []envEntry) error {
	var lines []string
	if data, err := os.ReadFile(path); err == nil {
		drop := map[string]bool{}
		for _, e := range entries {
			drop[e.key] = true
		}
		for _, k := range migratedEnvKeys {
			drop[k] = true
		}
		for _, line := range strings.Split(string(data), "\n") {
			key := line
			if i := strings.IndexByte(key, '='); i >= 0 {
				key = key[:i]
			}
			if !drop[key] {
				lines = append(lines, line)
			}
		}
	}
	for len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	for _, e := range entries {
		lines = append(lines, e.key+"="+e.value)
	}
	if err := writeFileAtomic(path, []byte(strings.Join(lines, "\n")+"\n"), 0o644); err != nil {
		return fmt.Errorf("write %s: %w", path, err)
	}
	return nil
}
