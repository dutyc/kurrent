// deployment.go：部署记录、稳定状态根与 compose 项目身份。
//
// 职责分层：仓库只承载声明、代码与可构建资产；运行时状态放在仓库之外的状态根
// （默认 $XDG_STATE_HOME/kurrent → ~/.local/state/kurrent），因此移动/改名仓库
// 不会改变部署身份。部署记录（~/.kurrent/deployment.yaml）保存本机的 compose
// 项目名、仓库根、状态根与角色，是 CLI 解析部署上下文的唯一入口。
package deploy

import (
	"fmt"
	"kurrent/cli/internal/fsx"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

const (
	DeploymentAPIVersion = "kurrent.io/v1"
	DeploymentKind       = "DeploymentRecord"

	DefaultComposeProject = "kurrent"
	DefaultStorageProject = "kurrent-storage"

	StatusBootstrapping = "bootstrapping"
	StatusReady         = "ready"
	StatusDegraded      = "degraded"
)

type DeploymentRoles struct {
	ControlPlane bool `yaml:"controlPlane"`
	Storage      bool `yaml:"storage"`
}

type DeploymentRecord struct {
	APIVersion     string          `yaml:"apiVersion"`
	Kind           string          `yaml:"kind"`
	Project        string          `yaml:"project"`
	StorageProject string          `yaml:"storageProject"`
	RepoRoot       string          `yaml:"repoRoot"`
	StateDir       string          `yaml:"stateDir"`
	Roles          DeploymentRoles `yaml:"roles"`
	Status         string          `yaml:"status"`
	UpdatedAt      string          `yaml:"updatedAt"`
}

// DeploymentRecordPath 部署记录路径：KURRENT_DEPLOYMENT_FILE 覆盖（测试/多部署预留），
// 默认 ~/.kurrent/deployment.yaml；主目录不可知时返回 ""。
func DeploymentRecordPath() string {
	if p := strings.TrimSpace(os.Getenv("KURRENT_DEPLOYMENT_FILE")); p != "" {
		if abs, err := filepath.Abs(p); err == nil {
			return abs
		}
		return p
	}
	if dir := fsx.UserCredentialsDir(); dir != "" {
		return filepath.Join(dir, "deployment.yaml")
	}
	return ""
}

// ReadDeploymentRecord 读取部署记录；文件不存在返回 (nil, nil)。
// 记录是部署身份的权威：内容非法直接报错，不静默回退到仓库路径。
func ReadDeploymentRecord() (*DeploymentRecord, error) {
	path := DeploymentRecordPath()
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
	var rec DeploymentRecord
	if err := yaml.Unmarshal(data, &rec); err != nil {
		return nil, fmt.Errorf("parse deployment record %s: %w (remove it to start over)", path, err)
	}
	if rec.APIVersion != DeploymentAPIVersion || rec.Kind != DeploymentKind {
		return nil, fmt.Errorf("deployment record %s: apiVersion/kind must be %s/%s (remove it to start over)",
			path, DeploymentAPIVersion, DeploymentKind)
	}
	if !filepath.IsAbs(rec.RepoRoot) || !filepath.IsAbs(rec.StateDir) {
		return nil, fmt.Errorf("deployment record %s: repoRoot and stateDir must be absolute paths", path)
	}
	if rec.Project == "" {
		rec.Project = DefaultComposeProject
	}
	if rec.StorageProject == "" {
		rec.StorageProject = DefaultStorageProject
	}
	return &rec, nil
}

// WriteDeploymentRecord 原子写入部署记录（0600；~/.kurrent 目录 0700）。
func WriteDeploymentRecord(rec *DeploymentRecord) error {
	path := DeploymentRecordPath()
	if path == "" {
		return fmt.Errorf("cannot determine the user home directory (deployment record ~/.kurrent/deployment.yaml)")
	}
	rec.APIVersion = DeploymentAPIVersion
	rec.Kind = DeploymentKind
	if rec.Project == "" {
		rec.Project = DefaultComposeProject
	}
	if rec.StorageProject == "" {
		rec.StorageProject = DefaultStorageProject
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
	if err := fsx.WriteFileAtomic(path, append([]byte(header), body...), 0o600); err != nil {
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
	if home := fsx.UserHomeDir(); home != "" {
		return filepath.Join(home, ".local", "state", "kurrent")
	}
	return ""
}

// ControlPlanePaths 控制面状态/引导交换/派生配置的宿主路径。
type ControlPlanePaths struct {
	Root       string // <state>/control-plane
	State      string // 容器 /app/state
	Config     string // 容器 /app/config
	AgentsFile string // 台账投影
	Dnsmasq    string // 容器 /app/dnsmasq
	Bootstrap  string // 容器 /app/bootstrap（一次性引导交换）
	AdminConf  string // 控制面侧管理凭据（交换源）
	ServerCert string // nginx TOFU 服务器证书
}

func NewControlPlanePaths(stateRoot string) ControlPlanePaths {
	root := filepath.Join(stateRoot, "control-plane")
	state := filepath.Join(root, "state")
	return ControlPlanePaths{
		Root:       root,
		State:      state,
		Config:     filepath.Join(root, "config"),
		AgentsFile: filepath.Join(root, "config", "agents.yml"),
		Dnsmasq:    filepath.Join(root, "dnsmasq"),
		Bootstrap:  filepath.Join(root, "bootstrap"),
		AdminConf:  filepath.Join(state, "admin.conf"),
		ServerCert: filepath.Join(state, "certs", "server.crt"),
	}
}

// StoragerPaths 存储节点状态、引导凭据与组件 PKI 的宿主路径。
type StoragerPaths struct {
	Root      string // <state>/storager
	State     string // 节点身份（agent.id）
	Bootstrap string // 一次性引导凭据（agent.token / nvmet-host.token）
	PKI       string
	AgentPKI  string // pki/agent-<id>
	NvmetPKI  string // pki/nvmet-<id>
	Logs      string
	Certs     string
	CPCA      string // certs/cp-ca.crt
}

func NewStoragerPaths(stateRoot, agentID string) StoragerPaths {
	root := filepath.Join(stateRoot, "storager")
	pki := filepath.Join(root, "pki")
	certs := filepath.Join(root, "certs")
	paths := StoragerPaths{
		Root:      root,
		State:     filepath.Join(root, "state"),
		Bootstrap: filepath.Join(root, "bootstrap"),
		PKI:       pki,
		Logs:      filepath.Join(root, "logs"),
		Certs:     certs,
		CPCA:      filepath.Join(certs, "cp-ca.crt"),
	}
	if agentID != "" {
		paths.AgentPKI = filepath.Join(pki, "agent-"+agentID)
		paths.NvmetPKI = filepath.Join(pki, "nvmet-"+agentID)
	}
	return paths
}

// DeployQuery 一次命令的部署上下文：仓库根（代码/声明）、状态根与记录（可为空）。
type DeployQuery struct {
	RepoRoot string
	StateDir string
	Record   *DeploymentRecord
}

func (q DeployQuery) ControlPlane() ControlPlanePaths {
	return NewControlPlanePaths(q.StateDir)
}

func (q DeployQuery) Storager(agentID string) StoragerPaths {
	return NewStoragerPaths(q.StateDir, agentID)
}

func (q DeployQuery) Project() string {
	if q.Record != nil && q.Record.Project != "" {
		return q.Record.Project
	}
	return DefaultComposeProject
}

func (q DeployQuery) StorageProject() string {
	if q.Record != nil && q.Record.StorageProject != "" {
		return q.Record.StorageProject
	}
	return DefaultStorageProject
}

// ResolveDeployment 解析仓库根与状态根：
//   - 仓库根：--dir/显式路径 ＞ 部署记录 ＞ 从 cwd 上溯；
//   - 状态根：--state-dir ＞ KURRENT_STATE_DIR ＞ 部署记录 ＞ 默认值。
//
// 记录存在时其路径是部署身份：状态根缺失不自动重建（由调用方报错），
// 仓库根改名/移动由 init 显式更新记录。
func ResolveDeployment(explicitRepo, explicitState string) (DeployQuery, error) {
	rec, err := ReadDeploymentRecord()
	if err != nil {
		return DeployQuery{}, err
	}
	repo := strings.TrimSpace(explicitRepo)
	if repo == "" && rec != nil {
		repo = rec.RepoRoot
	}
	if repo == "" {
		repo, err = ResolveRepoRoot("")
	} else {
		repo, err = ResolveRepoRoot(repo)
	}
	if err != nil {
		return DeployQuery{}, err
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
		return DeployQuery{}, fmt.Errorf("cannot determine the state directory; pass --state-dir or set KURRENT_STATE_DIR")
	}
	abs, err := filepath.Abs(state)
	if err != nil {
		return DeployQuery{}, fmt.Errorf("resolve state directory: %w", err)
	}
	return DeployQuery{RepoRoot: repo, StateDir: filepath.Clean(abs), Record: rec}, nil
}

// NormalizeComposeProject 复刻 docker compose 的项目名归一化：小写，
// 仅保留 [a-z0-9_-]，去掉开头非字母数字字符。用于识别旧部署的项目名
// （旧版未固定 name:，项目名默认取仓库目录 basename）。
func NormalizeComposeProject(name string) string {
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
func (q DeployQuery) ComposeEnv() []string {
	httpsPort := strings.TrimSpace(os.Getenv("KURRENT_HTTPS_PORT"))
	if httpsPort == "" {
		httpsPort = "443"
	}
	return []string{
		"KURRENT_STATE_DIR=" + q.StateDir,
		"KURRENT_HTTPS_PORT=" + httpsPort,
	}
}

// nodeComposeEnv 渲染存储节点 compose 的插值变量：节点状态、身份、盘目录与 CP 信任锚。
func (q DeployQuery) NodeComposeEnv(agentID, diskDir, cpCA string) []string {
	paths := q.Storager(agentID)
	return []string{
		"KURRENT_NODE_STATE_DIR=" + paths.Root,
		"KURRENT_AGENT_ID=" + agentID,
		"KURRENT_DISK_DIR=" + diskDir,
		"KURRENT_CP_CA_FILE=" + cpCA,
	}
}

// ResolveRepoRoot 定位仓库根：--dir 显式；否则部署记录（部署身份优先于 cwd）；
// 再否则从 cwd 逐级上溯（含 control_plane/ 与 storager/）。--dir 也接受 control_plane/
// 或 storager/ 本身。
func ResolveRepoRoot(explicit string) (string, error) {
	start := explicit
	if start == "" {
		if rec, err := ReadDeploymentRecord(); err != nil {
			return "", err
		} else if rec != nil {
			if fsx.IsDir(filepath.Join(rec.RepoRoot, "control_plane")) && fsx.IsDir(filepath.Join(rec.RepoRoot, "storager")) {
				return rec.RepoRoot, nil
			}
			return "", fmt.Errorf("the deployment record points at %s, but that checkout no longer exists; pass --dir with its new location (or run kurrent init --dir <new-checkout>)", rec.RepoRoot)
		}
		cwd, err := os.Getwd()
		if err != nil {
			return "", fmt.Errorf("get current directory: %w", err)
		}
		start = cwd
	}
	abs, err := filepath.Abs(start)
	if err != nil {
		return "", fmt.Errorf("resolve --dir: %w", err)
	}
	if fi, err := os.Stat(abs); err != nil || !fi.IsDir() {
		return "", fmt.Errorf("--dir is not a directory: %s", abs)
	}
	base := filepath.Base(abs)
	if base == "control_plane" || base == "storager" {
		abs = filepath.Dir(abs)
	}
	for dir := abs; ; dir = filepath.Dir(dir) {
		if fsx.IsDir(filepath.Join(dir, "control_plane")) && fsx.IsDir(filepath.Join(dir, "storager")) {
			return dir, nil
		}
		if parent := filepath.Dir(dir); parent == dir {
			break
		}
	}
	return "", fmt.Errorf("repository root not found (control_plane/ and storager/ must both exist; run inside the repository or pass --dir)")
}
