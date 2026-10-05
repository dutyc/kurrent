// credentials 子命令：管理凭据的用户级快照（install / show / remove）。
//
// install 把当前发现的管理凭据复制为自包含快照 ~/.kurrent/（admin.conf 重写证书键为
// pki/<文件名> 相对路径 + installed-from/installed-at 溯源；目录 0700、文件 0600），
// 供二进制脱离仓库使用（PATH 安装 / 异地执行），并可整包拷走。
// 快照是复制而非跟随——凭据轮换后重跑 install 刷新，是否陈旧看 show 的 installed-at。
// show 查看当前生效凭据与快照状态；remove 只删快照，仓库源与已签发 token 不受影响。
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

func cmdCredentials(args []string) {
	if len(args) < 1 {
		credentialsUsage()
	}
	switch args[0] {
	case "install":
		cmdCredentialsInstall()
	case "show":
		cmdCredentialsShow()
	case "remove":
		cmdCredentialsRemove()
	default:
		credentialsUsage()
	}
}

func credentialsUsage() {
	fatal("用法: kurrent credentials install   把管理凭据装到 ~/.kurrent（自包含快照：admin.conf + pki/）\n" +
		"      kurrent credentials show      查看当前生效凭据与用户级快照状态\n" +
		"      kurrent credentials remove    删除用户级快照（仓库内凭据不受影响）\n" +
		"      install 后任意目录可运行 kurrent；凭据轮换后重跑 install 刷新")
}

func cmdCredentialsInstall() {
	target, source, err := installCredentials()
	if err != nil {
		fatal("%v", err)
	}
	fmt.Printf("管理凭据已安装：%s（源: %s）\n", target, source)
	fmt.Printf("  证书副本：%s（自包含，可整目录拷贝到其他机器）\n", filepath.Join(filepath.Dir(target), "pki"))
	fmt.Println("  此后任意目录可运行 kurrent；凭据轮换后重跑 kurrent credentials install 刷新")
}

func cmdCredentialsShow() {
	out, err := credentialsShow()
	if err != nil {
		fatal("%v", err)
	}
	fmt.Print(out)
}

func cmdCredentialsRemove() {
	if err := removeCredentials(); err != nil {
		fatal("%v", err)
	}
	fmt.Println("用户级管理凭据已移除（仓库内凭据与已签发的 --token 不受影响）")
}

// installCredentials 把当前发现的管理凭据复制为自包含快照到 ~/.kurrent/，返回快照路径与源路径。
//
// 源 = discoverCredentials()（--credentials / KURRENT_CREDENTIALS / 仓库发现）；源即快照本身
// 时报错（无从刷新，需 --credentials 指向仓库源）。证书按 pki/<basename> 复制（同名不同源拒绝），
// admin.conf 重写为相对键 + 溯源键；先写临时文件再 rename，避免半写快照被后续命令读到。
func installCredentials() (target, source string, err error) {
	source = discoverCredentials()
	if source == "" {
		return "", "", fmt.Errorf("未发现管理凭据源（control_plane/state/admin.conf）——请先在仓库运行 kurrent init，或用 --credentials 指定源")
	}
	if abs, err := filepath.Abs(source); err == nil {
		source = abs
	}
	target = userCredentialsPath()
	if target == "" {
		return "", "", fmt.Errorf("无法确定用户主目录（安装目标 ~/.kurrent）")
	}
	if source == target {
		return "", "", fmt.Errorf("发现的管理凭据即用户级快照本身（%s）——刷新需以仓库 admin.conf 为源（进仓库重跑，或 --credentials 指定）", target)
	}
	data, err := os.ReadFile(source)
	if err != nil {
		return "", "", fmt.Errorf("读取管理凭据 %s: %w", source, err)
	}
	var cf credentialsFile
	if err := yaml.Unmarshal(data, &cf); err != nil {
		return "", "", fmt.Errorf("解析管理凭据 %s: %w", source, err)
	}
	if cf.Server == "" || cf.ClientCert == "" || cf.ClientKey == "" {
		return "", "", fmt.Errorf("管理凭据 %s 缺少 server/client-certificate/client-key", source)
	}

	dir := filepath.Dir(target)
	pkiDir := filepath.Join(dir, "pki")
	if err := os.MkdirAll(pkiDir, 0o700); err != nil {
		return "", "", fmt.Errorf("创建 %s: %w", pkiDir, err)
	}
	// 已存在目录收敛权限：快照含私钥，不留 group/other 位
	_ = os.Chmod(dir, 0o700)
	_ = os.Chmod(pkiDir, 0o700)

	srcDir := filepath.Dir(source)
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
			return "", "", fmt.Errorf("读取证书 %s（admin.conf 的 %s 键）: %w", sp, k.key, err)
		}
		base := filepath.Base(sp)
		if prev, ok := seen[base]; ok && prev != sp {
			return "", "", fmt.Errorf("证书文件名冲突：%s 与 %s 同名（%s）——请调整源目录布局", prev, sp, base)
		}
		seen[base] = sp
		rel := filepath.Join("pki", base)
		if err := writeFileAtomic(filepath.Join(dir, rel), pem, 0o600); err != nil {
			return "", "", fmt.Errorf("写入证书快照: %w", err)
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
		return "", "", fmt.Errorf("序列化快照 admin.conf: %w", err)
	}
	header := "# kurrent 用户级管理凭据快照（kurrent credentials install 生成；凭据轮换后重跑刷新）\n"
	if err := writeFileAtomic(target, append([]byte(header), body...), 0o600); err != nil {
		return "", "", fmt.Errorf("写入快照 %s: %w", target, err)
	}
	return target, source, nil
}

