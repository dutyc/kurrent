// deployment.go：部署记录、稳定状态根与 compose 项目身份。
//
// 职责分层：仓库只承载声明、代码与可构建资产；运行时状态放在仓库之外的状态根
// （默认 $XDG_STATE_HOME/kurrent → ~/.local/state/kurrent），因此移动/改名仓库
// 不会改变部署身份。部署记录（~/.kurrent/deployment.yaml）保存本机的 compose
// 项目名、仓库根、状态根与角色，是 CLI 解析部署上下文的唯一入口。
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

const (
	deploymentAPIVersion = "kurrent.io/v1"
	deploymentKind       = "DeploymentRecord"

	defaultComposeProject = "kurrent"
	defaultStorageProject = "kurrent-storage"

	deployStatusBootstrapping = "bootstrapping"
	deployStatusReady         = "ready"
	deployStatusDegraded      = "degraded"
)

type deploymentRoles struct {
	ControlPlane bool `yaml:"controlPlane"`
	Storage      bool `yaml:"storage"`
}

type deploymentRecord struct {
	APIVersion     string          `yaml:"apiVersion"`
	Kind           string          `yaml:"kind"`
	Project        string          `yaml:"project"`
	StorageProject string          `yaml:"storageProject"`
	RepoRoot       string          `yaml:"repoRoot"`
	StateDir       string          `yaml:"stateDir"`
	Roles          deploymentRoles `yaml:"roles"`
	Status         string          `yaml:"status"`
	UpdatedAt      string          `yaml:"updatedAt"`
}

// deploymentRecordPath 部署记录路径：KURRENT_DEPLOYMENT_FILE 覆盖（测试/多部署预留），
// 默认 ~/.kurrent/deployment.yaml；主目录不可知时返回 ""。
func deploymentRecordPath() string {
	if p := strings.TrimSpace(os.Getenv("KURRENT_DEPLOYMENT_FILE")); p != "" {
		if abs, err := filepath.Abs(p); err == nil {
			return abs
		}
		return p
	}
	if dir := userCredentialsDir(); dir != "" {
		return filepath.Join(dir, "deployment.yaml")
	}
	return ""
}

// readDeploymentRecord 读取部署记录；文件不存在返回 (nil, nil)。
// 记录是部署身份的权威：内容非法直接报错，不静默回退到仓库路径。
func readDeploymentRecord() (*deploymentRecord, error) {
	path := deploymentRecordPath()
	if path == "" {
		return nil, nil
	}
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read deployment record %s: %w", path, err)
	}
	var rec deploymentRecord
	if err := yaml.Unmarshal(data, &rec); err != nil {
		return nil, fmt.Errorf("parse deployment record %s: %w (remove it to start over)", path, err)
	}
	if rec.APIVersion != deploymentAPIVersion || rec.Kind != deploymentKind {
		return nil, fmt.Errorf("deployment record %s: apiVersion/kind must be %s/%s (remove it to start over)",
			path, deploymentAPIVersion, deploymentKind)
	}
	if !filepath.IsAbs(rec.RepoRoot) || !filepath.IsAbs(rec.StateDir) {
		return nil, fmt.Errorf("deployment record %s: repoRoot and stateDir must be absolute paths", path)
	}
	if rec.Project == "" {
		rec.Project = defaultComposeProject
	}
	if rec.StorageProject == "" {
		rec.StorageProject = defaultStorageProject
	}
	return &rec, nil
}

