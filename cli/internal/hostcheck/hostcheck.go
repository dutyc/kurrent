// host_check.go：宿主机只读基线检查（kurrent host check 与 join 的存储节点预检共用）。
//
// 监听面、Docker 守护进程与防火墙检查对两种角色都生效（端口白名单按角色区分）；
// control 另查控制面容器暴露面与凭据权限，storage 另查内核 nvmet 前置。
// 全部只读，不修改系统状态；无 root 时防火墙类检查标记为 SKIP（--strict 可将其视为失败）。
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
)

type checkStatus string

const (
	checkOK   checkStatus = "ok"
	checkWarn checkStatus = "warn"
	checkFail checkStatus = "fail"
	checkSkip checkStatus = "skip"
)

type hostCheck struct {
	ID     string      `json:"id"`
	Title  string      `json:"title"`
	Status checkStatus `json:"status"`
	Detail string      `json:"detail,omitempty"`
	Hint   string      `json:"hint,omitempty"`
}

type hostSummary struct {
	OK   int `json:"ok"`
	Warn int `json:"warn"`
	Fail int `json:"fail"`
	Skip int `json:"skip"`
}

type hostReport struct {
	Role    string      `json:"role"`
	Strict  bool        `json:"strict"`
	Summary hostSummary `json:"summary"`
	Checks  []hostCheck `json:"checks"`
}

type hostRole string

const (
	roleControl hostRole = "control"
	roleStorage hostRole = "storage"
	roleAll     hostRole = "all"
)

// hostEnv 宿主检查的只读探针。字段为 nil 时回落到真实系统调用；
// 测试按需替换路径、命令输出或权限判定。
type hostEnv struct {
	procMounts        string
	sysModuleRoot     string
	controlStateDir   string
	stateRoot         string
	deployment        *deploymentRecord
	deploymentErr     string
	deploymentChecked bool
	tlshdVersion      func() (string, error)
	uid               func() int
	readFile          func(string) ([]byte, error)
	statFile          func(string) (fs.FileInfo, error)
	glob              func(string) ([]string, error)
	lookPath          func(string) (string, error)
	output            func(string, ...string) (string, error)
}

var hostEnvPaths = hostEnv{procMounts: "/proc/mounts", sysModuleRoot: "/sys/module"}

func (e hostEnv) mountsPath() string {
	if e.procMounts != "" {
		return e.procMounts
	}
	return "/proc/mounts"
}

func (e hostEnv) moduleRoot() string {
	if e.sysModuleRoot != "" {
		return e.sysModuleRoot
	}
	return "/sys/module"
}

func (e hostEnv) stateDir(root string) string {
	if e.controlStateDir != "" {
		return e.controlStateDir
	}
	return filepath.Join(root, "control_plane", "state")
}

func (e hostEnv) out(name string, args ...string) (string, error) {
	if e.output != nil {
		return e.output(name, args...)
	}
	out, err := exec.Command(name, args...).Output()
	return string(out), err
}

func (e hostEnv) hasTool(name string) bool {
	if e.lookPath != nil {
		_, err := e.lookPath(name)
		return err == nil
	}
	_, err := exec.LookPath(name)
	return err == nil
}

func (e hostEnv) uidValue() int {
	if e.uid != nil {
		return e.uid()
	}
	return os.Geteuid()
}

func (e hostEnv) read(path string) ([]byte, error) {
	if e.readFile != nil {
		return e.readFile(path)
	}
	return os.ReadFile(path)
}

func (e hostEnv) stat(path string) (fs.FileInfo, error) {
	if e.statFile != nil {
		return e.statFile(path)
	}
	return os.Stat(path)
}

func (e hostEnv) globFiles(pattern string) []string {
	if e.glob != nil {
		out, _ := e.glob(pattern)
		return out
	}
	out, _ := filepath.Glob(pattern)
	return out
}

func (e hostEnv) tlshd() (string, error) {
	if e.tlshdVersion != nil {
		return e.tlshdVersion()
	}
	return detectTlshdVersion()
}

// ── 角色与检查矩阵 ──

func parseHostRole(v string) (hostRole, error) {
	switch hostRole(v) {
	case roleControl, roleStorage, roleAll:
		return hostRole(v), nil
	}
	return "", fmt.Errorf("invalid role %q (expected control|storage|all)", v)
}

