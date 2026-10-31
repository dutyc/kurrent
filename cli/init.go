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

func cmdInit(args []string) {
	p := parseArgs(args)
	dir, err := resolveControlPlaneDir(p.opt("dir", ""))
	if err != nil {
		fatal("%v", err)
	}
	configFile := p.opt("config", filepath.Join(dir, "kurrent.yaml"))
	if !filepath.IsAbs(configFile) {
		if configFile, err = filepath.Abs(configFile); err != nil {
			fatal("解析 --config: %v", err)
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
			return "", fmt.Errorf("解析 --dir: %w", err)
		}
		return abs, nil
	}
	cwd, err := os.Getwd()
	if err != nil {
		return "", fmt.Errorf("获取当前目录: %w", err)
	}
	if fi, err := os.Stat(filepath.Join(cwd, "control_plane")); err == nil && fi.IsDir() {
		return filepath.Join(cwd, "control_plane"), nil
	}
	if fi, err := os.Stat(filepath.Join(cwd, "config")); err == nil && fi.IsDir() {
		return cwd, nil // 已位于 control_plane 内
	}
	return "", fmt.Errorf("未找到 control_plane 目录（请在仓库根运行，或用 --dir 指定）")
}

// initRun 校验声明并收敛启动控制面：结构预检 → 控制面镜像内按运行时 schema
// 全量校验（写盘前）→ 应用声明 → 启动/重启 → 健康检查 → dnsmasq 起服核验。
// 任一环节失败返回非零；声明发生变化时回滚旧文件并恢复上一份声明的运行态。
func initRun(o initOpts) error {
	target := filepath.Join(o.dir, "kurrent.yaml")
	data, err := os.ReadFile(o.configFile)
	if err != nil {
		if os.IsNotExist(err) {
			return fmt.Errorf("control plane configuration not found: %s\n"+
				"先生成模板并编辑：kurrent config print init-defaults > %s", o.configFile, target)
		}
		return fmt.Errorf("读取 %s: %w", o.configFile, err)
	}
	if err := validateControlPlaneConfig(data, o.configFile); err != nil {
		return err
	}
	repoRoot := filepath.Dir(o.dir)

	fmt.Println("==> kurrent init: building validation image (kurrent-control-plane)")
	if err := composeRun(repoRoot, "build", "kurrent-control-plane"); err != nil {
		return fmt.Errorf("构建控制面镜像失败: %w", err)
	}
	out, err := runConfigTool("kurrent-control-plane", "validate", data)
	if err != nil {
		return fmt.Errorf("%s 声明校验失败（未应用）:\n%s", o.configFile, err)
	}
	fmt.Printf("==> kurrent init: %s\n", strings.TrimSpace(out))

	previous, prevErr := os.ReadFile(target)
	hadPrevious := prevErr == nil
	changed := !hadPrevious || !bytes.Equal(previous, data)

	if o.configFile != target {
		if err := os.WriteFile(target, data, 0o644); err != nil {
			return fmt.Errorf("应用声明到 %s: %w", target, err)
		}
		fmt.Printf("==> kurrent init: applied %s -> %s\n", o.configFile, target)
	}

	// rollback 恢复上一份声明并尽力恢复运行态；无旧文件时保留候选供修正。
	rollback := func(reason error) error {
		notes := []string{}
		switch {
		case hadPrevious && changed:
			if err := os.WriteFile(target, previous, 0o644); err != nil {
				notes = append(notes, fmt.Sprintf("回滚旧声明失败: %v", err))
				break
			}
			notes = append(notes, fmt.Sprintf("已回滚到上一份声明（%s）", target))
			if err := composeRun(repoRoot, "restart", "kurrent-control-plane"); err != nil {
				notes = append(notes, fmt.Sprintf("回滚重启控制面失败: %v", err))
			} else if !waitControlPlaneHealthy(rollbackHealthWait) {
				notes = append(notes, "回滚后的控制面仍未通过健康检查")
			}
			if err := composeRun(repoRoot, "restart", "kurrent-dnsmasq"); err != nil {
				notes = append(notes, fmt.Sprintf("回滚重启 dnsmasq 失败: %v", err))
			}
		case hadPrevious:
			notes = append(notes, "声明内容未变化，未回滚文件")
		default:
			notes = append(notes, fmt.Sprintf("无上一份声明可回滚，已保留 %s 供修正", target))
		}
		return fmt.Errorf("%v\n%s", reason, strings.Join(notes, "\n"))
	}

	fmt.Println("==> kurrent init: starting control plane (docker compose up -d)")
	if err := composeRun(repoRoot, "up", "-d"); err != nil {
		return rollback(err)
	}
	fmt.Println("==> kurrent init: restarting kurrent-control-plane (apply declaration)")
	if err := composeRun(repoRoot, "restart", "kurrent-control-plane"); err != nil {
		return rollback(err)
	}
	wait := o.healthWait
	if wait == 0 {
		wait = 30 * time.Second
	}
	fmt.Printf("==> kurrent init: waiting for control plane health (%s)\n", wait)
	if !waitControlPlaneHealthy(wait) {
		logs := containerLogs("kurrent-control-plane", 40)
		return rollback(fmt.Errorf(
			"控制面未在 %s 内通过健康检查（声明未生效）\n最近日志:\n%s", wait, logs))
	}
	fmt.Println("==> kurrent init: restarting kurrent-dnsmasq (apply rendered conf)")
	if err := composeRun(repoRoot, "restart", "kurrent-dnsmasq"); err != nil {
		return rollback(err)
	}
	if !waitContainerRunning("kurrent-dnsmasq", dnsmasqStartupWatch) {
		logs := containerLogs("kurrent-dnsmasq", 30)
		return rollback(fmt.Errorf(
			"dnsmasq 未能保持运行（dnsmasq.conf 可能不合法）\n最近日志:\n%s", logs))
	}
	fmt.Println("==> kurrent init: done. 控制面已按声明运行：")
	fmt.Println("    WebUI: https://<host>/（唯一入口 443）；控制面管理面只监听 unix socket（无 TCP 端口）")
	fmt.Println("    管理凭据：control_plane/state/admin.conf（kurrent 自动发现，admin 证书走 443 mTLS）")
	fmt.Println("    任意目录使用：kurrent credentials install（凭据快照到 ~/.kurrent，供 PATH 安装的二进制）")
	fmt.Println("    运行态核对：kurrent config status；下一步：kurrent token create --agent-id <节点 id>")
	return nil
}

