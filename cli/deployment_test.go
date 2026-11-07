// 部署记录与状态根解析：记录往返/校验、优先级、仓库迁移与旧项目名归一化。
package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDeploymentRecordRoundTrip(t *testing.T) {
	stubUserHome(t)
	path := filepath.Join(t.TempDir(), "deployment.yaml")
	t.Setenv("KURRENT_DEPLOYMENT_FILE", path)

	rec := &deploymentRecord{
		RepoRoot: "/srv/kurrent", StateDir: "/home/u/.local/state/kurrent",
		Roles: deploymentRoles{ControlPlane: true, Storage: true}, Status: deployStatusReady,
	}
	if err := writeDeploymentRecord(rec); err != nil {
		t.Fatal(err)
	}
	got, err := readDeploymentRecord()
	if err != nil {
		t.Fatal(err)
	}
	if got == nil || got.Project != defaultComposeProject || got.StorageProject != defaultStorageProject {
		t.Fatalf("record = %+v, want pinned default projects", got)
	}
	if got.Roles != rec.Roles || got.Status != deployStatusReady || got.RepoRoot != rec.RepoRoot {
		t.Fatalf("record round-trip mismatch: %+v", got)
	}
	if got.UpdatedAt == "" {
		t.Error("updatedAt should be set")
	}
}

func TestDeploymentRecordRejectsRelativePaths(t *testing.T) {
	stubUserHome(t)
	path := filepath.Join(t.TempDir(), "deployment.yaml")
	t.Setenv("KURRENT_DEPLOYMENT_FILE", path)
	body := "apiVersion: kurrent.io/v1\nkind: DeploymentRecord\nrepoRoot: relative/repo\nstateDir: /abs/state\n"
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := readDeploymentRecord(); err == nil || !strings.Contains(err.Error(), "absolute") {
		t.Fatalf("err = %v, want absolute path rejection", err)
	}
}

func TestResolveDeploymentPrecedence(t *testing.T) {
	repo, _, _, _ := newFakeRepo(t)
	recordFile := os.Getenv("KURRENT_DEPLOYMENT_FILE")
	stateFromEnv := os.Getenv("KURRENT_STATE_DIR")

	// --state-dir 显式优先于环境变量与记录
	explicit := t.TempDir()
	q, err := resolveDeployment(repo, explicit)
	if err != nil {
		t.Fatal(err)
	}
	if q.stateDir != explicit {
		t.Fatalf("state = %q, want explicit %q", q.stateDir, explicit)
	}

	// 记录次之：清掉环境变量后读记录
	t.Setenv("KURRENT_STATE_DIR", "")
	recorded := t.TempDir()
	rec := &deploymentRecord{
		RepoRoot: repo, StateDir: recorded,
		Roles: deploymentRoles{ControlPlane: true}, Status: deployStatusReady,
	}
	if err := writeDeploymentRecord(rec); err != nil {
		t.Fatal(err)
	}
	q, err = resolveDeployment(repo, "")
	if err != nil {
		t.Fatal(err)
	}
	if q.stateDir != recorded || q.record == nil {
		t.Fatalf("state = %q record=%v, want recorded %q", q.stateDir, q.record, recorded)
	}

	// 无记录无环境变量：XDG_STATE_HOME/kurrent
	if err := os.Remove(recordFile); err != nil {
		t.Fatal(err)
	}
	xdg := t.TempDir()
	t.Setenv("XDG_STATE_HOME", xdg)
	q, err = resolveDeployment(repo, "")
	if err != nil {
		t.Fatal(err)
	}
	if q.stateDir != filepath.Join(xdg, "kurrent") {
		t.Fatalf("state = %q, want XDG default", q.stateDir)
	}
	t.Setenv("KURRENT_STATE_DIR", stateFromEnv)
}

func TestResolveRepoRootUsesRecordBeforeCwd(t *testing.T) {
	repo, _, _, _ := newFakeRepo(t)
	rec := &deploymentRecord{
		RepoRoot: repo, StateDir: os.Getenv("KURRENT_STATE_DIR"),
		Roles: deploymentRoles{}, Status: deployStatusReady,
	}
	if err := writeDeploymentRecord(rec); err != nil {
		t.Fatal(err)
	}
	t.Chdir(t.TempDir())
	got, err := resolveRepoRoot("")
	if err != nil {
		t.Fatal(err)
	}
	if got != repo {
		t.Fatalf("resolveRepoRoot = %q, want record repo %q", got, repo)
	}
}

