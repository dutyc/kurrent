// state_prep.go：状态根预检、目录准备与旧仓库内状态迁移。
//
// 部署身份与仓库路径解耦后，运行时状态必须落在仓库之外。首次部署前由 init
// 预检（Docker/Compose、WebUI 构建产物、状态根属主与可写性、旧状态检测），
// 再创建目录并种下容器启动前必须存在的文件（否则 Docker 以 root 建目录）。
// --adopt-state 把旧布局（仓库内 control_plane/state、storager/state 等）
// 停栈、复制并校验到新布局，验证成功后删除旧副本。
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
)

// effectiveUID / statPath 变量化供测试注入属主与存在性判定。
var (
	effectiveUID = os.Geteuid
	statPath     = os.Stat
)

const chownRemediation = `sudo chown -R "$(id -u):$(id -g)" %s`

// ensurePrivateDir 创建/收敛私有目录（0700；状态与凭据不含 group/other 权限）。
func ensurePrivateDir(path string) error {
	if err := os.MkdirAll(path, 0o700); err != nil {
		return fmt.Errorf("create %s: %w", path, err)
	}
	if err := os.Chmod(path, 0o700); err != nil {
		return fmt.Errorf("chmod %s: %w", path, err)
	}
	return nil
}

// ensureDir 创建目录并收敛到指定权限（公共派生件用 0755）。
func ensureDir(path string, perm os.FileMode) error {
	if err := os.MkdirAll(path, perm); err != nil {
		return fmt.Errorf("create %s: %w", path, err)
	}
	if err := os.Chmod(path, perm); err != nil {
		return fmt.Errorf("chmod %s: %w", path, err)
	}
	return nil
}

// seedFile 仅在文件缺失时写入并收敛权限：已有运行态文件（台账/租约/凭据）不被覆盖。
func seedFile(path string, data []byte, perm os.FileMode) error {
	if _, err := statPath(path); err == nil {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("create %s: %w", filepath.Dir(path), err)
	}
	if err := writeFileAtomic(path, data, perm); err != nil {
		return fmt.Errorf("seed %s: %w", path, err)
	}
	return nil
}

// assertOwnedAndWritable 校验路径属于当前用户且可写；不存在视为通过（准备阶段会创建）。
// 属主不符时给出可直接执行的 chown 修复命令。
func assertOwnedAndWritable(path string) error {
	fi, err := statPath(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("inspect %s: %w", path, err)
	}
	if st, ok := fi.Sys().(*syscall.Stat_t); ok && int(st.Uid) != effectiveUID() {
		return fmt.Errorf("%s is owned by uid %d, but kurrent runs as uid %d; fix it with:\n  %s",
			path, st.Uid, effectiveUID(), fmt.Sprintf(chownRemediation, path))
	}
	if err := syscall.Access(path, 0x2); err != nil { // W_OK
		return fmt.Errorf("%s is not writable by uid %d; fix its permissions or pass another --state-dir", path, effectiveUID())
	}
	return nil
}

// firstForeignOwner 返回树中第一个不属于当前用户的路径（不存在/全属主正确返回 ""）。
func firstForeignOwner(path string) (string, error) {
	foreign := ""
	err := filepath.WalkDir(path, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		fi, err := d.Info()
		if err != nil {
			return err
		}
		if st, ok := fi.Sys().(*syscall.Stat_t); ok && int(st.Uid) != effectiveUID() {
			foreign = p
			return fs.SkipAll
		}
		return nil
	})
	return foreign, err
}

// shellQuotePath 在路径含 shell 元字符时加单引号，保证修复命令可直接粘贴执行。
func shellQuotePath(path string) string {
	if !strings.ContainsAny(path, " \t'\"\\$`") {
		return path
	}
	return `'` + strings.ReplaceAll(path, `'`, `'\''`) + `'`
}

// legacyPreflightPaths 迁移预检覆盖的旧状态根：控制面旧布局 + 同机存储节点旧状态。
func legacyPreflightPaths(q deployQuery, legacy legacyLayout) []string {
	paths := append([]string{}, legacy.paths...)
	if storage := detectLegacyStorageState(filepath.Join(q.repoRoot, "storager")); storage.present() {
		paths = append(paths, storage.paths...)
	}
	return paths
}

