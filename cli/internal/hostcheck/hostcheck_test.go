// host check：角色矩阵、SKIP 语义、退出判定与 JSON 结构。
package hostcheck

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const testMountFormat = `{{range .Mounts}}{{.Source}}:{{.Destination}} {{end}}`

func commandKey(name string, args ...string) string {
	return strings.Join(append([]string{name}, args...), " ")
}

// fakeCommands 按完整命令行返回输出；未登记的命令视为不存在。
func fakeCommands(outputs map[string]string) func(string, ...string) (string, error) {
	return func(name string, args ...string) (string, error) {
		key := commandKey(name, args...)
		if out, ok := outputs[key]; ok {
			return out, nil
		}
		return "", fmt.Errorf("unexpected command: %s", key)
	}
}

func fakeTools(available ...string) func(string) (string, error) {
	set := map[string]bool{}
	for _, name := range available {
		set[name] = true
	}
	return func(name string) (string, error) {
		if set[name] {
			return "/usr/bin/" + name, nil
		}
		return "", fmt.Errorf("%s not found", name)
	}
}

func storageReadyEnv(t *testing.T) HostEnv {
	t.Helper()
	root := t.TempDir()
	mounts := filepath.Join(root, "mounts")
	if err := os.WriteFile(mounts, []byte("configfs /sys/kernel/config configfs rw 0 0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	modRoot := filepath.Join(root, "module")
	for _, m := range []string{"nvmet", "nvmet_tcp"} {
		if err := os.MkdirAll(filepath.Join(modRoot, m), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return HostEnv{
		ProcMounts:    mounts,
		SysModuleRoot: modRoot,
		TlshdVersion:  func() (string, error) { return "1.5.0", nil },
		lookPath:      fakeTools(),
		output:        fakeCommands(map[string]string{}),
		uid:           func() int { return 1000 },
	}
}

func statusOf(report HostReport, id string) CheckStatus {
	for _, c := range report.Checks {
		if c.ID == id {
			return c.Status
		}
	}
	return ""
}

func TestParseHostRole(t *testing.T) {
	for _, raw := range []string{"control", "storage", "all"} {
		if got, err := ParseHostRole(raw); err != nil || string(got) != raw {
			t.Errorf("ParseHostRole(%q) = %q, %v", raw, got, err)
		}
	}
	if _, err := ParseHostRole("worker"); err == nil {
		t.Fatal("unknown roles must be rejected")
	}
}

func TestInferHostRole(t *testing.T) {
	psKey := commandKey("docker", "ps", "--format", composePSFormat)
	cases := []struct {
		name   string
		ps     string
		want   HostRole
		wantEr bool
	}{
		{"control", "a|kurrent-control-plane|\n", RoleControl, false},
		{"storage", "a|storager-agent|\nb|nvmet-host|\n", RoleStorage, false},
		{"all", "a|kurrent-control-plane|\nb|nvmet-host|\nb|storager-agent|\n", RoleAll, false},
		{"none", "", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			env := HostEnv{
				lookPath: fakeTools("docker"),
				output:   fakeCommands(map[string]string{psKey: tc.ps}),
			}
			got, err := InferHostRole(env)
			if tc.wantEr {
				if err == nil || !strings.Contains(err.Error(), "--role") {
					t.Fatalf("err = %v, want a --role hint", err)
				}
				return
			}
			if err != nil || got != tc.want {
				t.Fatalf("InferHostRole = %q, %v; want %q", got, err, tc.want)
			}
		})
	}
}

func TestStorageChecksPass(t *testing.T) {
	report := RunHostChecks(storageReadyEnv(t), t.TempDir(), RoleStorage)
	for _, id := range []string{"storage.module.nvmet", "storage.module.nvmet-tcp", "storage.configfs", "storage.tlshd"} {
		if got := statusOf(report, id); got != checkOK {
			t.Errorf("%s = %s, want ok", id, got)
		}
	}
	if report.Summary.Fail != 0 {
		t.Fatalf("summary = %+v, want 0 fail", report.Summary)
	}
	if report.Blocks(false) {
		t.Fatal("all passing must not block")
	}
}

func TestStorageChecksFail(t *testing.T) {
	root := t.TempDir()
	mounts := filepath.Join(root, "mounts")
	if err := os.WriteFile(mounts, []byte("sysfs /sys sysfs rw 0 0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	env := HostEnv{
		ProcMounts:    mounts,
		SysModuleRoot: filepath.Join(root, "module"),
		TlshdVersion:  func() (string, error) { return "1.4.9", nil },
		lookPath:      fakeTools(),
		output:        fakeCommands(map[string]string{}),
		uid:           func() int { return 1000 },
	}
	report := RunHostChecks(env, root, RoleStorage)
	if report.Summary.Fail != 4 {
		t.Fatalf("summary = %+v, want 4 fail", report.Summary)
	}
	if !report.Blocks(false) {
		t.Fatal("FAIL must block")
	}
	// join 预检复用同一套检查：错误内容与提示保持一致
	old := HostEnvPaths
	HostEnvPaths = env
	t.Cleanup(func() { HostEnvPaths = old })
	err := HostPreflightNvmet(filepath.Join(root, "storager"))
	if err == nil {
		t.Fatal("unready host prerequisites must fail")
	}
	for _, want := range []string{"nvmet", "nvmet-tcp", "configfs", "tlshd version too old: 1.4.9", "host-prepare.sh"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error is missing %q:\n%v", want, err)
		}
	}
}

func TestListenerRoleMatrix(t *testing.T) {
	// 4840 只对存储节点放行，443 只对控制面放行；回环监听不参与判定。
	ssOutput := "LISTEN 0 128 0.0.0.0:4840 0.0.0.0:*\n" +
		"LISTEN 0 128 0.0.0.0:443 0.0.0.0:*\n" +
		"LISTEN 0 128 127.0.0.1:3306 0.0.0.0:*\n"
	env := storageReadyEnv(t)
	env.lookPath = fakeTools("ss")
	env.output = fakeCommands(map[string]string{commandKey("ss", "-H", "-lnt"): ssOutput})
	env.uid = func() int { return 1000 }

	control := RunHostChecks(env, t.TempDir(), RoleControl)
	if got := statusOf(control, "host.listeners"); got != checkFail {
		t.Fatalf("control role: listener=%s, 4840 must not be allowed for the control role", got)
	}
	storage := RunHostChecks(env, t.TempDir(), RoleStorage)
	if got := statusOf(storage, "host.listeners"); got != checkFail {
		t.Fatalf("storage role: listener=%s, 443 must not be allowed for the storage role", got)
	}
	all := RunHostChecks(env, t.TempDir(), RoleAll)
	if got := statusOf(all, "host.listeners"); got != checkOK {
		t.Fatalf("all role: listener=%s, 4840/443 must both be allowed", got)
	}
}

func TestMissingComposeLabelsAreSkip(t *testing.T) {
	// 容器存在但没有 compose 服务标签：不能当成控制面容器而“通过”。
	env := HostEnv{
		lookPath: fakeTools("docker", "ss"),
		uid:      func() int { return 1000 },
		output: fakeCommands(map[string]string{
			commandKey("docker", "info"):                            "Server Version: 28\n",
			commandKey("docker", "ps", "--format", composePSFormat): "abc||0.0.0.0:80->80/tcp\n",
			commandKey("ss", "-H", "-lnt"):                          "",
		}),
	}
	report := RunHostChecks(env, t.TempDir(), RoleControl)
	for _, id := range []string{"control.control-plane", "control.docker-socket", "control.nginx-pki"} {
		if got := statusOf(report, id); got != checkSkip {
			t.Errorf("%s = %s, want skip", id, got)
		}
	}
}

func TestSkipIsNotPass(t *testing.T) {
	// 无 root + 无 docker + 无凭据目录：所有容器/防火墙/凭据检查为 SKIP。
	// 默认不阻断，--strict 必须阻断（SKIP 不计为通过）。
	env := HostEnv{
		lookPath: fakeTools("ss"),
		output:   fakeCommands(map[string]string{commandKey("ss", "-H", "-lnt"): ""}),
		uid:      func() int { return 1000 },
	}
	report := RunHostChecks(env, t.TempDir(), RoleControl)
	if report.Summary.Fail != 0 || report.Summary.Skip == 0 {
		t.Fatalf("summary = %+v, want 0 fail / >0 skip", report.Summary)
	}
	if report.Blocks(false) {
		t.Fatal("SKIP must not block by default")
	}
	if !report.Blocks(true) {
		t.Fatal("--strict must treat SKIP as a failure")
	}
}

func TestControlContainerChecks(t *testing.T) {
	psKey := commandKey("docker", "ps", "--format", composePSFormat)
	cpInspectKey := commandKey("docker", "inspect", "cp1", "--format", testMountFormat)
	webuiInspectKey := commandKey("docker", "inspect", "w1", "--format", testMountFormat)
	env := HostEnv{
		lookPath: fakeTools("docker"),
		uid:      func() int { return 1000 },
		output: fakeCommands(map[string]string{
			commandKey("docker", "info"): "Server Version: 28\n",
			psKey: "cp1|kurrent-control-plane|0.0.0.0:80->80/tcp\n" +
				"w1|kurrent-nginx|",
			cpInspectKey:    "/var/run/docker.sock:/var/run/docker.sock ",
			webuiInspectKey: "/srv/kurrent/control_plane/state/pki/ca:/etc/nginx/ca ",
		}),
	}
	report := RunHostChecks(env, t.TempDir(), RoleControl)
	for _, id := range []string{"control.control-plane", "control.docker-socket", "control.nginx-pki"} {
		if got := statusOf(report, id); got != checkFail {
			t.Errorf("%s = %s, want fail", id, got)
		}
	}
}

func TestControlContainerChecksPass(t *testing.T) {
	psKey := commandKey("docker", "ps", "--format", composePSFormat)
	cpInspectKey := commandKey("docker", "inspect", "cp1", "--format", testMountFormat)
	webuiInspectKey := commandKey("docker", "inspect", "w1", "--format", testMountFormat)
	env := HostEnv{
		lookPath: fakeTools("docker"),
		uid:      func() int { return 1000 },
		output: fakeCommands(map[string]string{
			commandKey("docker", "info"): "Server Version: 28\n",
			psKey: "cp1|kurrent-control-plane|\n" +
				"w1|kurrent-nginx|",
			cpInspectKey:    "/srv/control_plane/state/certs:/etc/nginx/certs ",
			webuiInspectKey: "/srv/control_plane/state/pki/nginx:/etc/nginx/pki ",
		}),
	}
	report := RunHostChecks(env, t.TempDir(), RoleControl)
	for _, id := range []string{"control.control-plane", "control.docker-socket", "control.nginx-pki", "host.docker-daemon"} {
		if got := statusOf(report, id); got != checkOK {
			t.Errorf("%s = %s, want ok", id, got)
		}
	}
}

func TestHostReportJSON(t *testing.T) {
	report := RunHostChecks(storageReadyEnv(t), t.TempDir(), RoleStorage)
	report.Strict = true
	var buf bytes.Buffer
	if err := PrintHostReport(&buf, report, true); err != nil {
		t.Fatal(err)
	}
	var decoded HostReport
	if err := json.Unmarshal(buf.Bytes(), &decoded); err != nil {
		t.Fatalf("invalid JSON: %v\n%s", err, buf.String())
	}
	if decoded.Role != "storage" || !decoded.Strict || decoded.Summary.OK != 4 {
		t.Fatalf("decoded = %+v", decoded)
	}
	if decoded.Summary.Fail != 0 {
		t.Fatalf("decoded summary = %+v, want 0 fail", decoded.Summary)
	}
	if statusOf(decoded, "storage.module.nvmet") != checkOK || statusOf(decoded, "host.listeners") == "" {
		t.Fatalf("checks = %+v", decoded.Checks)
	}
}

func TestCredentialChecks(t *testing.T) {
	root := t.TempDir()
	state := filepath.Join(root, "control_plane", "state")
	caDir := filepath.Join(state, "pki", "ca", "nodes")
	if err := os.MkdirAll(caDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(state, "users.yml"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(caDir, "ca.key"), []byte("k"), 0o644); err != nil {
		t.Fatal(err)
	}
	env := HostEnv{
		ControlStateDir: state,
		lookPath:        fakeTools(),
		output:          fakeCommands(map[string]string{}),
		uid:             func() int { return 1000 },
	}
	report := RunHostChecks(env, root, RoleControl)
	if got := statusOf(report, "control.credentials.users.yml"); got != checkOK {
		t.Errorf("users.yml = %s, want ok", got)
	}
	if got := statusOf(report, "control.credentials.ca-key.nodes"); got != checkFail {
		t.Errorf("ca.key = %s, want fail (private keys must be 0600)", got)
	}
}

func TestParseTlshdVersion(t *testing.T) {
	ver, err := parseTlshdVersion("tlshd, built from ktls-utils 1.5.0 on Oct 27 2026 13:20:47")
	if err != nil || ver != "1.5.0" {
		t.Fatalf("parse = %q, %v; want 1.5.0", ver, err)
	}
	if _, err := parseTlshdVersion("tlshd, unknown build"); err == nil {
		t.Fatal("unparseable output must be rejected")
	}
}

func TestVersionAtLeast(t *testing.T) {
	cases := []struct {
		v, min string
		want   bool
	}{
		{"1.5.0", "1.5.0", true},
		{"1.5.1", "1.5.0", true},
		{"1.10.0", "1.5.0", true},
		{"2.0.0", "1.5.0", true},
		{"1.4.9", "1.5.0", false},
		{"1.4", "1.5.0", false},
		{"1.5", "1.5.0", true},
	}
	for _, tc := range cases {
		if got := versionAtLeast(tc.v, tc.min); got != tc.want {
			t.Errorf("versionAtLeast(%q, %q) = %v; want %v", tc.v, tc.min, got, tc.want)
		}
	}
}

func TestParseExecStartPath(t *testing.T) {
	out := "{ path=/usr/local/sbin/tlshd ; argv[]=/usr/local/sbin/tlshd -c /etc/tlshd/config ; ignore_errors=no ; }"
	if got := parseExecStartPath(out); got != "/usr/local/sbin/tlshd" {
		t.Fatalf("parseExecStartPath = %q", got)
	}
	if got := parseExecStartPath(""); got != "" {
		t.Fatalf("empty output should yield an empty path: %q", got)
	}
}