// writeDeploymentRecord 原子写入部署记录（0600；~/.kurrent 目录 0700）。
func writeDeploymentRecord(rec *deploymentRecord) error {
	path := deploymentRecordPath()
	if path == "" {
		return fmt.Errorf("cannot determine the user home directory (deployment record ~/.kurrent/deployment.yaml)")
	}
	rec.APIVersion = deploymentAPIVersion
	rec.Kind = deploymentKind
	if rec.Project == "" {
		rec.Project = defaultComposeProject
	}
	if rec.StorageProject == "" {
		rec.StorageProject = defaultStorageProject
	}
	rec.UpdatedAt = time.Now().UTC().Format(time.RFC3339)
	body, err := yaml.Marshal(rec)
	if err != nil {
		return fmt.Errorf("serialize deployment record: %w", err)
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("create %s: %w", dir, err)
	}
	_ = os.Chmod(dir, 0o700)
	header := "# kurrent deployment record (written by kurrent init/join; identifies the deployment independently of the checkout path)\n"
	if err := writeFileAtomic(path, append([]byte(header), body...), 0o600); err != nil {
		return fmt.Errorf("write deployment record %s: %w", path, err)
	}
	return nil
}

// defaultStateRoot 状态根缺省值：KURRENT_STATE_DIR ＞ $XDG_STATE_HOME/kurrent ＞ ~/.local/state/kurrent。
func defaultStateRoot() string {
	for _, candidate := range []string{
		strings.TrimSpace(os.Getenv("KURRENT_STATE_DIR")),
		filepath.Join(strings.TrimSpace(os.Getenv("XDG_STATE_HOME")), "kurrent"),
	} {
		if candidate == "" || candidate == "kurrent" {
			continue
		}
		if abs, err := filepath.Abs(candidate); err == nil {
			return abs
		}
		return candidate
	}
	if home := userHomeDir(); home != "" {
		return filepath.Join(home, ".local", "state", "kurrent")
	}
	return ""
}

// controlPlanePaths 控制面状态/引导交换/派生配置的宿主路径。
type controlPlanePaths struct {
	root       string // <state>/control-plane
	state      string // 容器 /app/state
	config     string // 容器 /app/config
	agentsFile string // 台账投影
	dnsmasq    string // 容器 /app/dnsmasq
	bootstrap  string // 容器 /app/bootstrap（一次性引导交换）
	adminConf  string // 控制面侧管理凭据（交换源）
	serverCert string // nginx TOFU 服务器证书
}

func newControlPlanePaths(stateRoot string) controlPlanePaths {
	root := filepath.Join(stateRoot, "control-plane")
	state := filepath.Join(root, "state")
	return controlPlanePaths{
		root:       root,
		state:      state,
		config:     filepath.Join(root, "config"),
		agentsFile: filepath.Join(root, "config", "agents.yml"),
		dnsmasq:    filepath.Join(root, "dnsmasq"),
		bootstrap:  filepath.Join(root, "bootstrap"),
		adminConf:  filepath.Join(state, "admin.conf"),
		serverCert: filepath.Join(state, "certs", "server.crt"),
	}
}

// storagerPaths 存储节点状态、引导凭据与组件 PKI 的宿主路径。
type storagerPaths struct {
	root      string // <state>/storager
	state     string // 节点身份（agent.id）
	bootstrap string // 一次性引导凭据（agent.token / nvmet-host.token）
	pki       string
	agentPKI  string // pki/agent-<id>
	nvmetPKI  string // pki/nvmet-<id>
	logs      string
	certs     string
	cpCA      string // certs/cp-ca.crt
}

func newStoragerPaths(stateRoot, agentID string) storagerPaths {
	root := filepath.Join(stateRoot, "storager")
	pki := filepath.Join(root, "pki")
	certs := filepath.Join(root, "certs")
	paths := storagerPaths{
		root:      root,
		state:     filepath.Join(root, "state"),
		bootstrap: filepath.Join(root, "bootstrap"),
		pki:       pki,
		logs:      filepath.Join(root, "logs"),
		certs:     certs,
		cpCA:      filepath.Join(certs, "cp-ca.crt"),
	}
	if agentID != "" {
		paths.agentPKI = filepath.Join(pki, "agent-"+agentID)
		paths.nvmetPKI = filepath.Join(pki, "nvmet-"+agentID)
	}
	return paths
}