// inferHostRole 按运行中的 compose 服务推断角色；显式 --role 时不调用。
func inferHostRole(env hostEnv) (hostRole, error) {
	if !env.hasTool("docker") {
		return "", fmt.Errorf("cannot infer the host role: docker is not available; pass --role control|storage|all")
	}
	out, err := env.out("docker", "ps", "--format", composePSFormat)
	if err != nil {
		return "", fmt.Errorf("cannot infer the host role: docker ps failed; pass --role control|storage|all")
	}
	control, storage := false, false
	for _, c := range parseComposePS(out) {
		switch c.service {
		case "kurrent-control-plane", "kurrent-dnsmasq", "kurrent-nginx":
			control = true
		case "storager-agent", "storager-iscsi", "nvmet-host":
			storage = true
		}
	}
	switch {
	case control && storage:
		return roleAll, nil
	case control:
		return roleControl, nil
	case storage:
		return roleStorage, nil
	}
	return "", fmt.Errorf("no Kurrent containers are running; pass --role control|storage|all")
}

func runHostChecks(env hostEnv, root string, role hostRole) hostReport {
	report := hostReport{Role: string(role)}
	report.Checks = append(report.Checks,
		listenerCheck(env, role),
		dockerDaemonCheck(env),
	)
	if role == roleControl || role == roleAll {
		report.Checks = append(report.Checks, controlContainerChecks(env)...)
	}
	report.Checks = append(report.Checks, firewallCheck(env))
	if role == roleControl || role == roleAll {
		report.Checks = append(report.Checks, deploymentChecks(env, root)...)
		report.Checks = append(report.Checks, credentialChecks(env, root)...)
	}
	if role == roleStorage || role == roleAll {
		report.Checks = append(report.Checks, storageHostChecks(env, root)...)
	}
	for _, c := range report.Checks {
		switch c.Status {
		case checkOK:
			report.Summary.OK++
		case checkWarn:
			report.Summary.Warn++
		case checkFail:
			report.Summary.Fail++
		case checkSkip:
			report.Summary.Skip++
		}
	}
	return report
}

func (r hostReport) blocks(strict bool) bool {
	return r.Summary.Fail > 0 || (strict && r.Summary.Skip > 0)
}

// ── storage 角色：内核 nvmet 数据面前置 ──

const tlshdMinVersion = "1.5.0"

func storageHostChecks(env hostEnv, root string) []hostCheck {
	prepareHint := "sudo bash " + filepath.Join(root, "storager", "nvmeof", "host-prepare.sh")
	var checks []hostCheck
	for _, mod := range []string{"nvmet", "nvmet_tcp"} {
		name := strings.ReplaceAll(mod, "_", "-")
		c := hostCheck{ID: "storage.module." + name, Title: "kernel module " + name}
		if _, err := env.stat(filepath.Join(env.moduleRoot(), mod)); err != nil {
			c.Status = checkFail
			c.Detail = "kernel module not loaded: " + name
			c.Hint = prepareHint
		} else {
			c.Status = checkOK
			c.Detail = "loaded"
		}
		checks = append(checks, c)
	}

	c := hostCheck{ID: "storage.configfs", Title: "configfs mounted"}
	if configfsMounted(env) {
		c.Status = checkOK
		c.Detail = "configfs mounted at /sys/kernel/config"
	} else {
		c.Status = checkFail
		c.Detail = "configfs not mounted: /sys/kernel/config"
		c.Hint = prepareHint
	}
	checks = append(checks, c)

	c = hostCheck{ID: "storage.tlshd", Title: "tlshd >= " + tlshdMinVersion}
	ver, err := env.tlshd()
	switch {
	case err != nil:
		c.Status = checkFail
		c.Detail = "tlshd unavailable: " + err.Error()
		c.Hint = fmt.Sprintf("install/upgrade ktls-utils >= %s and run sudo systemctl enable --now tlshd (build steps: %s)",
			tlshdMinVersion, filepath.Join(root, "storager", "nvmeof", "nvmet-host", "README.md"))
	case !versionAtLeast(ver, tlshdMinVersion):
		c.Status = checkFail
		c.Detail = fmt.Sprintf("tlshd version too old: %s (requires >= %s; 1.5.0+ prefers the process keyring, older versions mask new keys after a PSK rotation)", ver, tlshdMinVersion)
		c.Hint = "install/upgrade ktls-utils and run sudo systemctl enable --now tlshd"
	default:
		c.Status = checkOK
		c.Detail = "version " + ver + " (service running)"
	}
	checks = append(checks, c)
	return checks
}

