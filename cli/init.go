// init 子命令：控制面初始化（配置即声明、CLI 即工具）。
// 声明文件 control_plane/kurrent.yaml 即用户维护的权威配置（模板：
// kurrent config print init-defaults > control_plane/kurrent.yaml 后编辑）。
// init 读入声明 → 校验（networking 五键必填 + 格式，其余块由控制面 pydantic
// 默认注入）→ 收敛启动控制面（docker compose 为内部编排细节，一条命令到
// 运行态；重跑幂等——容器按最新声明重启，改声明后重跑即生效）。
package main

import (
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
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

// initRun 校验声明并收敛启动控制面：声明应用（--config 指向其他路径时写入
// <dir>/kurrent.yaml——compose 挂载的权威位）→ 启动全套服务 → 重启控制面容器
// 使声明生效 → 等待 /healthz → 重启 dnsmasq 加载重新生成的 conf。
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
	if o.configFile != target {
		if err := os.WriteFile(target, data, 0o644); err != nil {
			return fmt.Errorf("应用声明到 %s: %w", target, err)
		}
		fmt.Printf("==> kurrent init: applied %s -> %s\n", o.configFile, target)
	}
	repoRoot := filepath.Dir(o.dir)

	fmt.Println("==> kurrent init: validating control plane configuration ... ok")
	fmt.Println("==> kurrent init: starting control plane (docker compose up -d)")
	if err := composeRun(repoRoot, "up", "-d"); err != nil {
		return err
	}
	fmt.Println("==> kurrent init: restarting kurrent-control-plane (apply declaration)")
	if err := composeRun(repoRoot, "restart", "kurrent-control-plane"); err != nil {
		return err
	}
	wait := o.healthWait
	if wait == 0 {
		wait = 30 * time.Second
	}
	if !waitControlPlaneHealthz(wait) {
		fmt.Fprintln(os.Stderr, "kurrent: warning: 控制面未在 30s 内就绪（GET http://127.0.0.1:4839/healthz）——docker compose ps / logs 查看")
	}
	if err := composeRun(repoRoot, "restart", "kurrent-dnsmasq"); err != nil {
		return err
	}
	fmt.Println("==> kurrent init: done. 控制面已按声明运行：")
	fmt.Println("    WebUI: https://<host>/（唯一入口 443）；本机健康检查 http://127.0.0.1:4839/healthz")
	fmt.Println("    管理凭据：control_plane/state/admin.conf（kurrent 自动发现，admin 证书走 443 mTLS）")
	fmt.Println("    任意目录使用：kurrent credentials install（凭据快照到 ~/.kurrent，供 PATH 安装的二进制）")
	fmt.Println("    下一步：kurrent token create 签发节点加入凭据")
	return nil
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

// waitControlPlaneHealthz 轮询控制面 /healthz（免鉴权；容器 4839:8080），
// 就绪返回 true；超时返回 false（调用方降级为警告，不阻断收敛流程）。
func waitControlPlaneHealthz(timeout time.Duration) bool {
	client := &http.Client{Timeout: 2 * time.Second}
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		resp, err := client.Get("http://127.0.0.1:4839/healthz")
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode >= 200 && resp.StatusCode < 300 {
				return true
			}
		}
		time.Sleep(500 * time.Millisecond)
	}
	return false
}

// validateControlPlaneConfig 校验控制面声明：kind 匹配 + spec.networking 五键
// 必填且格式合法（CIDR / IP / DHCP 起止对）。其余块（pki/serverCert/boot 等）
// 有默认值，由控制面 pydantic 注入，此处不要求显式写出。
func validateControlPlaneConfig(data []byte, path string) error {
	var root yaml.Node
	if err := yaml.Unmarshal(data, &root); err != nil {
		return fmt.Errorf("解析 %s: %w", path, err)
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