func TestResolveDeploymentRelocation(t *testing.T) {
	oldRepo, _, _, _ := newFakeRepo(t)
	state := os.Getenv("KURRENT_STATE_DIR")
	if err := writeDeploymentRecord(&deploymentRecord{
		RepoRoot: oldRepo, StateDir: state, Roles: deploymentRoles{ControlPlane: true},
		Status: deployStatusReady,
	}); err != nil {
		t.Fatal(err)
	}
	// 新 checkout：显式传入新仓库根，状态根沿用记录（--dir <new-checkout>）
	newRepo := t.TempDir()
	for _, d := range []string{"control_plane", "storager"} {
		if err := os.MkdirAll(filepath.Join(newRepo, d), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("KURRENT_STATE_DIR", "")
	q, err := resolveDeployment(newRepo, "")
	if err != nil {
		t.Fatal(err)
	}
	if q.repoRoot != newRepo || q.stateDir != state {
		t.Fatalf("q = %+v, want repo=%s state=%s", q, newRepo, state)
	}
	if q.record == nil || q.record.RepoRoot != oldRepo {
		t.Fatal("the old record should be visible so init can detect relocation")
	}
}

func TestNormalizeComposeProject(t *testing.T) {
	cases := map[string]string{
		"Kurrent":           "kurrent",
		"My Repo!":          "myrepo",
		"kurrent-2":         "kurrent-2",
		"---weird.name":     "weirdname",
		"__hidden":          "hidden",
		"repo/with/slashes": "repowithslashes",
		"UPPER_case-123":    "upper_case-123",
	}
	for in, want := range cases {
		if got := normalizeComposeProject(in); got != want {
			t.Errorf("normalizeComposeProject(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestComposeEnvDefaults(t *testing.T) {
	stubUserHome(t)
	t.Setenv("KURRENT_STATE_DIR", "/srv/state")
	t.Setenv("KURRENT_HTTPS_PORT", "")
	q, err := resolveDeployment(filepath.Join(".."), "")
	if err != nil {
		t.Fatal(err)
	}
	env := q.composeEnv()
	if !containsKV(env, "KURRENT_STATE_DIR=/srv/state") || !containsKV(env, "KURRENT_HTTPS_PORT=443") {
		t.Fatalf("env = %v", env)
	}
}

func TestWriteJoinRecordStatus(t *testing.T) {
	stubUserHome(t)
	t.Setenv("KURRENT_DEPLOYMENT_FILE", filepath.Join(t.TempDir(), "deployment.yaml"))

	// 同机部署：join 只补 storage 角色，控制面的部署状态仍由 init 维护
	q := deployQuery{
		repoRoot: "/srv/kurrent",
		stateDir: "/home/u/.local/state/kurrent",
		record:   &deploymentRecord{Roles: deploymentRoles{ControlPlane: true}, Status: deployStatusReady},
	}
	if err := writeJoinRecord(q, deployStatusBootstrapping); err != nil {
		t.Fatal(err)
	}
	got, err := readDeploymentRecord()
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != deployStatusReady {
		t.Errorf("status = %q, want the control-plane status %q", got.Status, deployStatusReady)
	}
	if !got.Roles.ControlPlane || !got.Roles.Storage {
		t.Errorf("roles = %+v, want controlPlane and storage", got.Roles)
	}

	// 纯存储节点：没有控制面记录时 join 自己的状态就是最终状态
	if err := os.Remove(deploymentRecordPath()); err != nil {
		t.Fatal(err)
	}
	if err := writeJoinRecord(deployQuery{repoRoot: "/srv/kurrent", stateDir: "/srv/state"}, deployStatusReady); err != nil {
		t.Fatal(err)
	}
	got, err = readDeploymentRecord()
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != deployStatusReady || got.Roles.ControlPlane || !got.Roles.Storage {
		t.Fatalf("record = %+v, want storage-only ready", got)
	}
}

func TestDeploymentShowEnvIncludesDiskDir(t *testing.T) {
	repo, _, storager, _ := newFakeRepo(t)
	state := os.Getenv("KURRENT_STATE_DIR")
	if err := writeDeploymentRecord(&deploymentRecord{
		RepoRoot: repo, StateDir: state, Roles: deploymentRoles{ControlPlane: true, Storage: true},
		Status: deployStatusReady,
	}); err != nil {
		t.Fatal(err)
	}
	declaration := "apiVersion: kurrent.io/v1\nkind: NodeConfiguration\nspec:\n  agent:\n    diskDir: /mnt/disks\n"
	if err := os.WriteFile(filepath.Join(storager, "kurrent.yaml"), []byte(declaration), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Chdir(t.TempDir()) // 仓库外执行也应按记录解析

	code, stdout, stderr := captureCLI(t, func() { deploymentShow([]string{"-o", "env"}) })
	if code != 0 {
		t.Fatalf("code = %d, stderr = %s", code, stderr)
	}
	for _, want := range []string{
		"export KURRENT_STATE_DIR='" + state + "'",
		"export KURRENT_DISK_DIR='/mnt/disks'",
	} {
		if !strings.Contains(stdout, want) {
			t.Errorf("stdout missing %q:\n%s", want, stdout)
		}
	}
}

func containsKV(env []string, want string) bool {
	for _, kv := range env {
		if kv == want {
			return true
		}
	}
	return false
}