// containerLogs 读取容器最近日志（失败返回提示文本，不阻断错误上报）。
func containerLogs(name string, tail int) string {
	out, err := exec.Command("docker", "logs", "--tail", strconv.Itoa(tail), name).CombinedOutput()
	if len(out) == 0 {
		return fmt.Sprintf("（无法读取 %s 日志: %v）", name, err)
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
		return fmt.Errorf("docker compose %s 失败（目录 %s）: %w", strings.Join(args, " "), dir, err)
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
		return fmt.Errorf("docker compose %s 失败（目录 %s）: %w", strings.Join(args, " "), dir, err)
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
		return fmt.Errorf("解析 %s: %w", path, err)
	}
	if v := nodeValue(&root, "apiVersion"); v != "kurrent.io/v1" {
		return fmt.Errorf("%s: apiVersion = %q, want kurrent.io/v1", path, v)
	}
	if kind := nodeValue(&root, "kind"); kind != "ControlPlaneConfiguration" {
		return fmt.Errorf("%s: kind = %q, want ControlPlaneConfiguration", path, kind)
	}
	netKeys := []string{"interface", "subnet", "dhcpRange", "gateway", "dns"}
	var missing []string
	for _, k := range netKeys {
		if strings.TrimSpace(nodeValue(&root, "spec", "networking", k)) == "" {
			missing = append(missing, "spec.networking."+k)
		}
	}
	if len(missing) > 0 {
		return fmt.Errorf("%s: networking 必填缺失（模板占位需替换）: %s", path, strings.Join(missing, ", "))
	}
	subnet := nodeValue(&root, "spec", "networking", "subnet")
	if _, _, err := net.ParseCIDR(subnet); err != nil {
		return fmt.Errorf("%s: spec.networking.subnet 须为 CIDR（如 192.168.80.0/24）: %s", path, subnet)
	}
	for _, k := range []string{"gateway", "dns"} {
		v := nodeValue(&root, "spec", "networking", k)
		if net.ParseIP(v) == nil {
			return fmt.Errorf("%s: spec.networking.%s 须为 IP 地址: %s", path, k, v)
		}
	}
	dhcpRange := nodeValue(&root, "spec", "networking", "dhcpRange")
	rangeIPs := strings.Split(dhcpRange, ",")
	if len(rangeIPs) != 2 ||
		net.ParseIP(strings.TrimSpace(rangeIPs[0])) == nil ||
		net.ParseIP(strings.TrimSpace(rangeIPs[1])) == nil {
		return fmt.Errorf("%s: spec.networking.dhcpRange 须为 起止 IP 对（如 192.168.80.50,192.168.80.100）: %s",
			path, dhcpRange)
	}
	return nil
}
