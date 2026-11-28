// 部署命令层：join 记录状态归并与 deployment show -o env 的插值输出。
package command

import (
	"kurrent/cli/internal/deploy"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestWriteJoinRecordStatus(t *testing.T) {
	stubUserHome(t)
	t.Setenv("KURRENT_DEPLOYMENT_FILE", filepath.Join(t.TempDir(), "deployment.yaml"))

	// 同机部署：join 只补 storage 角色，控制面的部署状态仍由 init 维护
	q := deploy.DeployQuery{
		RepoRoot: "/srv/kurrent",
		StateDir: "/home/u/.local/state/kurrent",
		Record:   &deploy.DeploymentRecord{Roles: deploy.DeploymentRoles{ControlPlane: true}, Status: deploy.StatusReady},
	}
	if err := writeJoinRecord(q, deploy.StatusBootstrapping); err != nil {
		t.Fatal(err)
	}
	got, err := deploy.ReadDeploymentRecord()
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != deploy.StatusReady {
		t.Errorf("status = %q, want the control-plane status %q", got.Status, deploy.StatusReady)
	}
	if !got.Roles.ControlPlane || !got.Roles.Storage {
		t.Errorf("roles = %+v, want controlPlane and storage", got.Roles)
	}

	// 纯存储节点：没有控制面记录时 join 自己的状态就是最终状态
	if err := os.Remove(deploy.DeploymentRecordPath()); err != nil {
		t.Fatal(err)
	}
	if err := writeJoinRecord(deploy.DeployQuery{RepoRoot: "/srv/kurrent", StateDir: "/srv/state"}, deploy.StatusReady); err != nil {
		t.Fatal(err)
	}
	got, err = deploy.ReadDeploymentRecord()
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != deploy.StatusReady || got.Roles.ControlPlane || !got.Roles.Storage {
		t.Fatalf("record = %+v, want storage-only ready", got)
	}
}

func TestDeploymentShowEnvIncludesDiskDir(t *testing.T) {
	repo, _, storager, _ := newFakeRepo(t)
	state := os.Getenv("KURRENT_STATE_DIR")
	if err := deploy.WriteDeploymentRecord(&deploy.DeploymentRecord{
		RepoRoot: repo, StateDir: state, Roles: deploy.DeploymentRoles{ControlPlane: true, Storage: true},
		Status: deploy.StatusReady,
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
