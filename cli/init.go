// init 子命令：控制面初始化（配置即声明、CLI 即工具）。
//
// 首次部署是一个事务：预检（Docker/Compose、WebUI 构建产物、状态根属主）→
// 校验声明 → 准备仓库外的状态根 → 固定 compose 项目 → 等待控制面通过一次性
// 引导交换发布管理凭据 → 安装到 ~/.kurrent → mTLS 自检通过。任一步失败返回
// 非零；声明校验失败回滚声明，引导失败保留交换包并让重跑可续。
// 运行时状态不再放在仓库内：移动/改名仓库只改变代码位置，部署身份由部署记录
// （~/.kurrent/deployment.yaml）与固定项目名维持。
package main

import (
	"bytes"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

var initSpec = cmdSpec{
	usage: initUsage,
	flags: []flagSpec{
		{name: "config", kind: flagString},
		{name: "dir", kind: flagString},
		{name: "state-dir", kind: flagString},
		{name: "adopt-state", kind: flagBool},
	},
	maxPos: 0,
}

func cmdInit(args []string) {
	p, help := mustParse(args, initSpec)
	if help {
		return
	}
	for _, flag := range []string{"dir", "config", "state-dir"} {
		if p.has(flag) && strings.TrimSpace(p.opt(flag, "")) == "" {
			usageError(initUsage, fmt.Errorf("--%s must not be empty", flag))
		}
	}
	dir, err := resolveControlPlaneDir(p.opt("dir", ""))
	if err != nil {
		fatal("%v", err)
	}
	configFile := p.opt("config", filepath.Join(dir, "kurrent.yaml"))
	if !filepath.IsAbs(configFile) {
		if configFile, err = filepath.Abs(configFile); err != nil {
			fatal("resolve --config: %v", err)
		}
	}
	if err := initRun(initOpts{
		dir:        dir,
		configFile: configFile,
		stateDir:   p.opt("state-dir", ""),
		adoptState: p.bool("adopt-state"),
	}); err != nil {
		fatal("%v", err)
	}
}

type initOpts struct {
	dir        string        // control_plane 目录
	configFile string        // 控制面声明文件（缺省 <dir>/kurrent.yaml；其他路径 = 应用为控制面配置）
	stateDir   string        // 状态根（缺省：KURRENT_STATE_DIR → 部署记录 → ~/.local/state/kurrent）
	adoptState bool          // 迁移仓库内旧布局
	healthWait time.Duration // 收敛时等待控制面 /healthz 的超时（0 = 默认 30s；测试注入缩短）
}

// resolveControlPlaneDir 定位 control_plane 目录：--dir 优先（也接受仓库根）；否则
// 部署记录指向的仓库；否则 cwd 下 control_plane/；cwd 已是 control_plane 时用 cwd。
func resolveControlPlaneDir(explicit string) (string, error) {
	if explicit != "" {
		abs, err := filepath.Abs(explicit)
		if err != nil {
			return "", fmt.Errorf("resolve --dir: %w", err)
		}
		if isDir(filepath.Join(abs, "control_plane")) {
			return filepath.Join(abs, "control_plane"), nil
		}
		return abs, nil
	}
	cwd, err := os.Getwd()
	if err != nil {
		return "", fmt.Errorf("get current directory: %w", err)
	}
	if fi, err := os.Stat(filepath.Join(cwd, "control_plane")); err == nil && fi.IsDir() {
		return filepath.Join(cwd, "control_plane"), nil
	}
	if fi, err := os.Stat(filepath.Join(cwd, "config")); err == nil && fi.IsDir() {
		return cwd, nil // 已位于 control_plane 内
	}
	if rec, err := readDeploymentRecord(); err == nil && rec != nil {
		if isDir(filepath.Join(rec.RepoRoot, "control_plane")) {
			return filepath.Join(rec.RepoRoot, "control_plane"), nil
		}
	}
	return "", fmt.Errorf("control_plane directory not found (run from the repository root or pass --dir)")
}

// initRun 校验声明并收敛启动控制面：预检 → 状态根准备 → 控制面镜像内按运行时
// schema 全量校验（写盘前）→ 应用声明 → 启动/重启 → 健康检查 → 引导凭据交换 →
// 安装凭据与 mTLS 自检 → dnsmasq 起服核验。声明变化失败时回滚旧文件并恢复上一份
// 声明的运行态；引导失败保留交换包，重跑可续。
func initRun(o initOpts) error {
	q, err := resolveDeployment(o.dir, o.stateDir)
	if err != nil {
		return err
	}
	target := filepath.Join(o.dir, "kurrent.yaml")
	data, err := os.ReadFile(o.configFile)
	if err != nil {
		if os.IsNotExist(err) {
			return fmt.Errorf("control-plane declaration does not exist: %s\n"+
				"Generate a template first and edit it: kurrent config print init-defaults > %s", o.configFile, target)
		}
		return fmt.Errorf("read %s: %w", o.configFile, err)
	}
	if err := validateControlPlaneConfig(data, o.configFile); err != nil {
		return err
	}

	legacy := detectLegacyControlPlaneState(q.repoRoot)
	if err := preflightControlPlane(q, legacy, o.adoptState); err != nil {
		return err
	}
	if err := rejectUnknownRunningDeployment(q, o.adoptState); err != nil {
		return err
	}

	paths := q.controlPlane()
	project := q.project()
	env := q.composeEnv()

	fmt.Println("==> kurrent init: building the validation image (kurrent-control-plane)")
	if err := composeExec(q.repoRoot, project, env, "build", "kurrent-control-plane"); err != nil {
		return fmt.Errorf("build control-plane image failed: %w", err)
	}
	out, err := runConfigTool("kurrent-control-plane", "validate", data)
	if err != nil {
		return fmt.Errorf("%s declaration validation failed (not applied):\n%s", o.configFile, err)
	}
	fmt.Printf("==> kurrent init: %s\n", strings.TrimSpace(out))

	previous, prevErr := os.ReadFile(target)
	hadPrevious := prevErr == nil
	changed := !hadPrevious || !bytes.Equal(previous, data)

	if o.configFile != target {
		if err := writeFileAtomic(target, data, 0o644); err != nil {
			return fmt.Errorf("apply declaration to %s: %w", target, err)
		}
		fmt.Printf("==> kurrent init: applied %s -> %s\n", o.configFile, target)
	}

	// rollback 恢复上一份声明并尽力恢复运行态；无旧文件时保留候选供修正。
	rollback := func(reason error) error {
		notes := []string{}
		switch {
		case hadPrevious && changed:
			if err := writeFileAtomic(target, previous, 0o644); err != nil {
				notes = append(notes, fmt.Sprintf("failed to roll back the previous declaration: %v", err))
				break
			}
			notes = append(notes, fmt.Sprintf("rolled back to the previous declaration (%s)", target))
			if err := composeExec(q.repoRoot, project, env, "restart", "kurrent-control-plane"); err != nil {
				notes = append(notes, fmt.Sprintf("failed to restart the control plane after rollback: %v", err))
			} else if !waitControlPlaneHealthy(rollbackHealthWait) {
				notes = append(notes, "the control plane still fails the health check after rollback")
			}
			if err := composeExec(q.repoRoot, project, env, "restart", "kurrent-dnsmasq"); err != nil {
				notes = append(notes, fmt.Sprintf("failed to restart dnsmasq after rollback: %v", err))
			}
		case hadPrevious:
			notes = append(notes, "the declaration did not change; no file rollback was needed")
		default:
			notes = append(notes, fmt.Sprintf("no previous declaration to roll back; %s was kept for correction", target))
		}
		return fmt.Errorf("%v\n%s", reason, strings.Join(notes, "\n"))
	}

	if o.adoptState && legacy.present() {
		fmt.Println("==> kurrent init: stopping the legacy stacks (state migration)")
		stopLegacyDeployment(q)
		fmt.Printf("==> kurrent init: migrating legacy state into %s\n", q.stateDir)
		if err := adoptControlPlaneState(q.repoRoot, q); err != nil {
			return fmt.Errorf("migrate legacy state: %w", err)
		}
		if storagerDir := filepath.Join(q.repoRoot, "storager"); detectLegacyStorageState(storagerDir).present() {
			if err := adoptStorageState(storagerDir, q); err != nil {
				return fmt.Errorf("migrate legacy node state: %w", err)
			}
		}
	}
	if err := prepareControlPlaneState(paths); err != nil {
		return fmt.Errorf("prepare the state root: %w", err)
	}
	if err := writeInitRecord(q, deployStatusBootstrapping); err != nil {
		return err
	}
	if err := requestBootstrapExchange(paths.bootstrap); err != nil {
		return err
	}

	relocated := q.record != nil && (q.record.RepoRoot != q.repoRoot || q.record.StateDir != q.stateDir)
	upArgs := []string{"up", "-d"}
	if relocated || (o.adoptState && legacy.present()) {
		upArgs = append(upArgs, "--force-recreate")
	}
	fmt.Printf("==> kurrent init: starting the control plane (docker compose -p %s up -d)\n", project)
	if err := composeExec(q.repoRoot, project, env, upArgs...); err != nil {
		return rollback(err)
	}
	fmt.Println("==> kurrent init: restarting kurrent-control-plane (applying the declaration)")
	if err := composeExec(q.repoRoot, project, env, "restart", "kurrent-control-plane"); err != nil {
		return rollback(err)
	}
	wait := o.healthWait
	if wait == 0 {
		wait = 30 * time.Second
	}
	fmt.Printf("==> kurrent init: waiting for the control-plane health check (%s)\n", wait)
	if !waitControlPlaneHealthy(wait) {
		logs := containerLogs("kurrent-control-plane", 40)
		return rollback(fmt.Errorf(
			"the control plane did not pass the health check within %s (declaration not applied)\nrecent logs:\n%s", wait, logs))
	}
	fmt.Println("==> kurrent init: waiting for the one-shot management credential exchange")
	if err := waitExchange(paths.bootstrap, bootstrapExchangeTimeout); err != nil {
		logs := containerLogs("kurrent-control-plane", 40)
		return fmt.Errorf("%v\nrecent logs:\n%s", err, logs)
	}
	bundle, err := validateExchangeBundle(paths.bootstrap)
	if err != nil {
		return fmt.Errorf("the exchanged management credentials are unusable: %w", err)
	}
	if _, _, err := installCredentialsFrom(bundle.AdminConf, paths.adminConf); err != nil {
		return fmt.Errorf("install management credentials: %w", err)
	}
	fmt.Println("==> kurrent init: verifying the installed credentials (mTLS GET /whoami)")
	if err := verifyInstalledCredentials(managementSelfTestTimeout); err != nil {
		return err
	}
	if err := finishBootstrapExchange(paths.bootstrap); err != nil {
		return err
	}

	fmt.Println("==> kurrent init: restarting kurrent-dnsmasq (applying the rendered config)")
	if err := composeExec(q.repoRoot, project, env, "restart", "kurrent-dnsmasq"); err != nil {
		return rollback(err)
	}
	if !waitContainerRunning("kurrent-dnsmasq", dnsmasqStartupWatch) {
		logs := containerLogs("kurrent-dnsmasq", 30)
		return rollback(fmt.Errorf(
			"dnsmasq did not stay running (dnsmasq.conf may be invalid)\nrecent logs:\n%s", logs))
	}

	if err := writeInitRecord(q, deployStatusReady); err != nil {
		return err
	}
	if o.adoptState && legacy.present() {
		if err := removeLegacyControlPlaneState(q.repoRoot); err != nil {
			return fmt.Errorf("deployment migrated, but removing the legacy state failed: %w", err)
		}
		if storagerDir := filepath.Join(q.repoRoot, "storager"); detectLegacyStorageState(storagerDir).present() {
			if err := removeLegacyStorageState(storagerDir); err != nil {
				return fmt.Errorf("deployment migrated, but removing the legacy node state failed: %w", err)
			}
		}
		fmt.Println("==> kurrent init: legacy in-repository state removed")
	}

	credentialsPath := userCredentialsPath()
	if credentialsPath == "" {
		credentialsPath = "(home directory unavailable)"
	}
	fmt.Println("==> kurrent init: done. The control plane is running with the declaration:")
	fmt.Printf("    state root: %s (outside the repository; move the checkout freely)\n", q.stateDir)
	fmt.Printf("    deployment record: %s (compose project %s)\n", deploymentRecordPath(), project)
	fmt.Printf("    management credentials: %s (mTLS; re-run kurrent init after rotation)\n", credentialsPath)
	fmt.Printf("    live server-side credential: %s\n", paths.adminConf)
	fmt.Println("    WebUI: https://<host>/ (the only entry on 443); the management API listens only on a unix socket (no TCP port)")
	fmt.Println("    inspect: kurrent config status; next: kurrent token create or the WebUI Join Node action")
	fmt.Println("    manual compose: kurrent deployment show -o env")
	fmt.Println("    host baseline (read-only): sudo kurrent host check")
	return nil
}

var (
	bootstrapExchangeTimeout  = 90 * time.Second
	managementSelfTestTimeout = 30 * time.Second
	// 变量化供测试注入：真实交换/自检由专门用例覆盖。
	waitExchange      = waitBootstrapExchange
	verifyCredentials = managementSelfTest
)

// verifyInstalledCredentials 用 ~/.kurrent 的安装副本做 mTLS 自检（不是交换包里的临时副本）。
func verifyInstalledCredentials(timeout time.Duration) error {
	cc := loadCredentialsFile(userCredentialsPath())
	if cc == nil {
		return fmt.Errorf("the installed management credential is missing (%s)", userCredentialsPath())
	}
	if err := verifyCredentials(cc.server, cc.caFile, cc.certFile, cc.keyFile, timeout); err != nil {
		return fmt.Errorf("%v\n  the credential snapshot is kept at %s; fix the control plane, then rerun kurrent init (the exchange is resumable)",
			err, userCredentialsPath())
	}
	return nil
}

// writeInitRecord 写入/更新部署记录；storage 角色沿用已有记录（join 负责维护）。
func writeInitRecord(q deployQuery, status string) error {
	rec := deploymentRecord{
		Project:        q.project(),
		StorageProject: q.storageProject(),
		RepoRoot:       q.repoRoot,
		StateDir:       q.stateDir,
		Roles:          deploymentRoles{ControlPlane: true},
		Status:         status,
	}
	if q.record != nil {
		rec.Roles.Storage = q.record.Roles.Storage
	}
	return writeDeploymentRecord(&rec)
}

// rejectUnknownRunningDeployment 无部署记录但已有同名容器时拒绝：旧部署必须先迁移，
// 否则 compose 会因固定 container_name 冲突而失败（且状态位置无人知晓）。
func rejectUnknownRunningDeployment(q deployQuery, adopt bool) error {
	if adopt || q.record != nil {
		return nil
	}
	out, err := exec.Command("docker", "ps", "-a", "--format", "{{.Names}}").Output()
	if err != nil {
		return nil // 预检已确认 docker 可用；这里失败按无容器处理
	}
	names := map[string]bool{}
	for _, line := range strings.Split(string(out), "\n") {
		names[strings.TrimSpace(line)] = true
	}
	var found []string
	for _, name := range []string{"kurrent-control-plane", "kurrent-dnsmasq", "kurrent-webui", "storager-agent", "kurrent-nvmet-host", "storager-iscsi"} {
		if names[name] {
			found = append(found, name)
		}
	}
	if len(found) > 0 {
		return fmt.Errorf("containers from an existing deployment are present (%s) but no deployment record exists;\n"+
			"rerun with --adopt-state to migrate the in-repository state, or remove them first if they are stale", strings.Join(found, ", "))
	}
	return nil
}

// stopLegacyDeployment 尽力停止旧项目（仓库 basename / nvmeof / iscsi）与新固定项目：
// 固定 container_name 下，旧容器不清掉就无法以新项目名重建。
func stopLegacyDeployment(q deployQuery) {
	nodeEnv := q.nodeComposeEnv("unknown", q.repoRoot, q.storager("unknown").cpCA)
	projects := []struct {
		dir, project string
		env          []string
	}{
		{q.repoRoot, normalizeComposeProject(filepath.Base(q.repoRoot)), q.composeEnv()},
		{filepath.Join(q.repoRoot, "storager", "nvmeof"), "nvmeof", nodeEnv},
		{filepath.Join(q.repoRoot, "storager", "iscsi"), "iscsi", nodeEnv},
		{q.repoRoot, q.project(), q.composeEnv()},
		{filepath.Join(q.repoRoot, "storager", "nvmeof"), q.storageProject(), nodeEnv},
		{filepath.Join(q.repoRoot, "storager", "iscsi"), q.storageProject(), nodeEnv},
	}
	seen := map[string]bool{}
	for _, p := range projects {
		key := p.dir + "|" + p.project
		if seen[key] {
			continue
		}
		seen[key] = true
		composeBestEffort(p.dir, p.project, p.env, "down", "--remove-orphans")
	}
}

// containerLogs 读取容器最近日志（失败返回提示文本，不阻断错误上报）。
func containerLogs(name string, tail int) string {
	out, err := exec.Command("docker", "logs", "--tail", strconv.Itoa(tail), name).CombinedOutput()
	if len(out) == 0 {
		return fmt.Sprintf("(cannot read %s logs: %v)", name, err)
	}
	return strings.TrimSpace(string(out))
}

// 起服/回滚观察窗（测试注入缩短，避免沙盒等待真实时间）。
var (
	dnsmasqStartupWatch = 2 * time.Second
	rollbackHealthWait  = 20 * time.Second
)

// waitContainerRunning 在 hold 窗口内持续确认容器处于 running：
// 进程读到非法配置会立即退出，短暂观察窗能把它判为失败。
func waitContainerRunning(name string, hold time.Duration) bool {
	deadline := time.Now().Add(hold)
	for {
		out, err := exec.Command("docker", "inspect", "--format", "{{.State.Running}}", name).Output()
		if err != nil || strings.TrimSpace(string(out)) != "true" {
			return false
		}
		if !time.Now().Before(deadline) {
			return true
		}
		time.Sleep(250 * time.Millisecond)
	}
}

// composeExec 在指定目录以固定项目名执行 docker compose；env 为插值变量覆盖。
func composeExec(dir, project string, env []string, args ...string) error {
	cmd := exec.Command("docker", append([]string{"compose", "--project-name", project}, args...)...)
	cmd.Dir = dir
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	if env != nil {
		cmd.Env = envWithOverrides(env)
	}
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("docker compose %s failed (directory %s): %w", strings.Join(args, " "), dir, err)
	}
	return nil
}

