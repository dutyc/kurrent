// init 子命令：控制面初始化（配置即声明、CLI 即工具）。
// 声明文件 control_plane/kurrent.yaml 即用户维护的权威配置（模板：
// kurrent config print init-defaults > control_plane/kurrent.yaml 后编辑）。
// init 读入声明 → 校验（networking 五键必填 + 格式，其余块由控制面 pydantic
// 默认注入）→ 收敛启动控制面（docker compose 为内部编排细节，一条命令到
// 运行态；重跑幂等——容器按最新声明重启，改声明后重跑即生效）。
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
	},
	maxPos: 0,
}

func cmdInit(args []string) {
	p, help := mustParse(args, initSpec)
	if help {
		return
	}
	if p.has("dir") && strings.TrimSpace(p.opt("dir", "")) == "" {
		usageError(initUsage, fmt.Errorf("--dir must not be empty"))
	}
	if p.has("config") && strings.TrimSpace(p.opt("config", "")) == "" {
		usageError(initUsage, fmt.Errorf("--config must not be empty"))
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
	if err := initRun(initOpts{dir: dir, configFile: configFile}); err != nil {
		fatal("%v", err)
	}
}

type initOpts struct {
	dir        string        // control_plane 目录
	configFile string        // 控制面声明文件（缺省 <dir>/kurrent.yaml；其他路径 = 应用为控制面配置）
	healthWait time.Duration // 收敛时等待控制面 /healthz 的超时（0 = 默认 30s；测试注入缩短）
}

// resolveControlPlaneDir 定位 control_plane 目录：--dir 优先；否则 cwd 下 control_plane/；
// cwd 已是 control_plane（含 config/ 目录）时用 cwd。
func resolveControlPlaneDir(explicit string) (string, error) {
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
	if fi, err := os.Stat(filepath.Join(cwd, "control_plane")); err == nil && fi.IsDir() {
		return filepath.Join(cwd, "control_plane"), nil
	}
	if fi, err := os.Stat(filepath.Join(cwd, "config")); err == nil && fi.IsDir() {
		return cwd, nil // 已位于 control_plane 内
	}
	return "", fmt.Errorf("control_plane directory not found (run from the repository root or pass --dir)")
}

// initRun 校验声明并收敛启动控制面：结构预检 → 控制面镜像内按运行时 schema
// 全量校验（写盘前）→ 应用声明 → 启动/重启 → 健康检查 → dnsmasq 起服核验。
// 任一环节失败返回非零；声明发生变化时回滚旧文件并恢复上一份声明的运行态。
func initRun(o initOpts) error {
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
	repoRoot := filepath.Dir(o.dir)

	fmt.Println("==> kurrent init: building the validation image (kurrent-control-plane)")
	if err := composeRun(repoRoot, "build", "kurrent-control-plane"); err != nil {
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
			if err := composeRun(repoRoot, "restart", "kurrent-control-plane"); err != nil {
				notes = append(notes, fmt.Sprintf("failed to restart the control plane after rollback: %v", err))
			} else if !waitControlPlaneHealthy(rollbackHealthWait) {
				notes = append(notes, "the control plane still fails the health check after rollback")
			}
			if err := composeRun(repoRoot, "restart", "kurrent-dnsmasq"); err != nil {
				notes = append(notes, fmt.Sprintf("failed to restart dnsmasq after rollback: %v", err))
			}
		case hadPrevious:
			notes = append(notes, "the declaration did not change; no file rollback was needed")
		default:
			notes = append(notes, fmt.Sprintf("no previous declaration to roll back; %s was kept for correction", target))
		}
		return fmt.Errorf("%v\n%s", reason, strings.Join(notes, "\n"))
	}

	fmt.Println("==> kurrent init: starting the control plane (docker compose up -d)")
	if err := composeRun(repoRoot, "up", "-d"); err != nil {
		return rollback(err)
	}
	fmt.Println("==> kurrent init: restarting kurrent-control-plane (applying the declaration)")
	if err := composeRun(repoRoot, "restart", "kurrent-control-plane"); err != nil {
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
	fmt.Println("==> kurrent init: restarting kurrent-dnsmasq (applying the rendered config)")
	if err := composeRun(repoRoot, "restart", "kurrent-dnsmasq"); err != nil {
		return rollback(err)
	}
	if !waitContainerRunning("kurrent-dnsmasq", dnsmasqStartupWatch) {
		logs := containerLogs("kurrent-dnsmasq", 30)
		return rollback(fmt.Errorf(
			"dnsmasq did not stay running (dnsmasq.conf may be invalid)\nrecent logs:\n%s", logs))
	}
	fmt.Println("==> kurrent init: done. The control plane is running with the declaration:")
	fmt.Println("    WebUI: https://<host>/ (the only entry on 443); the management API listens only on a unix socket (no TCP port)")
	fmt.Println("    management credentials: control_plane/state/admin.conf (auto-discovered by kurrent; the admin certificate uses 443 mTLS)")
	fmt.Println("    use from any directory: kurrent credentials install (snapshots credentials to ~/.kurrent for a PATH-installed binary)")
	fmt.Println("    inspect runtime state: kurrent config status; next: kurrent token create (node id generated) or the WebUI Join Node action")
	return nil
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

// composeRun 在指定目录执行 docker compose 子命令（编排细节收敛在 CLI 内部）。
func composeRun(dir string, args ...string) error {
	cmd := exec.Command("docker", append([]string{"compose"}, args...)...)
	cmd.Dir = dir
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("docker compose %s failed (directory %s): %w", strings.Join(args, " "), dir, err)
	}
	return nil
}

// composeRunStderr 同 composeRun，但把构建进度送到 stderr——config show 的 stdout
// 只承载生效声明，可直接重定向为文件。
func composeRunStderr(dir string, args ...string) error {
	cmd := exec.Command("docker", append([]string{"compose"}, args...)...)
	cmd.Dir = dir
	cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
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