// credentialsShow 汇总当前生效凭据（发现层级 + 四键可解析性）与用户级快照状态（诊断用；
// 未发现凭据不视为错误——show 的职责是如实汇报发现链结果）。
func credentialsShow() (string, error) {
	var b strings.Builder
	path := discoverCredentials()
	if path == "" {
		b.WriteString("生效管理凭据: 未发现（将以无凭据通道访问 --server/--token）\n")
		b.WriteString("  安装：kurrent credentials install（仓库内执行，装到 ~/.kurrent 后任意目录可用）\n")
	} else {
		fmt.Fprintf(&b, "生效管理凭据: %s（%s）\n", path, describeCredentialsSource(path))
		data, err := os.ReadFile(path)
		if err != nil {
			return "", fmt.Errorf("读取管理凭据 %s: %w", path, err)
		}
		var cf credentialsFile
		if err := yaml.Unmarshal(data, &cf); err != nil {
			return "", fmt.Errorf("解析管理凭据 %s: %w", path, err)
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
			mark := "存在"
			if !fileExists(abs) {
				mark = "缺失"
			}
			fmt.Fprintf(&b, "  %s: %s（%s）\n", k.label, abs, mark)
		}
		if cf.InstalledFrom != "" {
			fmt.Fprintf(&b, "  installed-from: %s\n  installed-at: %s\n", cf.InstalledFrom, cf.InstalledAt)
		}
	}
	if up := userCredentialsPath(); up != "" && up != path {
		if fileExists(up) {
			fmt.Fprintf(&b, "用户级快照: %s（已安装；仓库源在场时仓库源优先）\n", up)
		} else {
			b.WriteString("用户级快照: 未安装（kurrent credentials install 后任意目录可用）\n")
		}
	}
	return b.String(), nil
}

// describeCredentialsSource 说明生效凭据来自发现链的哪一层（show 输出用）。
func describeCredentialsSource(path string) string {
	if flagCredentials != "" {
		return "--credentials / KURRENT_CREDENTIALS 指定"
	}
	if userCredentialsPath() == path {
		return "用户级快照（kurrent credentials install 安装）"
	}
	return "自动发现（仓库上溯或可执行文件旁）"
}

// removeCredentials 删除用户级快照（admin.conf 与 pki/）；仓库内凭据与 --token 不受影响。
func removeCredentials() error {
	target := userCredentialsPath()
	if target == "" {
		return fmt.Errorf("无法确定用户主目录（~/.kurrent）")
	}
	if !fileExists(target) {
		return fmt.Errorf("未安装用户级管理凭据（%s 不存在）", target)
	}
	dir := filepath.Dir(target)
	if err := os.Remove(target); err != nil {
		return fmt.Errorf("删除 %s: %w", target, err)
	}
	if err := os.RemoveAll(filepath.Join(dir, "pki")); err != nil {
		return fmt.Errorf("删除 %s: %w", filepath.Join(dir, "pki"), err)
	}
	os.Remove(dir) // 余物为空才删除；用户另放文件则保留目录
	return nil
}

// writeFileAtomic 同目录临时文件 + rename（防半写；快照含私钥，权限恒收敛为 perm）。
func writeFileAtomic(path string, data []byte, perm os.FileMode) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, perm); err != nil {
		return err
	}
	_ = os.Chmod(tmp, perm)
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		return err
	}
	return nil
}
