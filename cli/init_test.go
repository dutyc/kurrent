// kurrent init / config print 沙盒测试：临时仓库 + fake docker，验证声明校验、
// 应用（--config 其他路径 → control_plane/kurrent.yaml）、收敛启动调用序列、
// 模板输出与 example 同源。
package main

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// cpYAML 完整控制面声明（模板编辑后的形态：networking 五键 + 默认块可省）。
const cpYAML = `apiVersion: kurrent.io/v1
kind: ControlPlaneConfiguration
metadata:
  name: kurrent-cp
spec:
  networking:
    interface: enp3s0
    subnet: 192.168.80.0/24
    dhcpRange: 192.168.80.50,192.168.80.100
    gateway: 192.168.80.2
    dns: 223.5.5.5
`

// writeControlPlaneConfig 把声明写入 control_plane/kurrent.yaml。
func writeControlPlaneConfig(t *testing.T, cpDir, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(cpDir, "kurrent.yaml"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestInitBasic(t *testing.T) {
	repo, cp, _, logFile := newFakeRepo(t)
	writeControlPlaneConfig(t, cp, cpYAML)
	err := initRun(initOpts{dir: cp, configFile: filepath.Join(cp, "kurrent.yaml"), healthWait: 500 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	// 声明原样保留（init 不改写用户文件）
	data, err := os.ReadFile(filepath.Join(cp, "kurrent.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), "192.168.80.0/24") {
		t.Error("kurrent.yaml 被 init 改写")
	}
	// 收敛序列：compose up -d（仓库根）→ restart 控制面 → restart dnsmasq
	logData, err := os.ReadFile(logFile)
	if err != nil {
		t.Fatal(err)
	}
	log := string(logData)
	for _, want := range []string{
		"FAKE-DOCKER " + repo + " compose up -d",
		"FAKE-DOCKER " + repo + " compose restart kurrent-control-plane",
		"FAKE-DOCKER " + repo + " compose restart kurrent-dnsmasq",
	} {
		if !strings.Contains(log, want) {
			t.Errorf("docker 调用缺 %s\n%s", want, log)
		}
	}
}

func TestInitIdempotentRerun(t *testing.T) {
	// 声明收敛语义（配置即声明）：kurrent.yaml 已存在 = 正常重跑（改声明后重跑生效），不拒绝
	_, cp, _, _ := newFakeRepo(t)
	writeControlPlaneConfig(t, cp, cpYAML)
	for i := 0; i < 2; i++ {
		if err := initRun(initOpts{dir: cp, configFile: filepath.Join(cp, "kurrent.yaml"), healthWait: 500 * time.Millisecond}); err != nil {
			t.Fatalf("第 %d 次重跑失败: %v", i+1, err)
		}
	}
}

func TestInitMissingConfig(t *testing.T) {
	_, cp, _, _ := newFakeRepo(t)
	err := initRun(initOpts{dir: cp, configFile: filepath.Join(cp, "kurrent.yaml")})
	if err == nil || !strings.Contains(err.Error(), "print init-defaults") {
		t.Fatalf("err = %v, want print init-defaults 指引", err)
	}
}

func TestInitAppliesExternalConfig(t *testing.T) {
	repo, cp, _, _ := newFakeRepo(t)
	external := filepath.Join(repo, "cluster.yaml")
	if err := os.WriteFile(external, []byte(cpYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	// --config 指向其他路径：校验后应用为 control_plane/kurrent.yaml（compose 挂载权威位）
	if err := initRun(initOpts{dir: cp, configFile: external, healthWait: 500 * time.Millisecond}); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(cp, "kurrent.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), "192.168.80.0/24") {
		t.Error("外部声明未应用到 control_plane/kurrent.yaml")
	}
}

func TestInitValidatesSubnet(t *testing.T) {
	_, cp, _, _ := newFakeRepo(t)
	writeControlPlaneConfig(t, cp, strings.Replace(cpYAML, "192.168.80.0/24", "192.168.80.0", 1))
	err := initRun(initOpts{dir: cp, configFile: filepath.Join(cp, "kurrent.yaml")})
	if err == nil || !strings.Contains(err.Error(), "CIDR") {
		t.Fatalf("err = %v, want CIDR error", err)
	}
}

func TestInitValidatesDhcpRange(t *testing.T) {
	_, cp, _, _ := newFakeRepo(t)
	writeControlPlaneConfig(t, cp, strings.Replace(cpYAML, "192.168.80.50,192.168.80.100", "192.168.80.50", 1))
	err := initRun(initOpts{dir: cp, configFile: filepath.Join(cp, "kurrent.yaml")})
	if err == nil || !strings.Contains(err.Error(), "dhcpRange") {
		t.Fatalf("err = %v, want dhcpRange error", err)
	}
}

func TestInitValidatesGatewayDNS(t *testing.T) {
	_, cp, _, _ := newFakeRepo(t)
	writeControlPlaneConfig(t, cp, strings.Replace(cpYAML, "192.168.80.2", "not-an-ip", 1))
	err := initRun(initOpts{dir: cp, configFile: filepath.Join(cp, "kurrent.yaml")})
	if err == nil || !strings.Contains(err.Error(), "gateway") {
		t.Fatalf("err = %v, want gateway error", err)
	}
}

func TestInitMissingNetworkingKeys(t *testing.T) {
	_, cp, _, _ := newFakeRepo(t)
	writeControlPlaneConfig(t, cp, strings.Replace(cpYAML, "    interface: enp3s0\n", "", 1))
	err := initRun(initOpts{dir: cp, configFile: filepath.Join(cp, "kurrent.yaml")})
	if err == nil || !strings.Contains(err.Error(), "spec.networking.interface") {
		t.Fatalf("err = %v, want networking.interface 缺失报错", err)
	}
}

func TestResolveControlPlaneDir(t *testing.T) {
	repo, cp, _, _ := newFakeRepo(t)
	// --dir 显式
	if got, err := resolveControlPlaneDir(cp); err != nil || got != cp {
		t.Fatalf("resolveControlPlaneDir(%s) = %q, %v", cp, got, err)
	}
	// 仓库根（cwd 下存在 control_plane/）
	t.Chdir(repo)
	if got, err := resolveControlPlaneDir(""); err != nil || got != cp {
		t.Fatalf("仓库根解析 = %q, %v, want %q", got, err, cp)
	}
	// 已位于 control_plane 内（cwd 下存在 config/）
	t.Chdir(cp)
	if got, err := resolveControlPlaneDir(""); err != nil || got != cp {
		t.Fatalf("control_plane 内解析 = %q, %v, want %q", got, err, cp)
	}
	// 找不到
	t.Chdir(t.TempDir())
	if _, err := resolveControlPlaneDir(""); err == nil {
		t.Fatal("期望找不到 control_plane 目录报错")
	}
}

// captureStdout 捕获 fn 执行期间的 os.Stdout 输出。
func captureStdout(t *testing.T, fn func()) string {
	t.Helper()
	old := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = w
	fn()
	w.Close()
	os.Stdout = old
	data, err := io.ReadAll(r)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestPrintInitDefaults(t *testing.T) {
	got := captureStdout(t, func() { cmdConfig([]string{"print", "init-defaults"}) })
	example, err := os.ReadFile(filepath.Join("..", "control_plane", "kurrent.yaml.example"))
	if err != nil {
		t.Fatalf("读取 example: %v", err)
	}
	if got != string(example) {
		t.Errorf("print init-defaults 输出与 kurrent.yaml.example 不同源（改注释先改 example）")
	}
	for _, want := range []string{
		"kind: ControlPlaneConfiguration", "interface:", "dhcpRange:", "bootstrapTokenTtlMinutes:",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("print init-defaults 缺 %q", want)
		}
	}
}

func TestPrintNodeDefaults(t *testing.T) {
	got := captureStdout(t, func() { cmdConfig([]string{"print", "node-defaults"}) })
	example, err := os.ReadFile(filepath.Join("..", "storager", "kurrent.yaml.example"))
	if err != nil {
		t.Fatalf("读取 example: %v", err)
	}
	if got != string(example) {
		t.Errorf("print node-defaults 输出与 kurrent.yaml.example 不同源（改注释先改 example）")
	}
	for _, want := range []string{
		"kind: NodeConfiguration", "backend:", "diskDir:", "nqnBase:", "controlPlane:",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("print node-defaults 缺 %q", want)
		}
	}
}

// ── 声明生命周期：全量校验先于写盘、健康失败回滚、dnsmasq 起服核验 ──

func TestInitFullSchemaValidationFailureDoesNotApply(t *testing.T) {
	repo, cp, _, logFile := newFakeRepo(t)
	writeControlPlaneConfig(t, cp, cpYAML)
	external := filepath.Join(repo, "cluster.yaml")
	newBody := strings.Replace(cpYAML, "192.168.80.50,192.168.80.100", "192.168.80.60,192.168.80.90", 1)
	if err := os.WriteFile(external, []byte(newBody), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FAKE_DOCKER_RUN_EXIT", "3")
	err := initRun(initOpts{dir: cp, configFile: external, healthWait: 100 * time.Millisecond})
	if err == nil || !strings.Contains(err.Error(), "declaration validation failed") ||
		!strings.Contains(err.Error(), "unknownField") {
		t.Fatalf("err = %v, want 全量校验失败（含字段错误）", err)
	}
	data, _ := os.ReadFile(filepath.Join(cp, "kurrent.yaml"))
	if !strings.Contains(string(data), "192.168.80.50,192.168.80.100") {
		t.Error("校验失败却改写了 control_plane/kurrent.yaml")
	}
	log := readLog(t, logFile)
	if strings.Contains(log, "compose up -d") || strings.Contains(log, "compose restart") {
		t.Errorf("校验失败后仍启动了控制面:\n%s", log)
	}
}

func TestInitRollsBackOnHealthFailure(t *testing.T) {
	repo, cp, _, logFile := newFakeRepo(t)
	writeControlPlaneConfig(t, cp, cpYAML)
	external := filepath.Join(repo, "cluster.yaml")
	newBody := strings.Replace(cpYAML, "192.168.80.50,192.168.80.100", "192.168.80.60,192.168.80.90", 1)
	if err := os.WriteFile(external, []byte(newBody), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FAKE_DOCKER_HEALTH", "unhealthy")
	err := initRun(initOpts{dir: cp, configFile: external, healthWait: 100 * time.Millisecond})
	if err == nil || !strings.Contains(err.Error(), "rolled back to the previous declaration") {
		t.Fatalf("err = %v, want 回滚提示", err)
	}
	data, _ := os.ReadFile(filepath.Join(cp, "kurrent.yaml"))
	if !strings.Contains(string(data), "192.168.80.50,192.168.80.100") {
		t.Error("健康检查失败后未恢复上一份声明")
	}
	log := readLog(t, logFile)
	if n := strings.Count(log, "compose restart kurrent-control-plane"); n < 2 {
		t.Errorf("期望应用重启 + 回滚重启两次控制面，实际 %d:\n%s", n, log)
	}
	if !strings.Contains(log, "compose restart kurrent-dnsmasq") {
		t.Errorf("回滚后应重启 dnsmasq 加载旧 conf:\n%s", log)
	}
}

func TestInitFailsWhenDnsmasqDoesNotStayRunning(t *testing.T) {
	repo, cp, _, _ := newFakeRepo(t)
	writeControlPlaneConfig(t, cp, cpYAML)
	external := filepath.Join(repo, "cluster.yaml")
	newBody := strings.Replace(cpYAML, "192.168.80.50,192.168.80.100", "192.168.80.60,192.168.80.90", 1)
	if err := os.WriteFile(external, []byte(newBody), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FAKE_DOCKER_RUNNING", "false")
	err := initRun(initOpts{dir: cp, configFile: external, healthWait: 100 * time.Millisecond})
	if err == nil || !strings.Contains(err.Error(), "dnsmasq did not stay running") {
		t.Fatalf("err = %v, want dnsmasq 起服失败", err)
	}
	data, _ := os.ReadFile(filepath.Join(cp, "kurrent.yaml"))
	if !strings.Contains(string(data), "192.168.80.50,192.168.80.100") {
		t.Error("dnsmasq 起服失败后未恢复上一份声明")
	}
}

func TestInitBuildFailureBeforeApply(t *testing.T) {
	repo, cp, _, _ := newFakeRepo(t)
	writeControlPlaneConfig(t, cp, cpYAML)
	external := filepath.Join(repo, "cluster.yaml")
	if err := os.WriteFile(external, []byte(cpYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FAKE_DOCKER_FAIL", "build")
	err := initRun(initOpts{dir: cp, configFile: external, healthWait: 100 * time.Millisecond})
	if err == nil || !strings.Contains(err.Error(), "build control-plane image failed") {
		t.Fatalf("err = %v, want 构建失败", err)
	}
}

func readLog(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}