func configfsMounted(env hostEnv) bool {
	data, err := env.read(env.mountsPath())
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

// ── control 角色：宿主暴露面与凭据权限 ──

const composePSFormat = `{{.ID}}|{{.Label "com.docker.compose.service"}}|{{.Ports}}`

type composeContainer struct {
	id      string
	service string
	ports   string
}

func parseComposePS(out string) []composeContainer {
	var containers []composeContainer
	for _, line := range strings.Split(out, "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		parts := strings.SplitN(line, "|", 3)
		if len(parts) < 2 {
			continue
		}
		c := composeContainer{id: strings.TrimSpace(parts[0]), service: strings.TrimSpace(parts[1])}
		if len(parts) == 3 {
			c.ports = strings.TrimSpace(parts[2])
		}
		containers = append(containers, c)
	}
	return containers
}

func findComposeService(containers []composeContainer, service string) (composeContainer, bool) {
	for _, c := range containers {
		if c.service == service {
			return c, true
		}
	}
	return composeContainer{}, false
}

func controlContainerChecks(env hostEnv) []hostCheck {
	var checks []hostCheck
	containers, dockerOK := listComposeContainers(env)
	if !dockerOK {
		return []hostCheck{
			{ID: "control.control-plane", Title: "control plane exposure", Status: checkSkip,
				Detail: "docker is not available; container exposure was not checked"},
			{ID: "control.docker-socket", Title: "docker.sock mount", Status: checkSkip,
				Detail: "docker is not available; container mounts were not checked"},
			{ID: "control.nginx-pki", Title: "nginx PKI mounts", Status: checkSkip,
				Detail: "docker is not available; container mounts were not checked"},
		}
	}
	checks = append(checks, controlPlaneExposureCheck(env, containers))
	checks = append(checks, dockerSocketCheck(env, containers))
	checks = append(checks, nginxPKICheck(env, containers))
	return checks
}

func listenerCheck(env hostEnv, role hostRole) hostCheck {
	c := hostCheck{ID: "host.listeners", Title: "public TCP listeners"}
	if !env.hasTool("ss") {
		c.Status = checkWarn
		c.Detail = "cannot read the listener table (ss is not installed)"
		return c
	}
	out, err := env.out("ss", "-H", "-lnt")
	if err != nil {
		c.Status = checkWarn
		c.Detail = "cannot read the listener table (ss failed)"
		return c
	}
	allowed := allowedPorts(role)
	var unexpected []string
	for _, addr := range publicListeners(out) {
		port := addr[strings.LastIndex(addr, ":")+1:]
		if !allowed[port] {
			hint := portHint(port)
			unexpected = append(unexpected, "tcp "+addr+hint)
		}
	}
	if len(unexpected) == 0 {
		c.Status = checkOK
		c.Detail = "no unexpected public listeners"
	} else {
		c.Status = checkFail
		c.Detail = "unexpected public listener: " + strings.Join(unexpected, ", ")
		c.Hint = "stop the service or bind it to loopback"
	}
	return c
}

func publicListeners(out string) []string {
	var listeners []string
	for _, line := range strings.Split(out, "\n") {
		fields := strings.Fields(line)
		if len(fields) < 4 {
			continue
		}
		local := fields[3]
		host := local[:strings.LastIndex(local, ":")]
		switch {
		case strings.HasPrefix(host, "127."), host == "[::1]", host == "::1":
			continue
		}
		listeners = append(listeners, local)
	}
	return listeners
}

func allowedPorts(role hostRole) map[string]bool {
	allowed := map[string]bool{"22": true}
	if role == roleControl || role == roleAll {
		for _, p := range []string{"53", "67", "69", "80", "443"} {
			allowed[p] = true
		}
	}
	if role == roleStorage || role == roleAll {
		for _, p := range []string{"4840", "4421"} {
			allowed[p] = true
		}
	}
	return allowed
}

func portHint(port string) string {
	switch port {
	case "2375", "2376":
		return " (Docker TCP; equivalent to host root)"
	case "3306":
		return " (MySQL)"
	case "7897":
		return " (proxy)"
	case "53317":
		return " (LocalSend)"
	}
	return ""
}

func dockerDaemonCheck(env hostEnv) hostCheck {
	c := hostCheck{ID: "host.docker-daemon", Title: "docker daemon exposure"}
	if !env.hasTool("docker") {
		c.Status = checkWarn
		c.Detail = "docker is not installed; container checks were skipped"
		return c
	}
	info, err := env.out("docker", "info")
	if err != nil {
		c.Status = checkWarn
		c.Detail = "cannot query the docker daemon"
		return c
	}
	if strings.Contains(info, "tcp://") {
		c.Status = checkFail
		c.Detail = "the docker daemon is exposed over TCP (2375/2376); this is equivalent to host root"
		c.Hint = "remove the TCP listener from the docker daemon configuration"
		return c
	}
	c.Status = checkOK
	c.Detail = "the docker daemon does not listen on TCP"
	return c
}

func listComposeContainers(env hostEnv) ([]composeContainer, bool) {
	if !env.hasTool("docker") {
		return nil, false
	}
	out, err := env.out("docker", "ps", "--format", composePSFormat)
	if err != nil {
		return nil, false
	}
	return parseComposePS(out), true
}

func controlPlaneExposureCheck(env hostEnv, containers []composeContainer) hostCheck {
	c := hostCheck{ID: "control.control-plane", Title: "control plane exposure"}
	cp, ok := findComposeService(containers, "kurrent-control-plane")
	if !ok {
		c.Status = checkSkip
		c.Detail = "the control plane container is not running"
		return c
	}
	if publishedToHost(cp.ports) {
		c.Status = checkFail
		c.Detail = "the control plane publishes a host port (the management plane must go through the unix socket): " + cp.ports
		c.Hint = "remove the published port from docker-compose.yml"
		return c
	}
	c.Status = checkOK
	c.Detail = "no host port is published"
	return c
}

func publishedToHost(ports string) bool {
	return strings.Contains(ports, "0.0.0.0:") || strings.Contains(ports, "[::]:")
}

func dockerSocketCheck(env hostEnv, containers []composeContainer) hostCheck {
	c := hostCheck{ID: "control.docker-socket", Title: "docker.sock mount"}
	cp, ok := findComposeService(containers, "kurrent-control-plane")
	if !ok {
		c.Status = checkSkip
		c.Detail = "the control plane container is not running"
		return c
	}
	mounts, err := containerMounts(env, cp.id)
	if err != nil {
		c.Status = checkWarn
		c.Detail = "cannot inspect container mounts"
		return c
	}
	if strings.Contains(mounts, "/var/run/docker.sock") {
		c.Status = checkFail
		c.Detail = "the control plane mounts docker.sock"
		c.Hint = "remove the mount; dnsmasq and nginx manage themselves"
		return c
	}
	c.Status = checkOK
	c.Detail = "docker.sock is not mounted"
	return c
}

func nginxPKICheck(env hostEnv, containers []composeContainer) hostCheck {
	c := hostCheck{ID: "control.nginx-pki", Title: "nginx PKI mounts"}
	webui, ok := findComposeService(containers, "kurrent-nginx")
	if !ok {
		c.Status = checkSkip
		c.Detail = "the webui container is not running"
		return c
	}
	mounts, err := containerMounts(env, webui.id)
	if err != nil {
		c.Status = checkWarn
		c.Detail = "cannot inspect container mounts"
		return c
	}
	if exposesPKIPrivateKey(mounts) {
		c.Status = checkFail
		c.Detail = "the webui container mounts the PKI private-key directory"
		c.Hint = "mount only the nginx trust bundle"
		return c
	}
	c.Status = checkOK
	c.Detail = "no PKI private key is mounted"
	return c
}

// exposesPKIPrivateKey：nginx 只应挂载公开信任包（pki/nginx）；CA 目录与私钥文件
// 一旦进入容器即可被用于签发，视为失败。
func exposesPKIPrivateKey(mounts string) bool {
	for _, entry := range strings.Fields(mounts) {
		source, _, _ := strings.Cut(entry, ":")
		source = filepath.Clean(source)
		switch {
		case strings.HasSuffix(source, "/pki"), strings.Contains(source, "/pki/ca"), strings.Contains(source, "ca.key"):
			return true
		}
	}
	return false
}

func containerMounts(env hostEnv, id string) (string, error) {
	return env.out("docker", "inspect", id, "--format",
		`{{range .Mounts}}{{.Source}}:{{.Destination}} {{end}}`)
}

func firewallCheck(env hostEnv) hostCheck {
	c := hostCheck{ID: "host.firewall", Title: "Docker-published port filtering"}
	if env.uidValue() != 0 {
		c.Status = checkSkip
		c.Detail = "requires root; re-run with sudo to check firewall state"
		return c
	}
	ufwOut, err := env.out("ufw", "status")
	if err != nil || !strings.Contains(ufwOut, "Status: active") {
		c.Status = checkWarn
		c.Detail = "ufw is not active or not readable; make sure Docker-published ports are filtered upstream"
		return c
	}
	rulesOut, err := env.out("iptables", "-S", "DOCKER-USER")
	if err != nil {
		c.Status = checkWarn
		c.Detail = "cannot read the DOCKER-USER chain"
		return c
	}
	if !hasIptablesRule(rulesOut) {
		c.Status = checkWarn
		c.Detail = "ufw is active but DOCKER-USER is empty; Docker-published ports bypass ufw"
		c.Hint = "add the filtering rules to DOCKER-USER"
		return c
	}
	c.Status = checkOK
	c.Detail = "DOCKER-USER has filtering rules"
	return c
}

func hasIptablesRule(out string) bool {
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimSpace(line)
		if line != "" && !strings.HasPrefix(line, "-N") && !strings.HasPrefix(line, "-P") {
			return true
		}
	}
	return false
}