// assertLegacyOwnership 校验旧状态树属主；任一文件不属于当前用户时，
// 用一条 chown 命令列出全部需修复的根——逐条报会让用户反复重跑才发现下一个。
func assertLegacyOwnership(paths []string) error {
	var roots, offenders []string
	for _, p := range paths {
		foreign, err := firstForeignOwner(p)
		if err != nil {
			return fmt.Errorf("inspect legacy state %s: %w", p, err)
		}
		if foreign != "" {
			roots = append(roots, p)
			offenders = append(offenders, foreign)
		}
	}
	if len(roots) == 0 {
		return nil
	}
	quoted := make([]string, len(roots))
	for i, p := range roots {
		quoted[i] = shellQuotePath(p)
	}
	return fmt.Errorf("legacy state contains files owned by another user:\n  %s\nfix it with:\n  %s",
		strings.Join(offenders, "\n  "), fmt.Sprintf(chownRemediation, strings.Join(quoted, " ")))
}

// dockerComposeReady 预检 docker CLI 与 compose v2 可用。
func dockerComposeReady() error {
	if _, err := exec.LookPath("docker"); err != nil {
		return fmt.Errorf("docker is not installed or not on PATH (kurrent drives the deployment with docker compose)")
	}
	cmd := exec.Command("docker", "compose", "version")
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("docker compose v2 is not available (`docker compose version` failed): %w", err)
	}
	return nil
}

// controlPlaneBuildReady 校验 WebUI 构建产物存在且属于当前用户。
// 让 compose 去创建 dist 会得到 root 所有的目录，后续构建再也写不进去。
func controlPlaneBuildReady(repoRoot string) error {
	index := filepath.Join(repoRoot, "webui", "app", "dist", "index.html")
	fi, err := statPath(index)
	if err != nil {
		if os.IsNotExist(err) {
			return fmt.Errorf("the WebUI build is missing: %s\nbuild it first: (cd %s/webui/app && npm ci && npm run build)",
				index, repoRoot)
		}
		return fmt.Errorf("inspect %s: %w", index, err)
	}
	if fi.IsDir() {
		return fmt.Errorf("the WebUI build path is a directory, not a build output: %s", index)
	}
	if st, ok := fi.Sys().(*syscall.Stat_t); ok && int(st.Uid) != effectiveUID() {
		p := filepath.Join(repoRoot, "webui", "app", "dist")
		return fmt.Errorf("%s is owned by uid %d; Docker created it as root. Remove it and rebuild:\n  sudo rm -rf %s\n  (cd %s/webui/app && npm ci && npm run build)",
			p, st.Uid, p, repoRoot)
	}
	return nil
}

// preflightControlPlane 首次部署/重跑的只读预检；不修改任何文件。
func preflightControlPlane(q deployQuery, legacy legacyLayout, adopt bool) error {
	if err := dockerComposeReady(); err != nil {
		return err
	}
	for _, dir := range []string{
		filepath.Join(q.repoRoot, "control_plane"),
		filepath.Join(q.repoRoot, "storager"),
	} {
		if !isDir(dir) {
			return fmt.Errorf("repository layout is incomplete: %s is missing (pass --dir with the checkout root)", dir)
		}
	}
	if err := controlPlaneBuildReady(q.repoRoot); err != nil {
		return err
	}
	if err := assertRecordedStatePresent(q); err != nil {
		return err
	}
	if err := assertOwnedAndWritable(q.stateDir); err != nil {
		return err
	}
	if dir := userCredentialsDir(); dir != "" {
		if err := assertOwnedAndWritable(dir); err != nil {
			return err
		}
	}
	if legacy.present() {
		if !adopt {
			return fmt.Errorf("an existing deployment predates the state-root layout:\n  %s\n"+
				"rerun with --adopt-state to migrate it into %s (the old stacks are stopped and the copies removed after verification)",
				strings.Join(legacy.paths, "\n  "), q.stateDir)
		}
		// init 在同一次迁移里也接管存储节点旧状态（agent 状态/PKI/日志），
		// 属主检查必须一并前置：否则停栈并复制控制面状态后才失败，留下半迁移现场。
		if err := assertLegacyOwnership(legacyPreflightPaths(q, legacy)); err != nil {
			return err
		}
		// 迁移失败（凭据交换/自检）后状态根已有本机副本，记录仍为 bootstrapping：
		// 这是同一次事务的续跑，不能按“目标非空”拒绝；其余情况仍须空目录。
		if !resumableBootstrap(q) {
			if err := assertStateTargetEmpty(q.stateDir); err != nil {
				return err
			}
		}
	}
	if effectiveUID() == 0 {
		fmt.Fprintln(os.Stderr, "kurrent: warning: running as root; credentials will be installed into /root/.kurrent")
	}
	return nil
}

