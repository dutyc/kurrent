// join 子命令：存储节点加入控制面并收敛到运行态（配置即声明）。
// 命令携带控制面地址——签发命令输出即带地址，节点粘贴执行即获得地址，
// 无需预编辑任何文件：kurrent join <cp-url> [--token T] [--agent-id ID]。
// 节点声明 storager/kurrent.yaml 缺失时按模板自动生成（url=命令地址、backend nvmet、
// advertiseUrl 推导、diskDir/nqnBase 默认），已存在则读入并把地址同步进声明
// （非 forbid 式合并，未知字段与手工编辑保留；yml 即 kurrent.yaml，后续可编辑）。
// 节点身份不进声明：--agent-id（`kurrent token create` 打印）或既有节点状态
// agent.id 提供，join 只校验并原子落盘，绝不生成。节点状态、引导凭据与组件 PKI
// 放在状态根（<state>/storager）而非仓库：移动/改名仓库不影响节点身份。
// 引导凭据落盘（--token 或既有 bootstrap/agent.token）→ compose 插值变量经进程环境注入
// → 收敛启动 agent（docker compose 为内部编排细节，重跑幂等）。nvmet 后端先预检宿主前置
// （内核模块 nvmet/nvmet-tcp + configfs 挂载 + tlshd 版本），未就绪指路
// nvmeof/host-prepare.sh。
package main

import (
	"crypto/rand"
	"crypto/x509"
	"encoding/hex"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
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
		{name: "state-dir", kind: flagString},
		{name: "cp-ca", kind: flagString},
		{name: "adopt-state", kind: flagBool},
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
	for _, flag := range []string{"agent-id", "config", "token", "dir", "state-dir", "cp-ca"} {
		if p.has(flag) && strings.TrimSpace(p.opt(flag, "")) == "" {
			usageError(joinUsage, fmt.Errorf("--%s must not be empty", flag))
		}
	}
	agentID := strings.TrimSpace(p.opt("agent-id", ""))
	if agentID != "" && !validAgentID(agentID) {
		usageError(joinUsage, fmt.Errorf("--agent-id must be 12 lowercase hex characters (printed by `kurrent token create`): %q", agentID))
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
		stateDir: p.opt("state-dir", ""), cpCA: p.opt("cp-ca", ""),
		adoptState: p.bool("adopt-state"),
	}); err != nil {
		fatal("%v", err)
	}
}

