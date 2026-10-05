// kurrent join 沙盒测试：临时仓库 + fake docker，验证声明校验/补默认写回、
// .env 收敛为插值键、引导凭据落盘与 compose 收敛调用。
package main

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

// nodeIDRe 自动生成的节点身份（agent_id）：12 位 hex 随机标识（与盘 os_tag 同构）。
var nodeIDRe = regexp.MustCompile(`^[0-9a-f]{12}$`)

const fakeDocker = `#!/usr/bin/env bash
echo "FAKE-DOCKER $(pwd) $*" >> "${FAKE_DOCKER_LOG}"
`

// stubHostReady 注入「宿主前置已就绪」的假环境（假 /proc/mounts + /sys/module）：
// join 的 nvmet 预检在无内核 nvmet 的沙盒里照常通过。真实路径见 hostEnvPaths 缺省值。
func stubHostReady(t *testing.T) {
	t.Helper()
	root := t.TempDir()
	mounts := filepath.Join(root, "mounts")
	content := "sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0\n" +
		"configfs /sys/kernel/config configfs rw,nosuid,nodev,noexec,relatime 0 0\n"
	if err := os.WriteFile(mounts, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	modRoot := filepath.Join(root, "module")
	for _, m := range []string{"nvmet", "nvmet_tcp"} {
		if err := os.MkdirAll(filepath.Join(modRoot, m), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	old := hostEnvPaths
	hostEnvPaths = hostEnv{procMounts: mounts, sysModuleRoot: modRoot}
	t.Cleanup(func() { hostEnvPaths = old })
}

// breakHost 覆盖为「宿主前置未就绪」的假环境（空 /proc/mounts、空 /sys/module）。
func breakHost(t *testing.T) {
	t.Helper()
	root := t.TempDir()
	mounts := filepath.Join(root, "mounts")
	if err := os.WriteFile(mounts, []byte("sysfs /sys sysfs rw 0 0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	old := hostEnvPaths
	hostEnvPaths = hostEnv{procMounts: mounts, sysModuleRoot: filepath.Join(root, "module")}
	t.Cleanup(func() { hostEnvPaths = old })
}

// newFakeRepo 建临时仓库骨架（control_plane/{app,config,state} +
// storager/{nvmeof,iscsi} + fake docker，PATH 注入；宿主前置假就绪）。
func newFakeRepo(t *testing.T) (repo, controlPlane, storager, logFile string) {
	t.Helper()
	stubHostReady(t)
	repo = t.TempDir()
	controlPlane = filepath.Join(repo, "control_plane")
	storager = filepath.Join(repo, "storager")
	for _, d := range []string{
		"control_plane/app", "control_plane/config", "control_plane/state",
		"storager/nvmeof", "storager/iscsi",
	} {
		if err := os.MkdirAll(filepath.Join(repo, d), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	bin := filepath.Join(repo, "bin")
	if err := os.MkdirAll(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	logFile = filepath.Join(repo, "docker.log")
	if err := os.WriteFile(filepath.Join(bin, "docker"), []byte(fakeDocker), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("FAKE_DOCKER_LOG", logFile)
	return repo, controlPlane, storager, logFile
}

// envValues 返回 .env 中某键的全部值（末位为最新）。
func envValues(t *testing.T, envFile, key string) []string {
	t.Helper()
	data, err := os.ReadFile(envFile)
	if err != nil {
		t.Fatal(err)
	}
	prefix := key + "="
	var vals []string
	for _, l := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(l, prefix) {
			vals = append(vals, strings.TrimPrefix(l, prefix))
		}
	}
	return vals
}

// yamlValue 读取 kurrent.yaml 指定路径的标量值（缺失返回空串）。
func yamlValue(t *testing.T, configFile string, path ...string) string {
	t.Helper()
	data, err := os.ReadFile(configFile)
	if err != nil {
		t.Fatal(err)
	}
	var root yaml.Node
	if err := yaml.Unmarshal(data, &root); err != nil {
		t.Fatal(err)
	}
	cur := rootMapping(&root)
	for i, key := range path {
		if cur.Kind != yaml.MappingNode {
			t.Fatalf("路径 %v 处非 mapping", path[:i+1])
		}
		v := mapValue(cur, key)
		if v == nil {
			return ""
		}
		if i == len(path)-1 {
			return v.Value
		}
		cur = v
	}
	return ""
}

// nodeYAML 完整节点声明（模板编辑后的形态）。
const nodeYAML = `apiVersion: kurrent.io/v1
kind: NodeConfiguration
metadata:
  name: storage-01
  remark: 生产存储
spec:
  agent:
    backend: nvmet
    advertiseUrl: https://192.168.1.10:4840
    diskDir: /mnt/disks
    nqnBase: nqn.2026-07.com.kurrent
  controlPlane:
    url: https://192.168.1.10
`

func TestJoinBasic(t *testing.T) {
	_, _, storager, logFile := newFakeRepo(t)
	// 预写旧版派生凭据（模拟历史部署：join 曾代写 nvmet-host.token）→ nvmet 后端重跑应清除
	bootstrapDir := filepath.Join(storager, "bootstrap")
	if err := os.MkdirAll(bootstrapDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(bootstrapDir, "nvmet-host.token"),
		[]byte("legacy.token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(storager, "kurrent.yaml"), []byte(nodeYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := joinRun(joinOpts{
		dir:        storager,
		configFile: filepath.Join(storager, "kurrent.yaml"),
		cpURL:      "https://192.168.1.10",
		token:      "abc123.def456",
	}); err != nil {
		t.Fatal(err)
	}
	configFile := filepath.Join(storager, "kurrent.yaml")
	// 声明值原样保留（非 forbid 式合并：手工编辑权威；remark = 人工输入的唯一字段）
	for path, want := range map[string]string{
		"metadata/name":           "storage-01",
		"metadata/remark":         "生产存储",
		"spec/agent/backend":      "nvmet",
		"spec/agent/advertiseUrl": "https://192.168.1.10:4840",
		"spec/agent/diskDir":      "/mnt/disks",
		"spec/agent/nqnBase":      "nqn.2026-07.com.kurrent",
		"spec/controlPlane/url":   "https://192.168.1.10",
	} {
		parts := strings.Split(path, "/")
		if got := yamlValue(t, configFile, parts...); got != want {
			t.Errorf("kurrent.yaml %s = %q, want %q", path, got, want)
		}
	}
	// 容器内路径/监听/内部通讯/一次性 token 不在 yml 声明（分层职责）
	for _, p := range [][]string{
		{"spec", "agent", "logFile"}, {"spec", "agent", "nvmetCacheFile"},
		{"spec", "agent", "iscsiContainer"}, {"spec", "agent", "nvmetHostUrl"},
		{"spec", "nvmetHost"}, {"spec", "controlPlane", "caFile"},
		{"spec", "pki"}, {"spec", "bootstrap"},
	} {
		if got := yamlValue(t, configFile, p...); got != "" {
			t.Errorf("kurrent.yaml %v = %q, want 空（部署细节不在 yml 声明）", p, got)
		}
	}
	// 引导凭据在独立文件：join 只写通用 token 到 agent.token；
	// nvmet-host.token 是派生凭据（agent enroll 按能力签发），join 不写也不产生
	tokenFile := filepath.Join(storager, "bootstrap", "agent.token")
	data, err := os.ReadFile(tokenFile)
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.TrimSpace(string(data)); got != "abc123.def456" {
		t.Errorf("bootstrap/agent.token = %q, want abc123.def456", got)
	}
	if _, err := os.Stat(filepath.Join(storager, "bootstrap", "nvmet-host.token")); !os.IsNotExist(err) {
		t.Errorf("bootstrap/nvmet-host.token 不应由 join 生成（enroll 派生），stat err = %v", err)
	}
	// nvmet 后端 → .env 含两组件 PKI 插值键（nvmet-host 组件目录仍按 agent_id 分配）
	envFile := filepath.Join(storager, ".env")
	got := envValues(t, envFile, "KURRENT_AGENT_PKI_HOST")[0]
	if !strings.HasSuffix(got, "control_plane/state/pki/components/agent-storage-01") {
		t.Errorf("KURRENT_AGENT_PKI_HOST = %s, want suffix components/agent-storage-01", got)
	}
	got = envValues(t, envFile, "KURRENT_NVMET_PKI_HOST")[0]
	if !strings.HasSuffix(got, "control_plane/state/pki/components/nvmet-storage-01") {
		t.Errorf("KURRENT_NVMET_PKI_HOST = %s, want suffix components/nvmet-storage-01", got)
	}
	got = envValues(t, envFile, "KURRENT_DISK_DIR")[0]
	if want := "/mnt/disks"; got != want {
		t.Errorf("KURRENT_DISK_DIR = %s, want %s（yml diskDir 同步）", got, want)
	}
	// 业务键已迁移到 kurrent.yaml：.env 不再出现
	for _, key := range []string{
		"KURRENT_AGENT_ID", "KURRENT_BOOTSTRAP_TOKEN", "KURRENT_BOOTSTRAP_TOKEN_NVMET",
		"KURRENT_CP_ENROLL_URL", "KURRENT_ADVERTISE_URL", "KURRENT_BACKEND",
	} {
		if vals := envValues(t, envFile, key); len(vals) != 0 {
			t.Errorf("%s 应已迁移到 kurrent.yaml, .env 残留 %v", key, vals)
		}
	}
	// nvmet 后端 → storager/nvmeof 下执行 compose（up + restart storager-agent 收敛）
	logData, err := os.ReadFile(logFile)
	if err != nil {
		t.Fatal(err)
	}
	log := string(logData)
	if !strings.Contains(log, "FAKE-DOCKER "+storager+"/nvmeof compose --env-file ../.env up -d") {
		t.Errorf("docker 调用缺 up -d: %s", log)
	}
	if !strings.Contains(log, "FAKE-DOCKER "+storager+"/nvmeof compose --env-file ../.env restart storager-agent") {
		t.Errorf("docker 调用缺 restart storager-agent: %s", log)
	}
}

// minimalNodeYAML 最小声明：仅控制面端点；其余由 join 补默认注入。
const minimalNodeYAML = `apiVersion: kurrent.io/v1
kind: NodeConfiguration
spec:
  controlPlane:
    url: https://192.168.1.10
`

func TestJoinFillsDefaults(t *testing.T) {
	repo, _, storager, _ := newFakeRepo(t)
	if err := os.WriteFile(filepath.Join(storager, "kurrent.yaml"), []byte(minimalNodeYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := joinRun(joinOpts{dir: storager, configFile: filepath.Join(storager, "kurrent.yaml"), cpURL: "https://192.168.1.10", token: "t"}); err != nil {
		t.Fatal(err)
	}
	configFile := filepath.Join(storager, "kurrent.yaml")
	// 声明留空 → 自动生成随机机器身份（12 位 hex，与盘 os_tag 同构）
	name := yamlValue(t, configFile, "metadata", "name")
	if !nodeIDRe.MatchString(name) {
		t.Errorf("metadata/name = %q, want 12 位 hex 随机标识", name)
	}
	for path, want := range map[string]string{
		"spec/agent/backend":      "nvmet",
		"spec/agent/advertiseUrl": "https://192.168.1.10:4840", // 缺省推导 https://<cp-host>:4840
		"spec/agent/diskDir":      filepath.Join(repo, "storager_img"),
		"spec/agent/nqnBase":      "nqn.2026-07.com.kurrent",
		"spec/controlPlane/url":   "https://192.168.1.10",
	} {
		parts := strings.Split(path, "/")
		if got := yamlValue(t, configFile, parts...); got != want {
			t.Errorf("kurrent.yaml %s = %q, want %q", path, got, want)
		}
	}
	// .env PKI 键按生成的 agent_id 同步
	envFile := filepath.Join(storager, ".env")
	got := envValues(t, envFile, "KURRENT_AGENT_PKI_HOST")[0]
	if !strings.HasSuffix(got, "components/agent-"+name) {
		t.Errorf("KURRENT_AGENT_PKI_HOST = %s, want suffix agent-%s", got, name)
	}
}

func TestJoinGeneratesConfig(t *testing.T) {
	// 声明缺失 → 按模板自动生成（无预置文件时用缺省模板；地址来自命令）
	repo, _, storager, _ := newFakeRepo(t)
	if err := joinRun(joinOpts{dir: storager, configFile: filepath.Join(storager, "kurrent.yaml"), cpURL: "https://192.168.1.10", token: "t"}); err != nil {
		t.Fatal(err)
	}
	configFile := filepath.Join(storager, "kurrent.yaml")
	name := yamlValue(t, configFile, "metadata", "name")
	if !nodeIDRe.MatchString(name) {
		t.Errorf("metadata/name = %q, want 12 位 hex 随机标识", name)
	}
	for path, want := range map[string]string{
		"metadata/remark":         "",                     // 人工输入的唯一字段：生成时留空（备注）
		"spec/controlPlane/url":   "https://192.168.1.10", // 命令地址写入声明
		"spec/agent/backend":      "nvmet",
		"spec/agent/advertiseUrl": "https://192.168.1.10:4840",
		"spec/agent/diskDir":      filepath.Join(repo, "storager_img"),
		"spec/agent/nqnBase":      "nqn.2026-07.com.kurrent",
	} {
		parts := strings.Split(path, "/")
		if got := yamlValue(t, configFile, parts...); got != want {
			t.Errorf("kurrent.yaml %s = %q, want %q", path, got, want)
		}
	}
}

func TestJoinGeneratesStableID(t *testing.T) {
	// 声明缺失 → 生成随机身份；重跑 join 身份保留（持久化在声明里，幂等稳定）
	_, _, storager, _ := newFakeRepo(t)
	opts := joinOpts{dir: storager, configFile: filepath.Join(storager, "kurrent.yaml"), cpURL: "https://192.168.1.10", token: "t"}
	if err := joinRun(opts); err != nil {
		t.Fatal(err)
	}
	first := yamlValue(t, opts.configFile, "metadata", "name")
	if !nodeIDRe.MatchString(first) {
		t.Fatalf("metadata/name = %q, want 12 位 hex 随机标识", first)
	}
	if err := joinRun(opts); err != nil {
		t.Fatal(err)
	}
	if second := yamlValue(t, opts.configFile, "metadata", "name"); second != first {
		t.Errorf("重跑后 metadata/name = %q, want %q（身份持久化不变）", second, first)
	}
}

func TestJoinURLFromCommand(t *testing.T) {
	// 既有声明无 url（预编辑仅业务键）→ 命令地址补入（引导输入权威）
	_, _, storager, _ := newFakeRepo(t)
	noURL := strings.Replace(nodeYAML, "    url: https://192.168.1.10", "", 1)
	if err := os.WriteFile(filepath.Join(storager, "kurrent.yaml"), []byte(noURL), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := joinRun(joinOpts{dir: storager, configFile: filepath.Join(storager, "kurrent.yaml"), cpURL: "https://192.168.1.10", token: "t"}); err != nil {
		t.Fatal(err)
	}
	configFile := filepath.Join(storager, "kurrent.yaml")
	if got := yamlValue(t, configFile, "spec", "controlPlane", "url"); got != "https://192.168.1.10" {
		t.Errorf("url = %q, want 命令地址写入声明", got)
	}
}

func TestJoinMissingToken(t *testing.T) {
	_, _, storager, _ := newFakeRepo(t)
	if err := os.WriteFile(filepath.Join(storager, "kurrent.yaml"), []byte(nodeYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	// 无 --token 且 bootstrap/agent.token 未就位 → 报错（凭据文件分层：token 不进 yml）
	err := joinRun(joinOpts{dir: storager, configFile: filepath.Join(storager, "kurrent.yaml"), cpURL: "https://192.168.1.10"})
	if err == nil || !strings.Contains(err.Error(), "bootstrap token missing") {
		t.Fatalf("err = %v, want bootstrap token missing", err)
	}
}

func TestJoinTokenFileReuse(t *testing.T) {
	_, _, storager, _ := newFakeRepo(t)
	if err := os.WriteFile(filepath.Join(storager, "kurrent.yaml"), []byte(nodeYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	bootstrapDir := filepath.Join(storager, "bootstrap")
	if err := os.MkdirAll(bootstrapDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(bootstrapDir, "agent.token"), []byte("tok.111\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	// 凭据文件已就位 → 无需 --token（TTL 内复用）
	if err := joinRun(joinOpts{dir: storager, configFile: filepath.Join(storager, "kurrent.yaml"), cpURL: "https://192.168.1.10"}); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(bootstrapDir, "agent.token"))
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.TrimSpace(string(data)); got != "tok.111" {
		t.Errorf("agent.token = %q, want tok.111（既有凭据保留）", got)
	}
}

func TestJoinIdempotentRerun(t *testing.T) {
	_, _, storager, _ := newFakeRepo(t)
	if err := os.WriteFile(filepath.Join(storager, "kurrent.yaml"), []byte(nodeYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	// 重跑（如换 token / 容器漂移恢复）：补默认幂等、不堆积
	if err := joinRun(joinOpts{dir: storager, configFile: filepath.Join(storager, "kurrent.yaml"), cpURL: "https://192.168.1.10", token: "aaa.111"}); err != nil {
		t.Fatal(err)
	}
	if err := joinRun(joinOpts{dir: storager, configFile: filepath.Join(storager, "kurrent.yaml"), cpURL: "https://192.168.1.10", token: "bbb.222"}); err != nil {
		t.Fatal(err)
	}
	configFile := filepath.Join(storager, "kurrent.yaml")
	if got := yamlValue(t, configFile, "metadata", "name"); got != "storage-01" {
		t.Errorf("kurrent.yaml name = %q, want storage-01", got)
	}
	if got := yamlValue(t, configFile, "spec", "agent", "diskDir"); got != "/mnt/disks" {
		t.Errorf("diskDir = %q, want /mnt/disks（手工业务键保留）", got)
	}
	// token 覆盖写（yml 不含 token——token 在独立凭据文件）
	data, err := os.ReadFile(filepath.Join(storager, "bootstrap", "agent.token"))
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.TrimSpace(string(data)); got != "bbb.222" {
		t.Errorf("agent.token = %q, want bbb.222（重跑覆盖）", got)
	}
	envFile := filepath.Join(storager, ".env")
	if vals := envValues(t, envFile, "KURRENT_AGENT_PKI_HOST"); len(vals) != 1 {
		t.Errorf("KURRENT_AGENT_PKI_HOST 出现 %d 次, want 1（幂等不堆积）: %v", len(vals), vals)
	}
}

func TestJoinAppliesExternalConfig(t *testing.T) {
	repo, _, storager, _ := newFakeRepo(t)
	external := filepath.Join(repo, "node.yaml")
	if err := os.WriteFile(external, []byte(minimalNodeYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	// --config 指向其他路径：应用为 storager/kurrent.yaml（容器挂载权威位）；
	// 命令地址覆盖声明 url（引导输入以命令为准）
	if err := joinRun(joinOpts{dir: storager, configFile: external, cpURL: "https://cp2", token: "t"}); err != nil {
		t.Fatal(err)
	}
	configFile := filepath.Join(storager, "kurrent.yaml")
	if got := yamlValue(t, configFile, "spec", "controlPlane", "url"); got != "https://cp2" {
		t.Errorf("url = %q, want https://cp2（外部声明应用 + 命令地址覆盖）", got)
	}
	if got := yamlValue(t, configFile, "spec", "agent", "backend"); got != "nvmet" {
		t.Errorf("backend = %q, want nvmet（补默认）", got)
	}
}

func TestJoinPreservesUnknownFields(t *testing.T) {
	_, _, storager, _ := newFakeRepo(t)
	configFile := filepath.Join(storager, "kurrent.yaml")
	// 预置带未知字段的既有配置（模拟手工编辑）
	if err := os.WriteFile(configFile, []byte(`apiVersion: kurrent.io/v1
kind: NodeConfiguration
metadata:
  name: old-node
spec:
  agent:
    diskDir: /mnt/disks
    customField: keep-me
  controlPlane:
    url: https://cp
`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := joinRun(joinOpts{dir: storager, configFile: configFile, cpURL: "https://cp", token: "t"}); err != nil {
		t.Fatal(err)
	}
	// 未知字段与手工业务键保留（非 forbid 式合并）
	if got := yamlValue(t, configFile, "spec", "agent", "customField"); got != "keep-me" {
		t.Errorf("customField = %q, want keep-me（未知字段保留）", got)
	}
	if got := yamlValue(t, configFile, "spec", "agent", "diskDir"); got != "/mnt/disks" {
		t.Errorf("diskDir = %q, want /mnt/disks（手工业务键保留）", got)
	}
	// 缺省字段补注入
	if got := yamlValue(t, configFile, "spec", "agent", "nqnBase"); got != "nqn.2026-07.com.kurrent" {
		t.Errorf("nqnBase = %q, want 默认注入", got)
	}
	if got := yamlValue(t, configFile, "spec", "agent", "advertiseUrl"); got != "https://cp:4840" {
		t.Errorf("advertiseUrl = %q, want https://cp:4840（默认推导）", got)
	}
}

func TestJoinInvalidBackend(t *testing.T) {
	_, _, storager, _ := newFakeRepo(t)
	bad := strings.Replace(nodeYAML, "backend: nvmet", "backend: hacker", 1)
	if err := os.WriteFile(filepath.Join(storager, "kurrent.yaml"), []byte(bad), 0o644); err != nil {
		t.Fatal(err)
	}
	err := joinRun(joinOpts{dir: storager, configFile: filepath.Join(storager, "kurrent.yaml"), cpURL: "https://192.168.1.10", token: "t"})
	if err == nil || !strings.Contains(err.Error(), "invalid backend") && !strings.Contains(err.Error(), "非法") {
		t.Fatalf("err = %v, want invalid backend", err)
	}
}

func TestJoinInvalidNameChars(t *testing.T) {
	_, _, storager, _ := newFakeRepo(t)
	bad := strings.Replace(nodeYAML, "name: storage-01", "name: \"Bad Node!\"", 1)
	if err := os.WriteFile(filepath.Join(storager, "kurrent.yaml"), []byte(bad), 0o644); err != nil {
		t.Fatal(err)
	}
	err := joinRun(joinOpts{dir: storager, configFile: filepath.Join(storager, "kurrent.yaml"), cpURL: "https://192.168.1.10", token: "t"})
	if err == nil || !strings.Contains(err.Error(), "非法字符") {
		t.Fatalf("err = %v, want 非法字符报错", err)
	}
}

func TestJoinNvmetHostPreflightRejects(t *testing.T) {
	// 宿主前置未就绪（模块未加载 + configfs 未挂载）→ nvmet 后端 fail fast：
	// 报错列出缺失项并指路 host-prepare.sh；compose 不启动（无 docker 调用）
	_, _, storager, logFile := newFakeRepo(t)
	breakHost(t)
	if err := os.WriteFile(filepath.Join(storager, "kurrent.yaml"), []byte(nodeYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	err := joinRun(joinOpts{dir: storager, configFile: filepath.Join(storager, "kurrent.yaml"), cpURL: "https://192.168.1.10", token: "t"})
	if err == nil {
		t.Fatal("宿主前置未就绪应报错")
	}
	for _, want := range []string{"nvmet", "nvmet-tcp", "configfs", "host-prepare.sh"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("err 缺 %q:\n%v", want, err)
		}
	}
	if _, statErr := os.Stat(logFile); statErr == nil {
		t.Error("宿主前置未就绪不应启动 compose（docker 已被调用）")
	}
}

func TestJoinHostPreflightSkippedForStgt(t *testing.T) {
	// stgt/lio 后端无 nvmet 宿主前置要求：前置未就绪也照常收敛启动（iscsi compose）
	_, _, storager, logFile := newFakeRepo(t)
	breakHost(t)
	cfg := strings.Replace(nodeYAML, "backend: nvmet", "backend: stgt", 1)
	if err := os.WriteFile(filepath.Join(storager, "kurrent.yaml"), []byte(cfg), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := joinRun(joinOpts{dir: storager, configFile: filepath.Join(storager, "kurrent.yaml"), cpURL: "https://192.168.1.10", token: "t"}); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(logFile)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), "iscsi compose") {
		t.Errorf("stgt 后端应走 iscsi compose: %s", data)
	}
}

func TestJoinCreatesDiskDir(t *testing.T) {
	// 盘目录兜底创建（compose bind 挂载源）：声明默认 diskDir（仓库 storager_img）时 join 建出目录
	repo, _, storager, _ := newFakeRepo(t)
	if err := os.WriteFile(filepath.Join(storager, "kurrent.yaml"), []byte(minimalNodeYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := joinRun(joinOpts{dir: storager, configFile: filepath.Join(storager, "kurrent.yaml"), cpURL: "https://192.168.1.10", token: "t"}); err != nil {
		t.Fatal(err)
	}
	if fi, err := os.Stat(filepath.Join(repo, "storager_img")); err != nil || !fi.IsDir() {
		t.Fatalf("storager_img 应自动创建: %v", err)
	}
}

func TestNormalizeAgentID(t *testing.T) {
	cases := map[string]string{
		"ROG-Z15":    "rog-z15",
		"node-01":    "node-01",
		"My.Node-7":  "my.node-7",
		"übung host": "-bung-host",
		"  spaced  ": "spaced",
		"A_B_C":      "a-b-c",
	}
	for in, want := range cases {
		if got := normalizeAgentID(in); got != want {
			t.Errorf("normalizeAgentID(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestResolveStoragerDir(t *testing.T) {
	repo, _, storager, _ := newFakeRepo(t)
	// --dir 显式
	if got, err := resolveStoragerDir(storager); err != nil || got != storager {
		t.Fatalf("resolveStoragerDir(%s) = %q, %v", storager, got, err)
	}
	// 仓库根（cwd 下存在 storager/）
	t.Chdir(repo)
	if got, err := resolveStoragerDir(""); err != nil || got != storager {
		t.Fatalf("仓库根解析 = %q, %v, want %q", got, err, storager)
	}
	// 已位于 storager 内（cwd 下存在 nvmeof/）
	t.Chdir(storager)
	if got, err := resolveStoragerDir(""); err != nil || got != storager {
		t.Fatalf("storager 内解析 = %q, %v, want %q", got, err, storager)
	}
	// 找不到
	t.Chdir(t.TempDir())
	if _, err := resolveStoragerDir(""); err == nil {
		t.Fatal("期望找不到 storager 目录报错")
	}
}
