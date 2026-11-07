// 状态根准备：目录权限、种子文件、属主/可写性、WebUI 构建预检、旧状态检测与
// 逐文件校验迁移。
package main

import (
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func TestPrepareControlPlaneState(t *testing.T) {
	stateRoot := t.TempDir()
	paths := newControlPlanePaths(stateRoot)
	if err := prepareControlPlaneState(paths); err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{paths.root, paths.state, paths.config, paths.bootstrap} {
		fi, err := os.Stat(dir)
		if err != nil {
			t.Fatalf("%s: %v", dir, err)
		}
		if fi.Mode().Perm() != 0o700 {
			t.Errorf("%s mode = %04o, want 0700", dir, fi.Mode().Perm())
		}
	}
	for _, file := range []string{
		paths.agentsFile,
		filepath.Join(paths.dnsmasq, "dnsmasq.conf"),
		filepath.Join(paths.dnsmasq, "dhcp-hosts.conf"),
		filepath.Join(paths.dnsmasq, "dnsmasq.leases"),
	} {
		if !fileExists(file) {
			t.Errorf("seed file missing: %s", file)
		}
	}
	if !isDir(filepath.Join(paths.state, "certs")) || !isDir(filepath.Join(paths.state, "pki", "nginx")) {
		t.Error("nginx/control-plane derived directories should exist before compose up")
	}

	// 重跑不覆盖已有运行态台账
	if err := os.WriteFile(paths.agentsFile, []byte("agents:\n  node-1:\n    base_url: https://x\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := prepareControlPlaneState(paths); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(paths.agentsFile)
	if !strings.Contains(string(data), "node-1") {
		t.Error("seedFile must not overwrite the existing ledger")
	}
}

func TestPrepareStoragerState(t *testing.T) {
	paths := newStoragerPaths(t.TempDir(), "aabbccddeeff")
	if err := prepareStoragerState(paths); err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{paths.state, paths.bootstrap, paths.agentPKI, paths.nvmetPKI, paths.certs} {
		fi, err := os.Stat(dir)
		if err != nil {
			t.Fatalf("%s: %v", dir, err)
		}
		if fi.Mode().Perm() != 0o700 {
			t.Errorf("%s mode = %04o, want 0700", dir, fi.Mode().Perm())
		}
	}
	if paths.agentPKI != filepath.Join(paths.pki, "agent-aabbccddeeff") {
		t.Fatalf("agentPKI = %s", paths.agentPKI)
	}
}

func TestAssertRecordedStatePresent(t *testing.T) {
	stateRoot := filepath.Join(t.TempDir(), "gone")
	q := deployQuery{stateDir: stateRoot}
	if err := assertRecordedStatePresent(q); err != nil {
		t.Fatalf("no record should pass: %v", err)
	}
	q.record = &deploymentRecord{StateDir: stateRoot}
	err := assertRecordedStatePresent(q)
	if err == nil || !strings.Contains(err.Error(), "no longer exists") {
		t.Fatalf("err = %v, want missing state root rejection", err)
	}
	if err := os.MkdirAll(stateRoot, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := assertRecordedStatePresent(q); err != nil {
		t.Fatalf("existing state root should pass: %v", err)
	}
}

func TestAssertOwnedAndWritableRejectsForeignOwner(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state")
	if err := os.Mkdir(path, 0o700); err != nil {
		t.Fatal(err)
	}
	oldUID := effectiveUID
	effectiveUID = func() int { return os.Geteuid() + 1 }
	t.Cleanup(func() { effectiveUID = oldUID })

	err := assertOwnedAndWritable(path)
	if err == nil || !strings.Contains(err.Error(), "sudo chown -R") {
		t.Fatalf("err = %v, want chown remediation", err)
	}
}

func TestControlPlaneBuildReady(t *testing.T) {
	repo := t.TempDir()
	err := controlPlaneBuildReady(repo)
	if err == nil || !strings.Contains(err.Error(), "npm ci && npm run build") {
		t.Fatalf("err = %v, want build remediation", err)
	}
	dist := filepath.Join(repo, "webui", "app", "dist")
	if err := os.MkdirAll(dist, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dist, "index.html"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := controlPlaneBuildReady(repo); err != nil {
		t.Fatal(err)
	}
	oldUID := effectiveUID
	effectiveUID = func() int { return os.Geteuid() + 1 }
	t.Cleanup(func() { effectiveUID = oldUID })
	if err := controlPlaneBuildReady(repo); err == nil || !strings.Contains(err.Error(), "sudo rm -rf") {
		t.Fatalf("err = %v, want root-owned dist remediation", err)
	}
}

func TestDetectLegacyState(t *testing.T) {
	repo := t.TempDir()
	// 空目录不算旧状态（失败重跑残留）
	if err := os.MkdirAll(filepath.Join(repo, "control_plane", "state"), 0o755); err != nil {
		t.Fatal(err)
	}
	if detectLegacyControlPlaneState(repo).present() {
		t.Fatal("empty state directory should not count as legacy")
	}
	if err := os.WriteFile(filepath.Join(repo, "control_plane", "state", "workers.yml"), []byte("workers: {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := detectLegacyControlPlaneState(repo); !got.present() || len(got.paths) == 0 {
		t.Fatal("non-empty state directory should be detected")
	}
	storager := filepath.Join(repo, "storager")
	if err := os.MkdirAll(filepath.Join(storager, "bootstrap"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(storager, "bootstrap", "agent.token"), []byte("t"), 0o600); err != nil {
		t.Fatal(err)
	}
	if !detectLegacyStorageState(storager).present() {
		t.Fatal("legacy bootstrap should be detected")
	}
}

func TestCopyTreeVerified(t *testing.T) {
	src := t.TempDir()
	sub := filepath.Join(src, "pki", "users")
	if err := os.MkdirAll(sub, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sub, "client.key"), []byte("secret"), 0o600); err != nil {
		t.Fatal(err)
	}
	dst := filepath.Join(t.TempDir(), "copy")
	if err := copyTreeVerified(src, dst); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(filepath.Join(dst, "pki", "users", "client.key"))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "secret" {
		t.Fatalf("content = %q", got)
	}
	if fi, _ := os.Stat(filepath.Join(dst, "pki", "users", "client.key")); fi.Mode().Perm() != 0o600 {
		t.Errorf("mode = %04o, want 0600", fi.Mode().Perm())
	}

	// 符号链接拒绝（迁移不接受可指向任意位置的链接）
	if err := os.Symlink(filepath.Join(sub, "client.key"), filepath.Join(src, "link.key")); err != nil {
		t.Fatal(err)
	}
	if err := copyTreeVerified(src, filepath.Join(t.TempDir(), "copy2")); err == nil || !strings.Contains(err.Error(), "symlink") {
		t.Fatalf("err = %v, want symlink rejection", err)
	}
}

func TestCopyFileReplacesReadOnlyDestination(t *testing.T) {
	src := filepath.Join(t.TempDir(), "src")
	if err := os.WriteFile(src, []byte("new"), 0o600); err != nil {
		t.Fatal(err)
	}
	dst := filepath.Join(t.TempDir(), "dst")
	if err := os.WriteFile(dst, []byte("old"), 0o600); err != nil {
		t.Fatal(err)
	}
	// 模拟容器以 root 重写的目标：当前用户不可写，但目录可写（可 unlink）
	if err := os.Chmod(dst, 0o400); err != nil {
		t.Fatal(err)
	}
	if err := copyFile(src, dst, 0o600); err != nil {
		t.Fatalf("resume copy should replace an unwritable destination: %v", err)
	}
	got, err := os.ReadFile(dst)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "new" {
		t.Fatalf("content = %q, want new", got)
	}
}

func TestRemoveLegacyStateKeepsExamples(t *testing.T) {
	repo := t.TempDir()
	cp := legacyControlPlanePaths(repo)
	if err := prepareControlPlaneState(cp); err != nil {
		t.Fatal(err)
	}
	example := filepath.Join(repo, "dnsmasq", "dnsmasq.conf.example")
	if err := os.MkdirAll(filepath.Dir(example), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(example, []byte("# example\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := removeLegacyControlPlaneState(repo); err != nil {
		t.Fatal(err)
	}
	if isDir(cp.state) || fileExists(cp.agentsFile) || fileExists(filepath.Join(repo, "dnsmasq", "dnsmasq.conf")) {
		t.Error("legacy runtime state should be removed")
	}
	if !fileExists(example) {
		t.Error("tracked example files must be kept")
	}
}

func TestLegacyPreflightPathsIncludeStorageState(t *testing.T) {
	repo := t.TempDir()
	write := func(rel string) {
		t.Helper()
		p := filepath.Join(repo, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte("x\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write(filepath.Join("control_plane", "state", "workers.yml"))
	write(filepath.Join("storager", "bootstrap", "nvmet-host.token"))
	write(filepath.Join("storager", "agent", "logs", "nvmet-credentials.json"))

	got := legacyPreflightPaths(deployQuery{repoRoot: repo}, detectLegacyControlPlaneState(repo))
	for _, want := range []string{
		filepath.Join(repo, "control_plane", "state"),
		filepath.Join(repo, "storager", "bootstrap"),
		filepath.Join(repo, "storager", "agent", "logs"),
	} {
		found := false
		for _, p := range got {
			if p == want {
				found = true
			}
		}
		if !found {
			t.Errorf("preflight paths %v missing %s (storage state is migrated by init too)", got, want)
		}
	}
}

func TestPreflightAllowsResumingBootstrap(t *testing.T) {
	repo, _, _, _ := newFakeRepo(t)
	if err := os.WriteFile(filepath.Join(repo, "control_plane", "state", "workers.yml"), []byte("workers: {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	q, err := resolveDeployment(repo, "")
	if err != nil {
		t.Fatal(err)
	}
	legacy := detectLegacyControlPlaneState(repo)
	if !legacy.present() {
		t.Fatal("fixture should contain legacy control-plane state")
	}
	// 上一次 init 已把状态复制进状态根并在凭据交换阶段失败
	q.record = &deploymentRecord{
		RepoRoot: repo, StateDir: q.stateDir,
		Roles: deploymentRoles{ControlPlane: true}, Status: deployStatusBootstrapping,
	}
	if err := preflightControlPlane(q, legacy, true); err != nil {
		t.Fatalf("resuming an interrupted bootstrap should not require an empty state root: %v", err)
	}

	// 记录已是 ready 时仍拒绝向非空状态根迁移，避免覆盖另一套部署
	q.record.Status = deployStatusReady
	if err := preflightControlPlane(q, legacy, true); err == nil || !strings.Contains(err.Error(), "already contains data") {
		t.Fatalf("non-resumable adoption should be rejected: %v", err)
	}
}

func TestAssertLegacyOwnershipReportsAllRoots(t *testing.T) {
	root := t.TempDir()
	cpState := filepath.Join(root, "control_plane", "state")
	bootstrap := filepath.Join(root, "storager", "bootstrap")
	for _, dir := range []string{cpState, bootstrap} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "f"), []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	oldUID := effectiveUID
	effectiveUID = func() int { return os.Geteuid() + 1 }
	t.Cleanup(func() { effectiveUID = oldUID })

	err := assertLegacyOwnership([]string{cpState, bootstrap})
	if err == nil {
		t.Fatal("foreign ownership should be rejected")
	}
	msg := err.Error()
	for _, want := range []string{cpState, bootstrap, "sudo chown -R"} {
		if !strings.Contains(msg, want) {
			t.Errorf("error %q missing %q", msg, want)
		}
	}
	// 所有待修复的根合并为一条命令，避免用户逐次重跑才发现下一个
	if n := strings.Count(msg, "sudo chown -R"); n != 1 {
		t.Errorf("chown appears %d times, want a single combined command:\n%s", n, msg)
	}
}

func TestShellQuotePath(t *testing.T) {
	if got := shellQuotePath("/srv/state"); got != "/srv/state" {
		t.Errorf("plain path = %q", got)
	}
	if got := shellQuotePath("/srv/my state"); got != "'/srv/my state'" {
		t.Errorf("path with space = %q", got)
	}
	if got := shellQuotePath("/srv/it's"); got != `'/srv/it'\''s'` {
		t.Errorf("path with quote = %q", got)
	}
}

// syscall 引用：属主检查依赖 Stat_t（避免平台差异时静默跳过）。
var _ = syscall.Stat_t{}