// deploymentChecks 校验状态根与部署记录：状态必须落在仓库之外、目录 0700 且属于
// 当前用户；记录存在且可解析。未注入部署上下文（单元测试）时不产生检查项。
func deploymentChecks(env hostEnv, root string) []hostCheck {
	if !env.deploymentChecked {
		return nil
	}
	stateRoot := env.stateRoot
	if stateRoot == "" {
		stateRoot = filepath.Dir(filepath.Dir(env.stateDir(root)))
	}
	c := hostCheck{ID: "control.state-root", Title: "state root outside the repository"}
	insideRepo := false
	if rel, err := filepath.Rel(root, stateRoot); err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		insideRepo = true
	}
	fi, statErr := env.stat(stateRoot)
	switch {
	case insideRepo:
		c.Status = checkFail
		c.Detail = stateRoot + " is inside the checkout"
		c.Hint = "pass --state-dir with a path outside the repository (default ~/.local/state/kurrent)"
	case statErr != nil:
		c.Status = checkSkip
		c.Detail = "not initialized: " + stateRoot
	default:
		mode := fi.Mode().Perm()
		c.Status = checkOK
		c.Detail = fmt.Sprintf("%s (mode=%04o)", stateRoot, uint32(mode))
		if mode != 0o700 {
			c.Status = checkWarn
			c.Hint = "chmod 700 " + stateRoot
		}
		if st, ok := fi.Sys().(*syscall.Stat_t); ok && int(st.Uid) != env.uidValue() {
			c.Status = checkFail
			c.Detail = fmt.Sprintf("%s is owned by uid %d (kurrent runs as uid %d)", stateRoot, st.Uid, env.uidValue())
			c.Hint = fmt.Sprintf("sudo chown -R \"$(id -u):$(id -g)\" %s", stateRoot)
		}
	}

	r := hostCheck{ID: "control.deployment-record", Title: "deployment record"}
	switch {
	case env.deploymentErr != "":
		r.Status = checkFail
		r.Detail = env.deploymentErr
		r.Hint = "remove ~/.kurrent/deployment.yaml and re-run kurrent init"
	case env.deployment == nil:
		r.Status = checkWarn
		r.Detail = "no deployment record (~/.kurrent/deployment.yaml)"
		r.Hint = "run kurrent init (or --adopt-state for a legacy deployment)"
	default:
		r.Status = checkOK
		r.Detail = fmt.Sprintf("repo=%s state=%s status=%s", env.deployment.RepoRoot, env.deployment.StateDir, env.deployment.Status)
		if env.deployment.StateDir != stateRoot {
			r.Status = checkWarn
			r.Detail += fmt.Sprintf(" (record state root differs from %s)", stateRoot)
		}
	}
	return []hostCheck{c, r}
}

