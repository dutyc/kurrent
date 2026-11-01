// credentials 子命令：管理凭据的用户级快照（install / show / remove）。
//
// install 把当前发现的管理凭据复制为自包含快照 ~/.kurrent/（admin.conf 重写证书键为
// pki/<文件名> 相对路径 + installed-from/installed-at 溯源；目录 0700、文件 0600），
// 供二进制脱离仓库使用（PATH 安装 / 异地执行），并可整包拷走。
// 快照是复制而非跟随——凭据轮换后重跑 install 刷新，是否陈旧看 show 的 installed-at。
// show 查看当前生效凭据与快照状态；remove 只删快照，仓库源与已签发 token 不受影响。
package main

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

var (
	credentialsInstallSpec = cmdSpec{usage: credentialsInstallUsage, maxPos: 0}
	credentialsShowSpec    = cmdSpec{usage: credentialsShowUsage, maxPos: 0}
	credentialsRemoveSpec  = cmdSpec{usage: credentialsRemoveUsage, maxPos: 0}
)

func cmdCredentials(args []string) {
	if groupHelp("credentials", args, credentialsGroupUsage) {
		return
	}
	switch args[0] {
	case "install":
		credentialsInstall(args[1:])
	case "show":
		credentialsShow(args[1:])
	case "remove":
		credentialsRemove(args[1:])
	default:
		usageError(credentialsGroupUsage, fmt.Errorf("unknown credentials subcommand: %s", args[0]))
	}
}

func credentialsInstall(args []string) {
	if _, help := mustParse(args, credentialsInstallSpec); help {
		return
	}
	target, source, err := installCredentials()
	if err != nil {
		fatal("%v", err)
	}
	fmt.Printf("management credentials installed: %s (source: %s)\n", target, source)
	fmt.Printf("  certificate copies: %s (self-contained; the whole directory can be copied to another machine)\n", filepath.Join(filepath.Dir(target), "pki"))
	fmt.Println("  kurrent now works from any directory; re-run kurrent credentials install after credential rotation")
}

func credentialsShow(args []string) {
	if _, help := mustParse(args, credentialsShowSpec); help {
		return
	}
	out, err := credentialsShowText()
	if err != nil {
		fatal("%v", err)
	}
	fmt.Print(out)
}

func credentialsRemove(args []string) {
	if _, help := mustParse(args, credentialsRemoveSpec); help {
		return
	}
	if err := removeCredentials(); err != nil {
		fatal("%v", err)
	}
	fmt.Println("user-level management credentials removed (repository credentials and issued --token values are unaffected)")
}