// composeBestEffort 同 composeExec，但忽略失败（清理旧栈/尽力停止）。
func composeBestEffort(dir, project string, env []string, args ...string) {
	cmd := exec.Command("docker", append([]string{"compose", "--project-name", project}, args...)...)
	cmd.Dir = dir
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	if env != nil {
		cmd.Env = envWithOverrides(env)
	}
	_ = cmd.Run()
}

// envWithOverrides 合并进程环境与覆盖项：同名键只保留覆盖值，避免子进程读到旧值。
func envWithOverrides(overrides []string) []string {
	drop := map[string]bool{}
	for _, kv := range overrides {
		if i := strings.IndexByte(kv, '='); i > 0 {
			drop[kv[:i]] = true
		}
	}
	env := make([]string, 0, len(os.Environ())+len(overrides))
	for _, kv := range os.Environ() {
		if i := strings.IndexByte(kv, '='); i > 0 && drop[kv[:i]] {
			continue
		}
		env = append(env, kv)
	}
	return append(env, overrides...)
}

// composeRunStderr 同 composeExec，但把构建进度送到 stderr——config show 的 stdout
// 只承载生效声明，可直接重定向为文件。extraEnv 为 nil 时不改动进程环境。
func composeRunStderr(dir, project string, extraEnv []string, args ...string) error {
	cmd := exec.Command("docker", append([]string{"compose", "--project-name", project}, args...)...)
	cmd.Dir = dir
	cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
	if extraEnv != nil {
		cmd.Env = envWithOverrides(extraEnv)
	}
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("docker compose %s failed (directory %s): %w", strings.Join(args, " "), dir, err)
	}
	return nil
}