// assertRecordedStatePresent 部署记录存在但状态根消失：不能悄悄新建一个空部署
// （台账、PKI 与节点身份都在里面）。
func assertRecordedStatePresent(q deployQuery) error {
	if q.record == nil {
		return nil
	}
	if _, err := statPath(q.stateDir); os.IsNotExist(err) {
		return fmt.Errorf("the deployment record %s points at the state root %s, which no longer exists;\n"+
			"restore it from a backup, or remove the record to start a fresh deployment",
			deploymentRecordPath(), q.stateDir)
	}
	return nil
}

// assertStateTargetEmpty 迁移前要求状态根不存在或为空：绝不往有数据的目录里合并旧状态。
// resumableBootstrap 判断状态根是否属于一次中断的本机部署（记录匹配且仍在 bootstrapping）。
func resumableBootstrap(q deployQuery) bool {
	rec := q.record
	return rec != nil &&
		rec.StateDir == q.stateDir &&
		rec.RepoRoot == q.repoRoot &&
		rec.Roles.ControlPlane &&
		rec.Status == deployStatusBootstrapping
}

func assertStateTargetEmpty(stateDir string) error {
	entries, err := os.ReadDir(stateDir)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("inspect state directory %s: %w", stateDir, err)
	}
	if len(entries) > 0 {
		return fmt.Errorf("state directory already contains data: %s (choose an empty --state-dir before adopting legacy state)", stateDir)
	}
	return nil
}

// prepareControlPlaneState 创建控制面状态布局并种下 compose 文件挂载点。
// 容器启动前这些文件必须存在，否则 Docker 会以 root 创建同名目录。
func prepareControlPlaneState(paths controlPlanePaths) error {
	for _, dir := range []string{paths.root, paths.state, paths.config, paths.dnsmasq} {
		if err := ensurePrivateDir(dir); err != nil {
			return err
		}
	}
	if err := ensurePrivateDir(paths.bootstrap); err != nil {
		return err
	}
	for _, dir := range []string{
		filepath.Join(paths.state, "certs"),
		filepath.Join(paths.state, "pki", "nginx"),
	} {
		if err := ensureDir(dir, 0o755); err != nil {
			return err
		}
	}
	files := []struct {
		path string
		data string
		perm os.FileMode
	}{
		{paths.agentsFile, "agents: {}\n", 0o644},
		{filepath.Join(paths.dnsmasq, "dnsmasq.conf"), "# rendered by the control plane from spec.networking at startup\n", 0o644},
		{filepath.Join(paths.dnsmasq, "dhcp-hosts.conf"), "", 0o644},
		{filepath.Join(paths.dnsmasq, "dnsmasq.leases"), "", 0o644},
	}
	for _, f := range files {
		if err := seedFile(f.path, []byte(f.data), f.perm); err != nil {
			return err
		}
	}
	return nil
}

// prepareStoragerState 创建节点状态布局（身份、引导凭据、组件 PKI、日志、CP 信任锚目录）。
func prepareStoragerState(paths storagerPaths) error {
	for _, dir := range []string{
		paths.root, paths.state, paths.bootstrap, paths.pki, paths.logs, paths.certs,
	} {
		if err := ensurePrivateDir(dir); err != nil {
			return err
		}
	}
	if err := ensureDir(paths.root, 0o755); err != nil {
		return err
	}
	if err := ensurePrivateDir(paths.pki); err != nil {
		return err
	}
	if err := ensurePrivateDir(paths.certs); err != nil {
		return err
	}
	if paths.agentPKI != "" {
		if err := ensurePrivateDir(paths.agentPKI); err != nil {
			return err
		}
	}
	if paths.nvmetPKI != "" {
		if err := ensurePrivateDir(paths.nvmetPKI); err != nil {
			return err
		}
	}
	return nil
}

// legacyLayout 检测到的旧布局路径（仓库内状态）。
type legacyLayout struct {
	paths []string
}

func (l legacyLayout) present() bool { return len(l.paths) > 0 }