// installCredentials 把当前发现的管理凭据复制为自包含快照到 ~/.kurrent/，返回快照路径与源路径。
//
// 源 = discoverCredentials()（--credentials / KURRENT_CREDENTIALS / 仓库发现）；源即快照本身
// 时报错（无从刷新，需 --credentials 指向仓库源）。证书按 pki/<basename> 复制（同名不同源拒绝），
// admin.conf 重写为相对键 + 溯源键；先写临时文件再 rename，避免半写快照被后续命令读到。
func installCredentials() (target, source string, err error) {
	source = discoverCredentials()
	if source == "" {
		return "", "", fmt.Errorf("no management credential source found (control_plane/state/admin.conf); run kurrent init inside the repository first, or pass --credentials")
	}
	if abs, err := filepath.Abs(source); err == nil {
		source = abs
	}
	target = userCredentialsPath()
	if target == "" {
		return "", "", fmt.Errorf("cannot determine the user home directory (install target ~/.kurrent)")
	}
	if source == target {
		return "", "", fmt.Errorf("the discovered credential is the user-level snapshot itself (%s); refresh from the repository admin.conf (run inside the repository or pass --credentials)", target)
	}
	data, err := os.ReadFile(source)
	if err != nil {
		return "", "", fmt.Errorf("read management credentials %s: %w", source, err)
	}
	var cf credentialsFile
	if err := yaml.Unmarshal(data, &cf); err != nil {
		return "", "", fmt.Errorf("parse management credentials %s: %w", source, err)
	}
	if cf.Server == "" || cf.ClientCert == "" || cf.ClientKey == "" {
		return "", "", fmt.Errorf("management credentials %s are missing server/client-certificate/client-key", source)
	}
	if _, err := validateHTTPURL("management credential server", cf.Server); err != nil {
		return "", "", fmt.Errorf("%s: %w", source, err)
	}

	srcDir := filepath.Dir(source)
	absSrc := func(p string) string {
		if p == "" || filepath.IsAbs(p) {
			return p
		}
		return filepath.Join(srcDir, p)
	}
	// 安装前先确认快照可用：证书/私钥配对、CA 可解析。否则“安装成功”的
	// 快照仍会在第一次请求时失败。
	if _, err := tls.LoadX509KeyPair(absSrc(cf.ClientCert), absSrc(cf.ClientKey)); err != nil {
		return "", "", fmt.Errorf("management certificate/private key unusable (%s): %w", source, err)
	}
	if cf.CA != "" {
		caPEM, err := os.ReadFile(absSrc(cf.CA))
		if err != nil {
			return "", "", fmt.Errorf("read management CA %s: %w", absSrc(cf.CA), err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(caPEM) {
			return "", "", fmt.Errorf("management CA %s contains no valid certificate", absSrc(cf.CA))
		}
	}

	dir := filepath.Dir(target)
	pkiDir := filepath.Join(dir, "pki")
	if err := os.MkdirAll(pkiDir, 0o700); err != nil {
		return "", "", fmt.Errorf("create %s: %w", pkiDir, err)
	}
	// 已存在目录收敛权限：快照含私钥，不留 group/other 位
	_ = os.Chmod(dir, 0o700)
	_ = os.Chmod(pkiDir, 0o700)

	seen := map[string]string{} // basename → 源路径（同名不同源拒绝）
	installed := map[string]string{}
	for _, k := range []struct{ key, path string }{
		{"certificate-authority", cf.CA},
		{"client-certificate", cf.ClientCert},
		{"client-key", cf.ClientKey},
	} {
		if k.path == "" {
			continue
		}
		sp := k.path
		if !filepath.IsAbs(sp) {
			sp = filepath.Join(srcDir, sp)
		}
		pem, err := os.ReadFile(sp)
		if err != nil {
			return "", "", fmt.Errorf("read certificate %s (admin.conf key %s): %w", sp, k.key, err)
		}
		base := filepath.Base(sp)
		if prev, ok := seen[base]; ok && prev != sp {
			return "", "", fmt.Errorf("certificate filename conflict: %s and %s share the name %s; adjust the source directory layout", prev, sp, base)
		}
		seen[base] = sp
		rel := filepath.Join("pki", base)
		if err := writeFileAtomic(filepath.Join(dir, rel), pem, 0o600); err != nil {
			return "", "", fmt.Errorf("write certificate snapshot: %w", err)
		}
		installed[k.key] = rel
	}

	out := credentialsFile{
		Server:        cf.Server,
		CA:            installed["certificate-authority"],
		ClientCert:    installed["client-certificate"],
		ClientKey:     installed["client-key"],
		InstalledFrom: source,
		InstalledAt:   time.Now().Format(time.RFC3339),
	}
	body, err := yaml.Marshal(out)
	if err != nil {
		return "", "", fmt.Errorf("serialize snapshot admin.conf: %w", err)
	}
	header := "# kurrent user-level management credential snapshot (created by kurrent credentials install; re-run after rotation)\n"
	if err := writeFileAtomic(target, append([]byte(header), body...), 0o600); err != nil {
		return "", "", fmt.Errorf("write snapshot %s: %w", target, err)
	}
	return target, source, nil
}

// credentialsShow 汇总当前生效凭据（发现层级 + 四键可解析性）与用户级快照状态（诊断用；
// 未发现凭据不视为错误——show 的职责是如实汇报发现链结果）。
func credentialsShowText() (string, error) {
	var b strings.Builder
	path := discoverCredentials()
	if path == "" {
		b.WriteString("active management credentials: not found (requests use the credential-less --server/--token channel)\n")
		b.WriteString("  install: kurrent credentials install (run in the repository; afterwards works from any directory)\n")
	} else {
		fmt.Fprintf(&b, "active management credentials: %s (%s)\n", path, describeCredentialsSource(path))
		data, err := os.ReadFile(path)
		if err != nil {
			return "", fmt.Errorf("read management credentials %s: %w", path, err)
		}
		var cf credentialsFile
		if err := yaml.Unmarshal(data, &cf); err != nil {
			return "", fmt.Errorf("parse management credentials %s: %w", path, err)
		}
		fmt.Fprintf(&b, "  server: %s\n", cf.Server)
		dir := filepath.Dir(path)
		for _, k := range []struct{ label, p string }{
			{"certificate-authority", cf.CA},
			{"client-certificate", cf.ClientCert},
			{"client-key", cf.ClientKey},
		} {
			if k.p == "" {
				continue
			}
			abs := k.p
			if !filepath.IsAbs(abs) {
				abs = filepath.Join(dir, abs)
			}
			mark := "present"
			if !fileExists(abs) {
				mark = "missing"
			}
			fmt.Fprintf(&b, "  %s: %s (%s)\n", k.label, abs, mark)
		}
		if cf.InstalledFrom != "" {
			fmt.Fprintf(&b, "  installed-from: %s\n  installed-at: %s\n", cf.InstalledFrom, cf.InstalledAt)
		}
	}
	if up := userCredentialsPath(); up != "" && up != path {
		if fileExists(up) {
			fmt.Fprintf(&b, "user-level snapshot: %s (installed; the repository source takes precedence when present)\n", up)
		} else {
			b.WriteString("user-level snapshot: not installed (run kurrent credentials install to work from any directory)\n")
		}
	}
	return b.String(), nil
}

// describeCredentialsSource 说明生效凭据来自发现链的哪一层（show 输出用）。
func describeCredentialsSource(path string) string {
	if flagCredentials != "" {
		return "specified by --credentials / KURRENT_CREDENTIALS"
	}
	if userCredentialsPath() == path {
		return "user-level snapshot installed by kurrent credentials install"
	}
	return "auto-discovered via repository walk-up or next to the executable"
}

// removeCredentials 删除用户级快照（admin.conf 与 pki/）；仓库内凭据与 --token 不受影响。
func removeCredentials() error {
	target := userCredentialsPath()
	if target == "" {
		return fmt.Errorf("cannot determine the user home directory (~/.kurrent)")
	}
	if !fileExists(target) {
		return fmt.Errorf("user-level management credentials are not installed (%s does not exist)", target)
	}
	dir := filepath.Dir(target)
	if err := os.Remove(target); err != nil {
		return fmt.Errorf("delete %s: %w", target, err)
	}
	if err := os.RemoveAll(filepath.Join(dir, "pki")); err != nil {
		return fmt.Errorf("delete %s: %w", filepath.Join(dir, "pki"), err)
	}
	os.Remove(dir) // 余物为空才删除；用户另放文件则保留目录
	return nil
}

// writeFileAtomic 同目录临时文件 + rename（防半写；文件含私钥/密钥时权限恒为 perm）。
// CreateTemp 生成唯一临时名：并发 install/join 不会互相覆盖，也不会跟随预置的 .tmp 路径。
func writeFileAtomic(path string, data []byte, perm os.FileMode) error {
	f, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer func() {
		if tmp != "" {
			os.Remove(tmp)
		}
	}()
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Chmod(perm); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		return err
	}
	tmp = ""
	return nil
}