// waitControlPlaneHealthy 轮询控制面容器的健康状态（healthcheck 经 unix socket
// 请求 /healthz，启动期派生失败时 healthz 为 503）；超时返回 false（调用方回滚）。
func waitControlPlaneHealthy(timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		out, err := exec.Command("docker", "inspect", "--format",
			"{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
			"kurrent-control-plane").Output()
		if err == nil {
			status := strings.TrimSpace(string(out))
			if status == "healthy" || status == "running" {
				return true
			}
		}
		time.Sleep(500 * time.Millisecond)
	}
	return false
}

// validateControlPlaneConfig 结构预检（快速失败）：apiVersion/kind 匹配 +
// spec.networking 五键必填且格式合法（CIDR / IP / DHCP 起止对）。完整 schema
// （未知字段、跨字段语义、策略上限）由组件镜像内的 config_tool 校验。
func validateControlPlaneConfig(data []byte, path string) error {
	var root yaml.Node
	if err := yaml.Unmarshal(data, &root); err != nil {
		return fmt.Errorf("parse %s: %w", path, err)
	}
	if v := nodeValue(&root, "apiVersion"); v != "kurrent.io/v1" {
		return fmt.Errorf("%s: apiVersion = %q, must be kurrent.io/v1", path, v)
	}
	if kind := nodeValue(&root, "kind"); kind != "ControlPlaneConfiguration" {
		return fmt.Errorf("%s: kind = %q, must be ControlPlaneConfiguration", path, kind)
	}
	netKeys := []string{"interface", "subnet", "dhcpRange", "gateway", "dns"}
	var missing []string
	for _, k := range netKeys {
		if strings.TrimSpace(nodeValue(&root, "spec", "networking", k)) == "" {
			missing = append(missing, "spec.networking."+k)
		}
	}
	if len(missing) > 0 {
		return fmt.Errorf("%s: required networking fields are missing (replace the template placeholders): %s", path, strings.Join(missing, ", "))
	}
	subnet := nodeValue(&root, "spec", "networking", "subnet")
	if _, _, err := net.ParseCIDR(subnet); err != nil {
		return fmt.Errorf("%s: spec.networking.subnet must be CIDR (e.g. 192.168.80.0/24): %s", path, subnet)
	}
	for _, k := range []string{"gateway", "dns"} {
		v := nodeValue(&root, "spec", "networking", k)
		if net.ParseIP(v) == nil {
			return fmt.Errorf("%s: spec.networking.%s must be an IP address: %s", path, k, v)
		}
	}
	dhcpRange := nodeValue(&root, "spec", "networking", "dhcpRange")
	rangeIPs := strings.Split(dhcpRange, ",")
	if len(rangeIPs) != 2 ||
		net.ParseIP(strings.TrimSpace(rangeIPs[0])) == nil ||
		net.ParseIP(strings.TrimSpace(rangeIPs[1])) == nil {
		return fmt.Errorf("%s: spec.networking.dhcpRange must be a start/end IP pair (e.g. 192.168.80.50,192.168.80.100): %s",
			path, dhcpRange)
	}
	return nil
}