// deployQuery 一次命令的部署上下文：仓库根（代码/声明）、状态根与记录（可为空）。
type deployQuery struct {
	repoRoot string
	stateDir string
	record   *deploymentRecord
}

func (q deployQuery) controlPlane() controlPlanePaths {
	return newControlPlanePaths(q.stateDir)
}

func (q deployQuery) storager(agentID string) storagerPaths {
	return newStoragerPaths(q.stateDir, agentID)
}

func (q deployQuery) project() string {
	if q.record != nil && q.record.Project != "" {
		return q.record.Project
	}
	return defaultComposeProject
}

func (q deployQuery) storageProject() string {
	if q.record != nil && q.record.StorageProject != "" {
		return q.record.StorageProject
	}
	return defaultStorageProject
}

// resolveDeployment 解析仓库根与状态根：
//   - 仓库根：--dir/显式路径 ＞ 部署记录 ＞ 从 cwd 上溯；
//   - 状态根：--state-dir ＞ KURRENT_STATE_DIR ＞ 部署记录 ＞ 默认值。
//
// 记录存在时其路径是部署身份：状态根缺失不自动重建（由调用方报错），
// 仓库根改名/移动由 init 显式更新记录。
func resolveDeployment(explicitRepo, explicitState string) (deployQuery, error) {
	rec, err := readDeploymentRecord()
	if err != nil {
		return deployQuery{}, err
	}
	repo := strings.TrimSpace(explicitRepo)
	if repo == "" && rec != nil {
		repo = rec.RepoRoot
	}
	if repo == "" {
		repo, err = resolveRepoRoot("")
	} else {
		repo, err = resolveRepoRoot(repo)
	}
	if err != nil {
		return deployQuery{}, err
	}
	state := strings.TrimSpace(explicitState)
	if state == "" {
		state = strings.TrimSpace(os.Getenv("KURRENT_STATE_DIR"))
	}
	if state == "" && rec != nil {
		state = rec.StateDir
	}
	if state == "" {
		state = defaultStateRoot()
	}
	if state == "" {
		return deployQuery{}, fmt.Errorf("cannot determine the state directory; pass --state-dir or set KURRENT_STATE_DIR")
	}
	abs, err := filepath.Abs(state)
	if err != nil {
		return deployQuery{}, fmt.Errorf("resolve state directory: %w", err)
	}
	return deployQuery{repoRoot: repo, stateDir: filepath.Clean(abs), record: rec}, nil
}

// normalizeComposeProject 复刻 docker compose 的项目名归一化：小写，
// 仅保留 [a-z0-9_-]，去掉开头非字母数字字符。用于识别旧部署的项目名
// （旧版未固定 name:，项目名默认取仓库目录 basename）。
func normalizeComposeProject(name string) string {
	name = strings.ToLower(strings.TrimSpace(name))
	var b strings.Builder
	for _, r := range name {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '_', r == '-':
			b.WriteRune(r)
		}
	}
	return strings.TrimLeft(b.String(), "_-")
}

// composeEnv 渲染 compose 插值变量：状态根是唯一权威，容器路径全部由它派生。
func (q deployQuery) composeEnv() []string {
	httpsPort := strings.TrimSpace(os.Getenv("KURRENT_HTTPS_PORT"))
	if httpsPort == "" {
		httpsPort = "443"
	}
	return []string{
		"KURRENT_STATE_DIR=" + q.stateDir,
		"KURRENT_HTTPS_PORT=" + httpsPort,
	}
}

// nodeComposeEnv 渲染存储节点 compose 的插值变量：节点状态、身份、盘目录与 CP 信任锚。
func (q deployQuery) nodeComposeEnv(agentID, diskDir, cpCA string) []string {
	paths := q.storager(agentID)
	return []string{
		"KURRENT_NODE_STATE_DIR=" + paths.root,
		"KURRENT_AGENT_ID=" + agentID,
		"KURRENT_DISK_DIR=" + diskDir,
		"KURRENT_CP_CA_FILE=" + cpCA,
	}
}