// legacyControlPlanePaths 仓库内旧控制面布局（control_plane/，与新状态根的
// control-plane/ 区分）。
func legacyControlPlanePaths(repoRoot string) controlPlanePaths {
	root := filepath.Join(repoRoot, "control_plane")
	state := filepath.Join(root, "state")
	return controlPlanePaths{
		root:       root,
		state:      state,
		config:     filepath.Join(root, "config"),
		agentsFile: filepath.Join(root, "config", "agents.yml"),
		dnsmasq:    filepath.Join(repoRoot, "dnsmasq"),
		bootstrap:  filepath.Join(root, "bootstrap"),
		adminConf:  filepath.Join(state, "admin.conf"),
		serverCert: filepath.Join(state, "certs", "server.crt"),
	}
}

// detectLegacyControlPlaneState 返回仓库内旧控制面状态路径；空目录不算（失败重跑残留）。
func detectLegacyControlPlaneState(repoRoot string) legacyLayout {
	cp := legacyControlPlanePaths(repoRoot)
	var paths []string
	for _, p := range []string{
		cp.state,
		cp.agentsFile,
		filepath.Join(repoRoot, "dnsmasq", "dnsmasq.conf"),
		filepath.Join(repoRoot, "dnsmasq", "dhcp-hosts.conf"),
		filepath.Join(repoRoot, "dnsmasq", "dnsmasq.leases"),
	} {
		if nonEmptyPath(p) {
			paths = append(paths, p)
		}
	}
	return legacyLayout{paths: paths}
}

// detectLegacyStorageState 返回仓库内旧节点状态路径。
func detectLegacyStorageState(storagerDir string) legacyLayout {
	var paths []string
	for _, p := range []string{
		filepath.Join(storagerDir, "state"),
		filepath.Join(storagerDir, "bootstrap"),
		filepath.Join(storagerDir, "agent", "logs"),
	} {
		if nonEmptyPath(p) {
			paths = append(paths, p)
		}
	}
	return legacyLayout{paths: paths}
}

// nonEmptyPath 路径存在且（目录非空或文件非空）。
func nonEmptyPath(path string) bool {
	fi, err := statPath(path)
	if err != nil {
		return false
	}
	if fi.IsDir() {
		entries, err := os.ReadDir(path)
		return err == nil && len(entries) > 0
	}
	return fi.Size() > 0
}

// adoptControlPlaneState 把旧控制面状态复制到新状态根（含节点组件 PKI 迁移）。
// 调用前栈已停止，且新状态根为空；复制逐文件做 SHA-256 校验。
func adoptControlPlaneState(repoRoot string, q deployQuery) error {
	legacy := legacyControlPlanePaths(repoRoot)
	target := q.controlPlane()
	if isDir(legacy.state) {
		if err := copyTreeVerified(legacy.state, target.state); err != nil {
			return fmt.Errorf("migrate control-plane state: %w", err)
		}
	}
	if fileExists(legacy.agentsFile) {
		data, err := os.ReadFile(legacy.agentsFile)
		if err != nil {
			return fmt.Errorf("read %s: %w", legacy.agentsFile, err)
		}
		if err := seedFile(target.agentsFile, data, 0o644); err != nil {
			return err
		}
	}
	for _, name := range []string{"dnsmasq.conf", "dhcp-hosts.conf", "dnsmasq.leases"} {
		src := filepath.Join(repoRoot, "dnsmasq", name)
		if !fileExists(src) {
			continue
		}
		data, err := os.ReadFile(src)
		if err != nil {
			return fmt.Errorf("read %s: %w", src, err)
		}
		if err := seedFile(filepath.Join(target.dnsmasq, name), data, 0o644); err != nil {
			return err
		}
	}
	return adoptNodePKI(legacy.state, q.stateDir)
}

// adoptNodePKI 把旧控制面 state 内的节点组件 PKI 复制到 <state>/storager/pki/。
// issued.yml 仍是证书台账权威；这里只恢复节点运行所需的 cert/key 文件。
func adoptNodePKI(legacyState, stateRoot string) error {
	srcRoot := filepath.Join(legacyState, "pki", "components")
	if !isDir(srcRoot) {
		return nil
	}
	entries, err := os.ReadDir(srcRoot)
	if err != nil {
		return fmt.Errorf("read %s: %w", srcRoot, err)
	}
	for _, e := range entries {
		name := e.Name()
		if !strings.HasPrefix(name, "agent-") && !strings.HasPrefix(name, "nvmet-") {
			continue
		}
		src := filepath.Join(srcRoot, name)
		dst := filepath.Join(stateRoot, "storager", "pki", name)
		if err := copyTreeVerified(src, dst); err != nil {
			return fmt.Errorf("migrate node PKI %s: %w", name, err)
		}
	}
	return nil
}