func credentialChecks(env hostEnv, root string) []hostCheck {
	stateDir := env.stateDir(root)
	if fi, err := env.stat(stateDir); err != nil || !fi.IsDir() {
		return []hostCheck{{ID: "control.credentials", Title: "control-plane credentials",
			Status: checkSkip, Detail: "control-plane state directory not found: " + stateDir}}
	}
	var checks []hostCheck
	paths := []string{
		filepath.Join(stateDir, "pki", "ca"),
		filepath.Join(stateDir, "users.yml"),
		filepath.Join(stateDir, "sessions.yml"),
		filepath.Join(stateDir, "credentials.yml"),
		filepath.Join(stateDir, "admin.conf"),
		filepath.Join(stateDir, "audit.jsonl"),
	}
	for _, path := range paths {
		fi, err := env.stat(path)
		if err != nil {
			continue
		}
		mode := fi.Mode().Perm()
		c := hostCheck{ID: "control.credentials." + filepath.Base(path),
			Title: "credential file " + filepath.Base(path)}
		if secureMode(mode) {
			c.Status = checkOK
		} else {
			c.Status = checkWarn
			c.Hint = "restrict it to 600/700"
		}
		c.Detail = fmt.Sprintf("mode=%04o", uint32(mode))
		checks = append(checks, c)
	}
	keys := env.globFiles(filepath.Join(stateDir, "pki", "ca", "*", "ca.key"))
	sort.Strings(keys)
	for _, key := range keys {
		fi, err := env.stat(key)
		if err != nil {
			continue
		}
		mode := fi.Mode().Perm()
		c := hostCheck{ID: "control.credentials.ca-key." + filepath.Base(filepath.Dir(key)),
			Title: "CA private key " + filepath.Base(filepath.Dir(key))}
		if mode == 0o600 {
			c.Status = checkOK
			c.Detail = "mode=0600"
		} else {
			c.Status = checkFail
			c.Detail = fmt.Sprintf("mode=%04o (private keys must be 0600)", uint32(mode))
			c.Hint = "chmod 600 " + key
		}
		checks = append(checks, c)
	}
	if len(checks) == 0 {
		checks = append(checks, hostCheck{ID: "control.credentials", Title: "control-plane credentials",
			Status: checkSkip, Detail: "no credential files found in " + stateDir})
	}
	return checks
}

func secureMode(mode fs.FileMode) bool {
	switch mode {
	case 0o600, 0o700, 0o750, 0o755, 0o640, 0o644:
		return true
	}
	return false
}

// ── 输出 ──

func printHostReport(w io.Writer, report hostReport, asJSON bool) error {
	if asJSON {
		enc := json.NewEncoder(w)
		enc.SetIndent("", "  ")
		return enc.Encode(report)
	}
	fmt.Fprintf(w, "role: %s (strict: %t)\n", report.Role, report.Strict)
	for _, c := range report.Checks {
		fmt.Fprintf(w, "%-4s %s", statusLabel(c.Status), c.Title)
		if c.Detail != "" {
			fmt.Fprintf(w, ": %s", c.Detail)
		}
		fmt.Fprintln(w)
		if c.Hint != "" {
			fmt.Fprintf(w, "     hint: %s\n", c.Hint)
		}
	}
	fmt.Fprintf(w, "\nsummary: %d ok, %d warn, %d fail, %d skip\n",
		report.Summary.OK, report.Summary.Warn, report.Summary.Fail, report.Summary.Skip)
	return nil
}

func statusLabel(s checkStatus) string {
	if s == checkFail {
		return "FAIL"
	}
	return string(s)
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
