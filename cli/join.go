// join 子命令：存储节点加入控制面并收敛到运行态（配置即声明）。
// 命令携带控制面地址——签发命令输出即带地址，节点粘贴执行即获得地址，
// 无需预编辑任何文件：kurrent join <cp-url> [--token T] [--agent-id ID]。
// 节点声明 storager/kurrent.yaml 缺失时按模板自动生成（url=命令地址、name=随机短标识、
// backend nvmet、advertiseUrl 推导、diskDir/nqnBase 默认），已存在则读入并把地址同步
// 进声明（非 forbid 式合并，未知字段与手工编辑保留；yml 即 kurrent.yaml，后续可编辑）。
// 身份分层：name = 机器身份（bootstrap token 绑定它；--agent-id 写入，未给则 join
// 自动生成随机短标识）；人工输入归 metadata.remark（备注，人类识别用，随首次 enroll
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
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

func cmdJoin(args []string) {
	p := parseArgs(args)
	if len(p.pos) < 1 {
		fatal("用法: kurrent join <cp-url> [--agent-id ID] [--config PATH] [--token T] [--dir PATH]\n" +
			"      <cp-url>: 控制面 HTTPS 入口（kurrent token\n" +
			"      create 签发的命令直接带地址，粘贴即可执行）；\n" +
			"      storager/kurrent.yaml 即节点声明：缺失时 join 按模板自动生成（地址/身份\n" +
			"      标识自决），kurrent config print node-defaults 可预置模板再编辑；\n" +
			"      bootstrap token 绑定节点身份，--agent-id 写入声明 metadata.name\n" +
			"      （token create 输出的命令已携带）；引导凭据 --token 可选（不给则要求\n" +
			"      storager/bootstrap/agent.token 已就位）")
	}
	if len(p.pos) > 1 {
		fatal("多余位置参数: %s（用法: kurrent join <cp-url> [--agent-id ID] [--config PATH] [--token T] [--dir PATH]）", strings.Join(p.pos[1:], " "))
	}
	dir, err := resolveStoragerDir(p.opt("dir", ""))
	if err != nil {
		fatal("%v", err)
	}
	configFile := p.opt("config", filepath.Join(dir, "kurrent.yaml"))
	if !filepath.IsAbs(configFile) {
		if configFile, err = filepath.Abs(configFile); err != nil {
			fatal("解析 --config: %v", err)
		}
	}
	if err := joinRun(joinOpts{
		dir: dir, configFile: configFile, cpURL: p.pos[0],
		token: p.opt("token", ""), agentID: strings.TrimSpace(p.opt("agent-id", "")),
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
			return fmt.Errorf("等待 %s 超时（%s）", path, timeout)
		}
		time.Sleep(interval)
	}
}

// resolveStoragerDir 定位 storager 目录：--dir 优先；否则 cwd 下 storager/；cwd 已是 storager 时用 cwd。
func resolveStoragerDir(explicit string) (string, error) {
	if explicit != "" {
		abs, err := filepath.Abs(explicit)
		if err != nil {
			return "", fmt.Errorf("解析 --dir: %w", err)
		}
		return abs, nil
	}
	cwd, err := os.Getwd()
	if err != nil {
		return "", fmt.Errorf("获取当前目录: %w", err)
	}
	if fi, err := os.Stat(filepath.Join(cwd, "storager")); err == nil && fi.IsDir() {
		return filepath.Join(cwd, "storager"), nil
	}
	if _, err := os.Stat(filepath.Join(cwd, "nvmeof")); err == nil {
		return cwd, nil // 已位于 storager 内
	}
	return "", fmt.Errorf("未找到 storager 目录（请在仓库根运行，或用 --dir 指定）")
}

var cpHostRe = regexp.MustCompile(`https?://([^/:]+)`)

var errAgentIDRequired = fmt.Errorf("bootstrap token 绑定节点身份：请用 --agent-id <id> 指定" +
	"（kurrent token create --agent-id 输出的命令已携带；id 须与签发时的绑定一致）")

