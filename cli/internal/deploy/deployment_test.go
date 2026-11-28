// 部署记录与状态根解析：记录往返/校验、优先级、仓库迁移与旧项目名归一化。
package deploy

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"kurrent/cli/internal/fsx"
)

// fakeDeployRepo 构造最小仓库布局与隔离的部署记录/状态根环境。
func fakeDeployRepo(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	oldHome := fsx.UserHomeDir
	fsx.UserHomeDir = func() string { return home }
	t.Cleanup(func() { fsx.UserHomeDir = oldHome })
	t.Setenv("KURRENT_STATE_DIR", t.TempDir())
	t.Setenv("KURRENT_DEPLOYMENT_FILE", filepath.Join(home, "deployment.yaml"))
	repo := t.TempDir()
	for _, d := range []string{"control_plane/state", "storager"} {
		if err := os.MkdirAll(filepath.Join(repo, d), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return repo
}

func TestDeploymentRecordRoundTrip(t *testing.T) {
	t.Setenv("KURRENT_DEPLOYMENT_FILE", filepath.Join(t.TempDir(), "deployment.yaml"))

	rec := &DeploymentRecord{
		RepoRoot: "/srv/kurrent", StateDir: "/home/u/.local/state/kurrent",
		Roles: DeploymentRoles{ControlPlane: true, Storage: true}, Status: StatusReady,
	}
	if err := WriteDeploymentRecord(rec); err != nil {
		t.Fatal(err)
	}
	got, err := ReadDeploymentRecord()
	if err != nil {
		t.Fatal(err)
	}
	if got == nil || got.Project != DefaultComposeProject || got.StorageProject != DefaultStorageProject {
		t.Fatalf("record = %+v, want pinned default projects", got)
	}
	if got.Roles != rec.Roles || got.Status != StatusReady || got.RepoRoot != rec.RepoRoot {
		t.Fatalf("record round-trip mismatch: %+v", got)
	}
	if got.UpdatedAt == "" {
		t.Error("updatedAt should be set")
	}
}

func TestDeploymentRecordRejectsRelativePaths(t *testing.T) {
	path := filepath.Join(t.TempDir(), "deployment.yaml")
	t.Setenv("KURRENT_DEPLOYMENT_FILE", path)
	body := "apiVersion: kurrent.io/v1\nkind: DeploymentRecord\nrepoRoot: relative/repo\nstateDir: /abs/state\n"
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := ReadDeploymentRecord(); err == nil || !strings.Contains(err.Error(), "absolute") {
		t.Fatalf("err = %v, want absolute path rejection", err)
	}
}

func TestResolveDeploymentPrecedence(t *testing.T) {
	repo := fakeDeployRepo(t)
	recordFile := os.Getenv("KURRENT_DEPLOYMENT_FILE")
	stateFromEnv := os.Getenv("KURRENT_STATE_DIR")

	// --state-dir 显式优先于环境变量与记录
	explicit := t.TempDir()
	q, err := ResolveDeployment(repo, explicit)
	if err != nil {
		t.Fatal(err)
	}
	if q.StateDir != explicit {
		t.Fatalf("state = %q, want explicit %q", q.StateDir, explicit)
	}

	// 记录次之：清掉环境变量后读记录
	t.Setenv("KURRENT_STATE_DIR", "")
	recorded := t.TempDir()
	rec := &DeploymentRecord{
		RepoRoot: repo, StateDir: recorded,
		Roles: DeploymentRoles{ControlPlane: true}, Status: StatusReady,
	}
	if err := WriteDeploymentRecord(rec); err != nil {
		t.Fatal(err)
	}
	q, err = ResolveDeployment(repo, "")
	if err != nil {
		t.Fatal(err)
	}
	if q.StateDir != recorded || q.Record == nil {
		t.Fatalf("state = %q record=%v, want recorded %q", q.StateDir, q.Record, recorded)
	}

	// 无记录无环境变量：XDG_STATE_HOME/kurrent
	if err := os.Remove(recordFile); err != nil {
		t.Fatal(err)
	}
	xdg := t.TempDir()
	t.Setenv("XDG_STATE_HOME", xdg)
	q, err = ResolveDeployment(repo, "")
	if err != nil {
		t.Fatal(err)
	}
	if q.StateDir != filepath.Join(xdg, "kurrent") {
		t.Fatalf("state = %q, want XDG default", q.StateDir)
	}
	t.Setenv("KURRENT_STATE_DIR", stateFromEnv)
}

func TestResolveRepoRootUsesRecordBeforeCwd(t *testing.T) {
	repo := fakeDeployRepo(t)
	rec := &DeploymentRecord{
		RepoRoot: repo, StateDir: os.Getenv("KURRENT_STATE_DIR"),
		Roles: DeploymentRoles{}, Status: StatusReady,
	}
	if err := WriteDeploymentRecord(rec); err != nil {
		t.Fatal(err)
	}
	t.Chdir(t.TempDir())
	got, err := ResolveRepoRoot("")
	if err != nil {
		t.Fatal(err)
	}
	if got != repo {
		t.Fatalf("ResolveRepoRoot = %q, want record repo %q", got, repo)
	}
}

func TestResolveDeploymentRelocation(t *testing.T) {
	oldRepo := fakeDeployRepo(t)
	state := os.Getenv("KURRENT_STATE_DIR")
	if err := WriteDeploymentRecord(&DeploymentRecord{
		RepoRoot: oldRepo, StateDir: state, Roles: DeploymentRoles{ControlPlane: true},
		Status: StatusReady,
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
	q, err := ResolveDeployment(newRepo, "")
	if err != nil {
		t.Fatal(err)
	}
	if q.RepoRoot != newRepo || q.StateDir != state {
		t.Fatalf("q = %+v, want repo=%s state=%s", q, newRepo, state)
	}
	if q.Record == nil || q.Record.RepoRoot != oldRepo {
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
		if got := NormalizeComposeProject(in); got != want {
			t.Errorf("NormalizeComposeProject(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestComposeEnvDefaults(t *testing.T) {
	repo := fakeDeployRepo(t)
	t.Setenv("KURRENT_STATE_DIR", "/srv/state")
	t.Setenv("KURRENT_HTTPS_PORT", "")
	q, err := ResolveDeployment(repo, "")
	if err != nil {
		t.Fatal(err)
	}
	env := q.ComposeEnv()
	if !containsKV(env, "KURRENT_STATE_DIR=/srv/state") || !containsKV(env, "KURRENT_HTTPS_PORT=443") {
		t.Fatalf("env = %v", env)
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