// adoptStorageState 把旧节点状态复制到新状态根。
func adoptStorageState(storagerDir string, q deployQuery) error {
	target := q.storager("")
	for _, item := range []struct{ src, dst string }{
		{filepath.Join(storagerDir, "state"), target.state},
		{filepath.Join(storagerDir, "bootstrap"), target.bootstrap},
		{filepath.Join(storagerDir, "agent", "logs"), target.logs},
	} {
		if !isDir(item.src) {
			continue
		}
		if err := copyTreeVerified(item.src, item.dst); err != nil {
			return fmt.Errorf("migrate %s: %w", item.src, err)
		}
	}
	if err := adoptNodePKI(filepath.Join(filepath.Dir(storagerDir), "control_plane", "state"), q.stateDir); err != nil {
		return err
	}
	// CP 信任锚：旧布局下 nginx 证书在控制面 state/certs；复制为节点副本（--cp-ca 仍可覆盖）。
	legacyCA := filepath.Join(filepath.Dir(storagerDir), "control_plane", "state", "certs", "server.crt")
	if fileExists(legacyCA) {
		data, err := os.ReadFile(legacyCA)
		if err != nil {
			return fmt.Errorf("read %s: %w", legacyCA, err)
		}
		if err := os.MkdirAll(target.certs, 0o700); err != nil {
			return err
		}
		if err := writeFileAtomic(target.cpCA, data, 0o644); err != nil {
			return fmt.Errorf("write %s: %w", target.cpCA, err)
		}
	}
	return nil
}

// removeLegacyControlPlaneState 验证成功后删除旧控制面状态副本（保留代码与 .example）。
func removeLegacyControlPlaneState(repoRoot string) error {
	cp := legacyControlPlanePaths(repoRoot)
	for _, p := range []string{
		cp.state,
		cp.agentsFile,
		filepath.Join(repoRoot, "dnsmasq", "dnsmasq.conf"),
		filepath.Join(repoRoot, "dnsmasq", "dhcp-hosts.conf"),
		filepath.Join(repoRoot, "dnsmasq", "dnsmasq.leases"),
	} {
		if err := os.RemoveAll(p); err != nil {
			return fmt.Errorf("remove legacy %s: %w", p, err)
		}
	}
	return nil
}

// removeLegacyStorageState 删除旧节点状态副本。
func removeLegacyStorageState(storagerDir string) error {
	for _, p := range []string{
		filepath.Join(storagerDir, "state"),
		filepath.Join(storagerDir, "bootstrap"),
		filepath.Join(storagerDir, "agent", "logs"),
	} {
		if err := os.RemoveAll(p); err != nil {
			return fmt.Errorf("remove legacy %s: %w", p, err)
		}
	}
	return nil
}

// copyTreeVerified 复制目录树（保留权限）并逐文件校验 SHA-256；拒绝符号链接。
func copyTreeVerified(src, dst string) error {
	if err := os.MkdirAll(dst, 0o700); err != nil {
		return err
	}
	err := filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		info, err := d.Info()
		if err != nil {
			return err
		}
		if d.Type()&fs.ModeSymlink != 0 {
			return fmt.Errorf("refusing to migrate symlink %s", path)
		}
		if d.IsDir() {
			return os.MkdirAll(target, info.Mode().Perm())
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("refusing to migrate special file %s", path)
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
			return err
		}
		if err := copyFile(path, target, info.Mode().Perm()); err != nil {
			return err
		}
		return nil
	})
	if err != nil {
		return err
	}
	return verifyTree(src, dst)
}

func copyFile(src, dst string, perm os.FileMode) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	// 续跑时目标可能是容器以 root 重写的文件：unlink 只需目录写权限，
	// 直接 O_TRUNC 会因文件属主/权限被拒（EACCES）。先删再以当前用户重建。
	if err := os.Remove(dst); err != nil && !os.IsNotExist(err) {
		return err
	}
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, perm)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	if err := out.Chmod(perm); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// verifyTree 逐文件比对 SHA-256，任一不一致即报错。
func verifyTree(src, dst string) error {
	return filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		want, err := hashFile(path)
		if err != nil {
			return err
		}
		got, err := hashFile(filepath.Join(dst, rel))
		if err != nil {
			return err
		}
		if want != got {
			return fmt.Errorf("copy verification failed for %s (sha256 %s != %s)", rel, got, want)
		}
		return nil
	})
}

func hashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
