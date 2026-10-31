// config validate/show：kind 分派、仓库根解析、组件镜像内权威校验。
package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestConfigValidateControlPlane(t *testing.T) {
	repo, cp, _, logFile := newFakeRepo(t)
	writeControlPlaneConfig(t, cp, cpYAML)
	if err := configInspect("validate", parseArgs([]string{"--dir", repo})); err != nil {
		t.Fatal(err)
	}
	log := readLog(t, logFile)
	for _, want := range []string{
		"compose build kurrent-control-plane",
		"kurrent-control-plane -m app.config_tool validate",
	} {
		if !strings.Contains(log, want) {
			t.Errorf("缺调用 %s\n%s", want, log)
		}
	}
}

func TestConfigShowNode(t *testing.T) {
	repo, _, storager, logFile := newFakeRepo(t)
	if err := os.WriteFile(filepath.Join(storager, "kurrent.yaml"), []byte(nodeYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	var runErr error
	out := captureStdout(t, func() {
		runErr = configInspect("show", parseArgs([]string{"--dir", repo, "--node"}))
	})
	if runErr != nil {
		t.Fatal(runErr)
	}
	if !strings.Contains(out, "fake validation") {
		t.Errorf("show 未透传组件输出: %q", out)
	}
	log := readLog(t, logFile)
	if !strings.Contains(log, "storager-agent -m app.config_tool show") {
		t.Errorf("未调用节点镜像 show:\n%s", log)
	}
	if !strings.Contains(log, filepath.Join(repo, "storager", "nvmeof")+" compose build storager-agent") {
		t.Errorf("节点默认后端应使用 nvmeof 编排:\n%s", log)
	}
}

func TestConfigDispatchNodeBackendComposeDir(t *testing.T) {
	repo, _, storager, logFile := newFakeRepo(t)
	stgt := strings.Replace(nodeYAML, "backend: nvmet", "backend: stgt", 1)
	if err := os.WriteFile(filepath.Join(storager, "kurrent.yaml"), []byte(stgt), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := configInspect("validate", parseArgs([]string{"--dir", repo, "--node"})); err != nil {
		t.Fatal(err)
	}
	log := readLog(t, logFile)
	if !strings.Contains(log, filepath.Join(repo, "storager", "iscsi")+" compose build storager-agent") {
		t.Errorf("stgt 后端应使用 iscsi 编排:\n%s", log)
	}
}

func TestConfigValidationFailurePropagates(t *testing.T) {
	repo, cp, _, _ := newFakeRepo(t)
	writeControlPlaneConfig(t, cp, cpYAML)
	t.Setenv("FAKE_DOCKER_RUN_EXIT", "3")
	err := configInspect("validate", parseArgs([]string{"--dir", repo}))
	if err == nil || !strings.Contains(err.Error(), "unknownField") {
		t.Fatalf("err = %v, want 字段级错误", err)
	}
}

func TestResolveRepoRootFromComponentDirs(t *testing.T) {
	repo, cp, storager, _ := newFakeRepo(t)
	for _, dir := range []string{repo, cp, storager, filepath.Join(repo, "storager", "nvmeof")} {
		got, err := resolveRepoRoot(dir)
		if err != nil || got != repo {
			t.Errorf("resolveRepoRoot(%s) = %q, %v; want %q", dir, got, err, repo)
		}
	}
}

func TestConfigMissingFile(t *testing.T) {
	repo, _, _, _ := newFakeRepo(t)
	err := configInspect("validate", parseArgs([]string{"--dir", repo}))
	if err == nil || !strings.Contains(err.Error(), "print init-defaults") {
		t.Fatalf("err = %v, want 模板指引", err)
	}
}

func TestConfigUnknownKind(t *testing.T) {
	repo, cp, _, _ := newFakeRepo(t)
	writeControlPlaneConfig(t, cp, strings.Replace(cpYAML, "kind: ControlPlaneConfiguration", "kind: SomethingElse", 1))
	err := configInspect("validate", parseArgs([]string{"--dir", repo}))
	if err == nil || !strings.Contains(err.Error(), "SomethingElse") {
		t.Fatalf("err = %v, want kind 错误", err)
	}
}