type joinOpts struct {
	dir        string // storager 目录
	configFile string // 节点声明文件（缺省 <dir>/kurrent.yaml；其他路径 = 应用为节点配置）
	cpURL      string // 控制面 HTTPS 入口（必填）
	token      string // 引导凭据（可选：给则写入 agent.token；不给要求文件已就位）
	agentID    string // 节点身份（首跑必填；重跑可省略，从状态文件读取并校验一致）
	stateDir   string // 状态根（缺省：KURRENT_STATE_DIR → 部署记录 → ~/.local/state/kurrent）
	cpCA       string // 控制面信任锚（缺省：同机控制面 state/certs/server.crt → 已有节点副本）
	adoptState bool   // 迁移仓库内旧节点状态
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

// resolveStoragerDir 定位 storager 目录：--dir 优先（也接受仓库根）；否则部署记录
// 指向的仓库；否则 cwd 下 storager/；cwd 已是 storager 时用 cwd。
func resolveStoragerDir(explicit string) (string, error) {
	if explicit != "" {
		abs, err := filepath.Abs(explicit)
		if err != nil {
			return "", fmt.Errorf("resolve --dir: %w", err)
		}
		if isDir(filepath.Join(abs, "storager")) {
			return filepath.Join(abs, "storager"), nil
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
	if rec, err := readDeploymentRecord(); err == nil && rec != nil {
		if isDir(filepath.Join(rec.RepoRoot, "storager")) {
			return filepath.Join(rec.RepoRoot, "storager"), nil
		}
	}
	return "", fmt.Errorf("storager directory not found (run from the repository root or pass --dir)")
}

var errAgentIDRequired = fmt.Errorf("missing node identity: pass --agent-id <id> from the command printed by " +
	"`kurrent token create` (the control plane allocates it; join never generates one); " +
	"on reruns it is read from <state>/storager/state/agent.id")

var agentIDRe = regexp.MustCompile(`^[0-9a-f]{12}$`)

// validAgentID 节点身份（agent_id）只接受 12 位小写 hex：证书 CN、控制面登记键与
// 数据面随机标识同构；描述性名称是控制面台账里的昵称，不进入节点文件。
func validAgentID(id string) bool {
	return agentIDRe.MatchString(id)
}

// readNodeID 读取已有身份状态文件：缺失返回 ("", false, nil)；内容非法直接报错。
func readNodeID(path string) (string, bool, error) {
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return "", false, nil
	}
	if err != nil {
		return "", false, fmt.Errorf("read %s: %w", path, err)
	}
	id := strings.TrimSpace(string(data))
	if id == "" {
		return "", false, fmt.Errorf("%s is empty; delete it and pass --agent-id from `kurrent token create`", path)
	}
	if !validAgentID(id) {
		return "", false, fmt.Errorf("%s: invalid node identity %q (expected 12 lowercase hex characters)", path, id)
	}
	return id, true, nil
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
	q, err := resolveDeployment(o.dir, o.stateDir)
	if err != nil {
		return err
	}
	if o.agentID != "" && !validAgentID(o.agentID) {
		return fmt.Errorf("--agent-id must be 12 lowercase hex characters (printed by `kurrent token create`): %s", o.agentID)
	}
	repoRoot := q.repoRoot

	// 节点声明：kurrent.yaml 缺失时按模板自动生成（无预置文件时用缺省模板：
	// 生成后即权威声明，可后续手工编辑；--config 指向其他路径 = 应用为节点配置）
	data, err := os.ReadFile(o.configFile)
	var root yaml.Node
	if err != nil {
		if !os.IsNotExist(err) {
			return fmt.Errorf("read %s: %w", o.configFile, err)
		}
		declaration, err := newNodeDeclaration(cpURL, filepath.Join(repoRoot, "storager_img"))
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
	if mapValue(rootMapping(&root), "metadata") != nil {
		return fmt.Errorf("%s: metadata is no longer part of the node declaration; the node identity lives in <state>/storager/state/agent.id and the nickname in the control-plane ledger -- remove the metadata block", o.configFile)
	}

	// 旧布局（仓库内 state/bootstrap/agent/logs）先迁移：--adopt-state 显式启停旧栈、
	// 复制并校验后再继续；否则拒绝，避免节点带着两套状态各走各路。
	legacy := detectLegacyStorageState(o.dir)
	if legacy.present() && !o.adoptState {
		return fmt.Errorf("node state from the old layout is present in the checkout:\n  %s\n"+
			"rerun with --adopt-state to migrate it into %s (the old stack is stopped and the copies removed after verification)",
			strings.Join(legacy.paths, "\n  "), q.stateDir)
	}
	if o.adoptState && legacy.present() {
		if err := assertLegacyOwnership(legacy.paths); err != nil {
			return err
		}
		if err := assertStateTargetEmpty(q.stateDir); err != nil {
			return err
		}
		fmt.Println("==> kurrent join: stopping the legacy storage stack (state migration)")
		stopLegacyStorage(q, o.dir)
		if err := adoptStorageState(o.dir, q); err != nil {
			return fmt.Errorf("migrate legacy node state: %w", err)
		}
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
		setPath(&root, filepath.Join(repoRoot, "storager_img"), "spec", "agent", "diskDir")
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

	// 节点状态根准备：身份、引导凭据、组件 PKI、日志与 CP 信任锚都在仓库之外。
	if err := prepareStoragerState(q.storager("")); err != nil {
		return fmt.Errorf("prepare the node state root: %w", err)
	}

	// 节点身份：只来自 --agent-id 或既有状态文件，join 不生成。首跑缺身份直接失败；
	// 重跑以状态文件为准，--agent-id 只做一致性校验（身份必须稳定）。
	stateFile := filepath.Join(q.storager("").state, "agent.id")
	existingID, hasState, err := readNodeID(stateFile)
	if err != nil {
		return err
	}
	agentID := existingID
	if o.agentID != "" {
		if hasState && existingID != o.agentID {
			return fmt.Errorf("%s=%s does not match --agent-id=%s (the node identity must stay stable; do not change it midway)",
				stateFile, existingID, o.agentID)
		}
		agentID = o.agentID
	} else if !hasState {
		return errAgentIDRequired
	}
	nodePaths := q.storager(agentID)
	if err := prepareStoragerState(nodePaths); err != nil {
		return fmt.Errorf("prepare the node state root: %w", err)
	}

	// 引导凭据先确认可用再改任何文件（不给 --token 时要求文件已就位）。
	tokenFile := filepath.Join(nodePaths.bootstrap, "agent.token")
	tokenProvided := strings.TrimSpace(o.token) != ""
	if !tokenProvided {
		if data, err := os.ReadFile(tokenFile); err != nil || strings.TrimSpace(string(data)) == "" {
			return fmt.Errorf("missing bootstrap token: %s does not exist or is empty -- pass it with kurrent join --token <token> (issued by kurrent token create)",
				tokenFile)
		}
	}

	// 控制面信任锚：--cp-ca ＞ 同机控制面 state/certs/server.crt ＞ 已有节点副本。
	cpCA, err := resolveNodeCPCA(o.cpCA, q, nodePaths)
	if err != nil {
		return err
	}

	// 盘目录是 compose 的 bind 挂载源：存在性兜底创建（非致命——docker 起服时也会建，
	// 但可能归 root 所有；无权限时告警，提示先手工建好）
	diskDir := nodeValue(&root, "spec", "agent", "diskDir")
	if diskDir != "" {
		if err := os.MkdirAll(diskDir, 0o755); err != nil {
			fmt.Fprintf(os.Stderr, "kurrent: warning: cannot create disk directory (%s): %v -- create it first and make sure it is writable\n", diskDir, err)
		}
		if rel, err := filepath.Rel(repoRoot, diskDir); err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
			fmt.Fprintf(os.Stderr, "kurrent: warning: spec.agent.diskDir is inside the checkout (%s); moving the repository moves the disk images -- set an absolute path outside the repository if that is not intended\n", diskDir)
		}
	}

	// 写回节点配置（非 forbid 式合并：未知/手工编辑字段原样保留；kurrent.yaml 即声明）
	out, err := yaml.Marshal(&root)
	if err != nil {
		return fmt.Errorf("serialize %s: %w", target, err)
	}
	// 写盘前用 agent 镜像内同一份 schema 全量校验：未知字段/非法值当场失败，
	// 不会等容器启动才崩溃（合并保留的未知字段按 extra="forbid" 拒绝）。
	// compose 插值变量只作用于本次子进程：部署目录不再保留派生的 .env；build 同样需要。
	composeEnv := q.nodeComposeEnv(agentID, diskDir, cpCA)
	fmt.Println("==> kurrent join: building the validation image (storager-agent)")
	if err := composeExec(composeDir, q.storageProject(), composeEnv, "build", "storager-agent"); err != nil {
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

	// 节点身份落盘：首跑从 --agent-id 写入，重跑保持原值（已在前面校验一致）。
	if err := writeFileAtomic(stateFile, []byte(agentID+"\n"), 0o600); err != nil {
		return fmt.Errorf("write %s: %w", stateFile, err)
	}

	// 引导凭据文件：--token 给则写入 agent.token（0600）；不给则沿用已确认就位的文件。
	// nvmet-host.token 是派生凭据——agent enroll 上报 backend=nvmet 时控制面签发随
	// 响应下发（绑定 agent_id、不预知后端）。nvmet 后端重跑 join 时清除旧派生文件
	// （迁移清理，将由 agent 引导时重新派生；stgt/lio 后端不产生该文件）
	if tokenProvided {
		// 原子写 + 恒 0600：已有 token 文件权限过宽时也会被收敛
		if err := writeFileAtomic(tokenFile, []byte(strings.TrimSpace(o.token)+"\n"), 0o600); err != nil {
			return fmt.Errorf("write %s: %w", tokenFile, err)
		}
	}
	if backend == "nvmet" {
		if err := os.Remove(filepath.Join(nodePaths.bootstrap, "nvmet-host.token")); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("remove old %s: %w", filepath.Join(nodePaths.bootstrap, "nvmet-host.token"), err)
		}
	}
	if err := writeJoinRecord(q, deployStatusBootstrapping); err != nil {
		return err
	}

	fmt.Printf("==> kurrent join: agent=%s backend=%s cp=%s\n", agentID, backend, cpURL)
	fmt.Printf("==> kurrent join: state root -> %s\n", q.stateDir)
	fmt.Printf("==> kurrent join: node configuration -> %s\n", target)
	fmt.Printf("==> kurrent join: node identity -> %s\n", stateFile)
	fmt.Printf("==> kurrent join: bootstrap token -> %s (nvmet-host.token is derived automatically from agent enroll)\n", tokenFile)
	fmt.Printf("==> kurrent join: control-plane trust anchor -> %s\n", cpCA)
	fmt.Printf("==> kurrent join: starting %s (compose -p %s)\n", composeDir, q.storageProject())
	if err := composeExec(composeDir, q.storageProject(), composeEnv, "up", "-d"); err != nil {
		return err
	}
	fmt.Println("==> kurrent join: restarting storager-agent (applying the declaration and token)")
	if err := composeExec(composeDir, q.storageProject(), composeEnv, "restart", "storager-agent"); err != nil {
		return err
	}
	if !waitContainerRunning("storager-agent", agentStartupWatch) {
		return fmt.Errorf("storager-agent did not stay running after start; check docker logs storager-agent")
	}
	if backend == "nvmet" {
		nvmetTokenPath := filepath.Join(nodePaths.bootstrap, "nvmet-host.token")
		fmt.Println("==> kurrent join: waiting for agent enroll -> nvmet-host bootstrap token")
		if err := nvmetTokenWaiter(nvmetTokenPath, nvmetTokenWaitTimeout, nvmetTokenPollInterval); err != nil {
			return fmt.Errorf(
				"agent did not complete enroll: %v; check docker logs storager-agent, then after it writes %s rerun "+
					"`kurrent join` to restart nvmet-host", err, nvmetTokenPath)
		}
		fmt.Println("==> kurrent join: restarting nvmet-host (consuming the derived token)")
		if err := composeExec(composeDir, q.storageProject(), composeEnv, "restart", "nvmet-host"); err != nil {
			return err
		}
	}
	if err := writeJoinRecord(q, deployStatusReady); err != nil {
		return err
	}
	if o.adoptState && legacy.present() {
		if err := removeLegacyStorageState(o.dir); err != nil {
			return fmt.Errorf("node joined, but removing the legacy state failed: %w", err)
		}
		fmt.Println("==> kurrent join: legacy in-repository node state removed")
	}
	fmt.Println("==> kurrent join: done. On first start the containers bootstrap their certificates automatically;")
	fmt.Println("    verify from the control plane: kurrent agents list (or the WebUI Agents page, health=ok)")
	fmt.Println("    host baseline (read-only): sudo kurrent host check --role storage")
	return nil
}

// resolveNodeCPCA 解析控制面信任锚并原子复制到节点状态根：
// --cp-ca ＞ 同机控制面 <state>/control-plane/state/certs/server.crt ＞ 已有节点副本。
// 分机部署且两者都不在时给出可执行的复制指令，而不是让 enroll 在 TLS 阶段静默失败。
func resolveNodeCPCA(explicit string, q deployQuery, paths storagerPaths) (string, error) {
	copyIn := func(src string) (string, error) {
		abs, err := filepath.Abs(src)
		if err != nil {
			return "", fmt.Errorf("resolve control-plane trust anchor: %w", err)
		}
		if abs == paths.cpCA {
			return paths.cpCA, nil
		}
		data, err := os.ReadFile(abs)
		if err != nil {
			return "", fmt.Errorf("read control-plane trust anchor %s: %w", abs, err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(data) {
			return "", fmt.Errorf("%s contains no valid certificate", abs)
		}
		if err := os.MkdirAll(paths.certs, 0o700); err != nil {
			return "", fmt.Errorf("create %s: %w", paths.certs, err)
		}
		if err := writeFileAtomic(paths.cpCA, data, 0o644); err != nil {
			return "", fmt.Errorf("write %s: %w", paths.cpCA, err)
		}
		return paths.cpCA, nil
	}
	if explicit != "" {
		return copyIn(explicit)
	}
	coLocated := q.controlPlane().serverCert
	if fileExists(coLocated) {
		return copyIn(coLocated)
	}
	if fileExists(paths.cpCA) {
		return paths.cpCA, nil
	}
	return "", fmt.Errorf("control-plane trust anchor not found (%s); on a split-node deployment copy it from the control plane:\n"+
		"  scp <cp-host>:%s %s\n  kurrent join <cp-url> --cp-ca %s --token <token>",
		paths.cpCA, coLocated, paths.cpCA, paths.cpCA)
}

// writeJoinRecord 更新部署记录的 storage 角色与状态（同机控制面记录保留 CP 角色）。
func writeJoinRecord(q deployQuery, status string) error {
	rec := deploymentRecord{
		Project:        q.project(),
		StorageProject: q.storageProject(),
		RepoRoot:       q.repoRoot,
		StateDir:       q.stateDir,
		Roles:          deploymentRoles{Storage: true},
		Status:         status,
	}
	if q.record != nil {
		rec.Roles.ControlPlane = q.record.Roles.ControlPlane
		// 同机部署时 join 只更新 storage 角色，控制面的部署状态由 init 维护。
		if rec.Roles.ControlPlane && q.record.Status != "" {
			rec.Status = q.record.Status
		}
	}
	return writeDeploymentRecord(&rec)
}

// stopLegacyStorage 尽力停止旧存储项目（nvmeof / iscsi / 固定存储项目）。
func stopLegacyStorage(q deployQuery, storagerDir string) {
	nodeEnv := q.nodeComposeEnv("unknown", q.repoRoot, q.storager("unknown").cpCA)
	for _, item := range []struct{ dir, project string }{
		{filepath.Join(storagerDir, "nvmeof"), "nvmeof"},
		{filepath.Join(storagerDir, "iscsi"), "iscsi"},
		{filepath.Join(storagerDir, "nvmeof"), q.storageProject()},
		{filepath.Join(storagerDir, "iscsi"), q.storageProject()},
	} {
		composeBestEffort(item.dir, item.project, nodeEnv, "down", "--remove-orphans")
	}
}

// ── 宿主前置预检（nvmet 数据面）──

// hostPreflightNvmet 复用 kurrent host check 的存储节点判定：任一 FAIL 即 fail fast
// 并指路 host-prepare.sh（同一套检查，避免两处判定漂移）。
func hostPreflightNvmet(storagerDir string) error {
	var missing []string
	tlshdBad := false
	for _, c := range storageHostChecks(hostEnvPaths, filepath.Dir(storagerDir)) {
		if c.Status != checkFail {
			continue
		}
		missing = append(missing, c.Detail)
		if c.ID == "storage.tlshd" {
			tlshdBad = true
		}
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
// 节点身份在 state/agent.id；diskDir = 宿主存储路径（默认仓库 storager_img，与 compose 一致）。
// 用 yaml.Marshal 而非字符串拼接：路径里的 #、:、引号不会破坏文档结构。
func newNodeDeclaration(cpURL, diskDir string) ([]byte, error) {
	decl := map[string]any{
		"apiVersion": "kurrent.io/v1",
		"kind":       "NodeConfiguration",
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
	header := "# Node declaration (generated by kurrent join from the default template)\n"
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