// normalizeAgentID agent_id 字符集规范化（校验用）：小写，非字母数字/点/横线字符替换为
// '-'，保留 '.'——证书 CN 与控制面登记键仅用该字符集。身份由 join 自动生成，显式声明值
// 经本函数归一后必须与原值一致，否则拒绝。
func normalizeAgentID(hostname string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(strings.TrimSpace(hostname)) {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '.', r == '-':
			b.WriteRune(r)
		default:
			b.WriteRune('-')
		}
	}
	return b.String()
}

// joinRun 校验/补默认节点声明并收敛启动 agent（测试直接调用，不经 cmdJoin）。
func joinRun(o joinOpts) error {
	target := filepath.Join(o.dir, "kurrent.yaml")
	// 控制面入口来自命令位置参数（必填）：
	// 签发命令输出直接携带，节点执行即获得；同步为声明 spec.controlPlane.url
	cpURL := strings.TrimRight(strings.TrimSpace(o.cpURL), "/")
	if cpURL == "" {
		return fmt.Errorf("cp-url 必填（kurrent join <cp-url> [--token T]）")
	}
	m := cpHostRe.FindStringSubmatch(cpURL)
	if len(m) < 2 {
		return fmt.Errorf("无法从 cp-url 解析主机名: %s", o.cpURL)
	}

	// 节点声明：kurrent.yaml 缺失时按模板自动生成（无预置文件时用缺省模板：
	// 生成后即权威声明，可后续手工编辑；--config 指向其他路径 = 应用为节点配置）
	if o.agentID != "" && normalizeAgentID(o.agentID) != o.agentID {
		return fmt.Errorf("--agent-id 含非法字符（字符集仅允许小写字母/数字/./-）: %s", o.agentID)
	}
	data, err := os.ReadFile(o.configFile)
	var root yaml.Node
	if err != nil {
		if !os.IsNotExist(err) {
			return fmt.Errorf("读取 %s: %w", o.configFile, err)
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
		if err := yaml.Unmarshal([]byte(newNodeDeclaration(nodeID, cpURL,
			filepath.Join(filepath.Dir(o.dir), "storager_img"))), &root); err != nil {
			return fmt.Errorf("解析配置模板: %w", err)
		}
		fmt.Printf("==> kurrent join: no %s — generated from defaults\n", o.configFile)
	} else if err := yaml.Unmarshal(data, &root); err != nil {
		return fmt.Errorf("解析 %s: %w", o.configFile, err)
	}
	if kind := nodeValue(&root, "kind"); kind != "NodeConfiguration" {
		return fmt.Errorf("%s: kind = %q, want NodeConfiguration", o.configFile, kind)
	}

	// agent_id：机器身份——bootstrap token 绑定的就是这个 id。--agent-id 写入声明
	// （与已声明值不一致即拒绝，身份须稳定）；声明留空且未给 --agent-id 时自动生成
	// 随机短标识（12 位 hex，与盘 os_tag 同构、docker 容器 ID 短形式）；给了 --token
	// 却无身份则拒绝（随机 id 必然与 token 绑定不符，提前报错胜过 enroll 401）。
	agentID := strings.TrimSpace(nodeValue(&root, "metadata", "name"))
	if o.agentID != "" {
		if agentID != "" && agentID != o.agentID {
			return fmt.Errorf("%s: metadata.name=%s 与 --agent-id=%s 不一致（节点身份须稳定，勿中途改名）",
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
	} else if normalizeAgentID(agentID) != agentID {
		return fmt.Errorf("%s: metadata.name 含非法字符（机器身份，由 join 自动生成、勿手工输入——人工输入请写入 metadata.remark；字符集仅允许小写字母/数字/./-）",
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
		return fmt.Errorf("%s: spec.agent.backend 非法: %s（nvmet|stgt|lio）", o.configFile, backend)
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
			fmt.Fprintf(os.Stderr, "kurrent: 警告: 盘目录不可建（%s）: %v——请先创建并确保可写\n", diskDir, err)
		}
	}
	if strings.TrimSpace(nodeValue(&root, "spec", "agent", "nqnBase")) == "" {
		setPath(&root, "nqn.2026-07.com.kurrent", "spec", "agent", "nqnBase")
	}
	// advertise：声明留空 → 推导 https://<cp-host>:4840；特殊场景（NAT 等）编辑
	// kurrent.yaml 的 spec.agent.advertiseUrl 覆盖
	if strings.TrimSpace(nodeValue(&root, "spec", "agent", "advertiseUrl")) == "" {
		setPath(&root, "https://"+m[1]+":4840", "spec", "agent", "advertiseUrl")
	}

	// 写回节点配置（非 forbid 式合并：未知/手工编辑字段原样保留；kurrent.yaml 即声明）
	out, err := yaml.Marshal(&root)
	if err != nil {
		return fmt.Errorf("序列化 %s: %w", target, err)
	}
	// 写盘前用 agent 镜像内同一份 schema 全量校验：未知字段/非法值当场失败，
	// 不会等容器启动才崩溃（合并保留的未知字段按 extra="forbid" 拒绝）。
	fmt.Println("==> kurrent join: building validation image (storager-agent)")
	if err := composeRun(composeDir, "build", "storager-agent"); err != nil {
		return fmt.Errorf("构建 storager-agent 镜像失败: %w", err)
	}
	summary, err := runConfigTool("storager-agent", "validate", out)
	if err != nil {
		return fmt.Errorf("%s 声明校验失败（未写入）:\n%s", target, err)
	}
	fmt.Printf("==> kurrent join: %s\n", strings.TrimSpace(summary))
	if err := os.WriteFile(target, out, 0o644); err != nil {
		return fmt.Errorf("写入 %s: %w", target, err)
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
		return fmt.Errorf("创建 %s: %w", bootstrapDir, err)
	}
	tokenFile := filepath.Join(bootstrapDir, "agent.token")
	if strings.TrimSpace(o.token) != "" {
		if err := os.WriteFile(tokenFile, []byte(strings.TrimSpace(o.token)+"\n"), 0o600); err != nil {
			return fmt.Errorf("写入 %s: %w", tokenFile, err)
		}
	} else if data, err := os.ReadFile(tokenFile); err != nil || strings.TrimSpace(string(data)) == "" {
		return fmt.Errorf("bootstrap token missing: %s 不存在或为空——kurrent join --token <token> 传入（kurrent token create --agent-id %s 签发）",
			tokenFile, agentID)
	}
	if backend == "nvmet" {
		if err := os.Remove(filepath.Join(bootstrapDir, "nvmet-host.token")); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("清除旧 %s: %w", filepath.Join(bootstrapDir, "nvmet-host.token"), err)
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
	fmt.Printf("==> kurrent join: bootstrap token -> %s（nvmet-host.token 由 agent enroll 派生自动生成）\n", tokenFile)
	fmt.Printf("==> kurrent join: starting %s (compose)\n", composeDir)
	if err := composeRun(composeDir, "--env-file", "../.env", "up", "-d"); err != nil {
		return err
	}
	fmt.Println("==> kurrent join: restarting storager-agent (apply declaration & token)")
	if err := composeRun(composeDir, "--env-file", "../.env", "restart", "storager-agent"); err != nil {
		return err
	}
	if !waitContainerRunning("storager-agent", agentStartupWatch) {
		return fmt.Errorf("storager-agent 启动后未保持运行；docker logs storager-agent 查看原因")
	}
	if backend == "nvmet" {
		nvmetTokenPath := filepath.Join(bootstrapDir, "nvmet-host.token")
		fmt.Println("==> kurrent join: waiting for agent enroll -> nvmet-host bootstrap token")
		if err := nvmetTokenWaiter(nvmetTokenPath, nvmetTokenWaitTimeout, nvmetTokenPollInterval); err != nil {
			return fmt.Errorf(
				"agent did not complete enroll: %v；检查 docker logs storager-agent，待其写出 %s 后执行 "+
					"docker compose --env-file ../.env restart nvmet-host", err, nvmetTokenPath)
		}
		fmt.Println("==> kurrent join: restarting nvmet-host (consumes derived token)")
		if err := composeRun(composeDir, "--env-file", "../.env", "restart", "nvmet-host"); err != nil {
			return err
		}
	}
	fmt.Println("==> kurrent join: done. 容器首次启动会自动引导证书；")
	fmt.Println("    控制面侧验证: kurrent agents list（或 WebUI「Agent 列表」看 health=ok）")
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
			missing = append(missing, "内核模块未加载: "+strings.ReplaceAll(m, "_", "-"))
		}
	}
	if !configfsMounted() {
		missing = append(missing, "configfs 未挂载: /sys/kernel/config")
	}
	tlshdBad := false
	ver, err := hostEnvPaths.tlshd()
	switch {
	case err != nil:
		tlshdBad = true
		missing = append(missing, "tlshd 不可用: "+err.Error())
	case !versionAtLeast(ver, tlshdMinVersion):
		tlshdBad = true
		missing = append(missing, fmt.Sprintf("tlshd 版本过低: %s（需 >= %s；1.5.0 起进程 keyring 优先，旧版会在 PSK 轮换后遮蔽新密钥）",
			ver, tlshdMinVersion))
	}
	if len(missing) == 0 {
		return nil
	}
	hint := fmt.Sprintf("修复（需 root，幂等）: sudo bash %s", filepath.Join(storagerDir, "nvmeof", "host-prepare.sh"))
	if tlshdBad {
		hint += fmt.Sprintf("\n  tlshd: 安装/升级 ktls-utils >= %s 并 sudo systemctl enable --now tlshd（构建步骤见 %s）",
			tlshdMinVersion, filepath.Join(storagerDir, "nvmeof", "nvmet-host", "README.md"))
	}
	return fmt.Errorf("宿主机 nvmet 前置未就绪（存储数据面无法起服）:\n  %s\n  %s",
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
		return "", fmt.Errorf("未安装（需 ktls-utils >= %s）", tlshdMinVersion)
	}
	out, err := exec.Command(bin, "-v").CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("%s -v 失败: %v", bin, err)
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
				return "", fmt.Errorf("服务未运行（sudo systemctl enable --now tlshd）")
			}
		case "masked":
			return "", fmt.Errorf("tlshd.service 被 mask")
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
	return "", fmt.Errorf("无法解析 tlshd 版本输出: %q", strings.TrimSpace(out))
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
		return "", fmt.Errorf("生成节点身份: %w", err)
	}
	return hex.EncodeToString(b), nil
}

// newNodeDeclaration 缺省节点声明（kurrent.yaml 缺失时 join 自动生成；仅 remark 一行
// 提示注释，权威声明——后续可手工编辑或 kurrent config print node-defaults 预置模板）。
// 分层职责：容器内路径/监听属 compose 职责；token 在独立凭据文件；
// diskDir = 宿主存储路径（默认仓库 storager_img，与 compose 一致）。
func newNodeDeclaration(agentID, cpURL, diskDir string) string {
	return fmt.Sprintf(`apiVersion: kurrent.io/v1
kind: NodeConfiguration
metadata:
  name: %s
  # 备注（可选，人类识别用）：手工输入的唯一字段；随首次 enroll 登记进控制面台账（WebUI 可改）
  remark: ""
spec:
  agent:
    backend: nvmet
    diskDir: %s
    nqnBase: nqn.2026-07.com.kurrent
  controlPlane:
    url: %s
`, agentID, diskDir, cpURL)
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
	if err := os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0o644); err != nil {
		return fmt.Errorf("写入 %s: %w", path, err)
	}
	return nil
}
