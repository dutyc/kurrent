package command

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"kurrent/cli/internal/cli"
	"kurrent/cli/internal/creds"
	"kurrent/cli/internal/fsx"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

// selfSignedCert 生成自签证书（测试用；CN 即身份）。
func selfSignedCert(t *testing.T, cn string, ips []net.IP) (certPEM, keyPEM []byte) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(time.Now().UnixNano()),
		Subject:               pkix.Name{CommonName: cn},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
		IPAddresses:           ips,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("issue certificate: %v", err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatalf("marshal private key: %v", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
		pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
}

func writeFile(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

// stubUserHome 注入临时用户主目录（隔离真实 ~/.kurrent；返回注入的 home）。
func stubUserHome(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	old := fsx.UserHomeDir
	fsx.UserHomeDir = func() string { return home }
	t.Cleanup(func() { fsx.UserHomeDir = old })
	return home
}

// clearCredentialFlags 清空凭据相关全局 flag 与环境变量（发现链前两跳归零，走纯自动发现）。
func clearCredentialFlags(t *testing.T) {
	t.Helper()
	t.Setenv("KURRENT_CREDENTIALS", "")
	oldCred, oldServer, oldToken := cli.Credentials, cli.Server, cli.Token
	cli.Credentials, cli.Server, cli.Token = "", "", ""
	t.Cleanup(func() { cli.Credentials, cli.Server, cli.Token = oldCred, oldServer, oldToken })
}

// stubExeDir 注入可执行文件目录（隔离真实二进制位置）。
func stubExeDir(t *testing.T, dir string) {
	t.Helper()
	old := fsx.ExeDir
	fsx.ExeDir = func() string { return dir }
	t.Cleanup(func() { fsx.ExeDir = old })
}

// makeRepoCredentials 构造仓库布局的管理凭据（control_plane/state/admin.conf +
// 证书文件，与真实自举产物同构），返回 admin.conf 路径。
func makeRepoCredentials(t *testing.T, root string) string {
	t.Helper()
	stateDir := filepath.Join(root, "control_plane", "state")
	for _, d := range []string{
		filepath.Join(stateDir, "certs"),
		filepath.Join(stateDir, "pki", "users", "admin"),
	} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	caPEM, _ := selfSignedCert(t, "kurrent-ca", nil)
	clientCertPEM, clientKeyPEM := selfSignedCert(t, "admin", nil)
	writeFile(t, filepath.Join(stateDir, "certs", "server.crt"), caPEM)
	writeFile(t, filepath.Join(stateDir, "pki", "users", "admin", "client.crt"), clientCertPEM)
	writeFile(t, filepath.Join(stateDir, "pki", "users", "admin", "client.key"), clientKeyPEM)
	conf := filepath.Join(stateDir, "admin.conf")
	writeFile(t, conf, []byte("server: https://127.0.0.1/api/cp\n"+
		"certificate-authority: certs/server.crt\n"+
		"client-certificate: pki/users/admin/client.crt\n"+
		"client-key: pki/users/admin/client.key\n"))
	return conf
}

// readConf 解析凭据文件（快照内容断言用）。
func readConf(t *testing.T, path string) creds.CredentialsFile {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	var cf creds.CredentialsFile
	if err := yaml.Unmarshal(data, &cf); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	return cf
}

// copyDir 递归复制目录（模拟快照整包拷走）。
func copyDir(t *testing.T, src, dst string) {
	t.Helper()
	err := filepath.WalkDir(src, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		if d.IsDir() {
			return os.MkdirAll(target, 0o700)
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		return os.WriteFile(target, data, 0o600)
	})
	if err != nil {
		t.Fatalf("copy directory %s -> %s: %v", src, dst, err)
	}
}

// adminConfBody 完整的管理凭据文件内容（三键齐备，避免 creds.LoadCredentials 因缺键 fatal 退出测试进程）；
// 证书路径只做发现/解析用，测试不实际加载证书，故指向不存在的相对路径即可。
const adminConfBody = "server: https://127.0.0.1/api/cp\n" +
	"client-certificate: admin.crt\nclient-key: admin.key\n"

// TestCredentialsInstall：install 复制为自包含快照（相对键 + 溯源 + 0700/0600 权限），
// 仓库外命中快照且证书可加载；整目录拷贝到别处仍可用。
func TestCredentialsInstall(t *testing.T) {
	clearCredentialFlags(t)
	home := stubUserHome(t)
	stubExeDir(t, filepath.Join(t.TempDir(), "cli"))
	repo := t.TempDir()
	conf := makeRepoCredentials(t, repo)
	t.Chdir(repo)

	target, source, err := installCredentialsFrom(creds.DiscoverCredentials(), "")
	if err != nil {
		t.Fatalf("install: %v", err)
	}
	wantTarget := filepath.Join(home, ".kurrent", "admin.conf")
	if target != wantTarget || source != conf {
		t.Fatalf("install returned target=%q source=%q, want %q / %q", target, source, wantTarget, conf)
	}

	snapDir := filepath.Dir(target)
	if fi, err := os.Stat(snapDir); err != nil || fi.Mode().Perm() != 0o700 {
		t.Fatalf("snapshot directory has unexpected permissions (err=%v)", err)
	}
	if fi, err := os.Stat(target); err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("snapshot admin.conf has unexpected permissions (err=%v)", err)
	}

	cf := readConf(t, target)
	if cf.Server != "https://127.0.0.1/api/cp" ||
		cf.CA != "pki/server.crt" || cf.ClientCert != "pki/client.crt" || cf.ClientKey != "pki/client.key" {
		t.Fatalf("snapshot cert keys must be rewritten to pki/<file> relative paths: %+v", cf)
	}
	if cf.InstalledFrom != conf || cf.InstalledAt == "" {
		t.Fatalf("snapshot is missing provenance keys: %+v", cf)
	}
	for rel, srcPath := range map[string]string{
		"pki/server.crt": filepath.Join(repo, "control_plane/state/certs/server.crt"),
		"pki/client.crt": filepath.Join(repo, "control_plane/state/pki/users/admin/client.crt"),
		"pki/client.key": filepath.Join(repo, "control_plane/state/pki/users/admin/client.key"),
	} {
		got, err := os.ReadFile(filepath.Join(snapDir, rel))
		if err != nil {
			t.Fatalf("snapshot is missing %s: %v", rel, err)
		}
		want, _ := os.ReadFile(srcPath)
		if !bytes.Equal(got, want) {
			t.Fatalf("%s differs from the source", rel)
		}
		if fi, _ := os.Stat(filepath.Join(snapDir, rel)); fi.Mode().Perm() != 0o600 {
			t.Fatalf("%s mode %v, want 0600", rel, fi.Mode().Perm())
		}
	}

	// 脱离仓库：仓库外命中用户级快照，证书经快照内副本加载
	outside := t.TempDir()
	t.Chdir(outside)
	if p := creds.DiscoverCredentials(); p != wantTarget {
		t.Fatalf("outside the repository the user-level snapshot %s should match, got %q", wantTarget, p)
	}
	cc := creds.LoadCredentials()
	if cc == nil || cc.CertFile != filepath.Join(snapDir, "pki", "client.crt") {
		t.Fatalf("snapshot credentials failed to parse: %+v", cc)
	}
	_ = cc.Client() // 证书副本可加载（失败会以 fatal 终止测试进程）

	// 自包含：整目录拷贝到别处，凭相对键仍可用
	moved := filepath.Join(t.TempDir(), "admin-creds")
	copyDir(t, snapDir, moved)
	cli.Credentials = filepath.Join(moved, "admin.conf")
	cc2 := creds.LoadCredentials()
	if cc2 == nil || cc2.CertFile != filepath.Join(moved, "pki", "client.crt") {
		t.Fatalf("copied snapshot is unusable: %+v", cc2)
	}
	_ = cc2.Client()
}

// TestCredentialsInstallRefresh：重跑 install 刷新快照（快照被篡改后恢复为源内容）。
func TestCredentialsInstallRefresh(t *testing.T) {
	clearCredentialFlags(t)
	stubUserHome(t)
	stubExeDir(t, filepath.Join(t.TempDir(), "cli"))
	repo := t.TempDir()
	makeRepoCredentials(t, repo)
	t.Chdir(repo)

	target, _, err := installCredentialsFrom(creds.DiscoverCredentials(), "")
	if err != nil {
		t.Fatal(err)
	}
	copied := filepath.Join(filepath.Dir(target), "pki", "client.key")
	if err := os.WriteFile(copied, []byte("tampered"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := installCredentialsFrom(creds.DiscoverCredentials(), ""); err != nil {
		t.Fatalf("rerun install: %v", err)
	}
	got, _ := os.ReadFile(copied)
	want, _ := os.ReadFile(filepath.Join(repo, "control_plane/state/pki/users/admin/client.key"))
	if !bytes.Equal(got, want) {
		t.Fatal("rerun install did not refresh the certificate copies")
	}
}

// TestCredentialsInstallRefuseSelf：源即快照本身时拒绝（无从刷新，须 --credentials 指向仓库源）。
func TestCredentialsInstallRefuseSelf(t *testing.T) {
	clearCredentialFlags(t)
	home := stubUserHome(t)
	stubExeDir(t, filepath.Join(t.TempDir(), "cli"))
	t.Chdir(t.TempDir())

	snapDir := filepath.Join(home, ".kurrent")
	if err := os.MkdirAll(filepath.Join(snapDir, "pki"), 0o700); err != nil {
		t.Fatal(err)
	}
	writeFile(t, filepath.Join(snapDir, "admin.conf"), []byte(adminConfBody))
	if _, _, err := installCredentialsFrom(creds.DiscoverCredentials(), ""); err == nil || !strings.Contains(err.Error(), "user-level snapshot") {
		t.Fatalf("using the snapshot as the source must be rejected, got err=%v", err)
	}
}

// TestCredentialsDiscoveryPriority：仓库源（cwd 上溯）优先于用户级快照；仓库外才吃快照。
func TestCredentialsDiscoveryPriority(t *testing.T) {
	clearCredentialFlags(t)
	stubUserHome(t)
	repo := t.TempDir()
	conf := makeRepoCredentials(t, repo)

	snap := fsx.UserCredentialsPath()
	if err := os.MkdirAll(filepath.Dir(snap), 0o700); err != nil {
		t.Fatal(err)
	}
	writeFile(t, snap, []byte(adminConfBody))

	sub := filepath.Join(repo, "cli")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(sub)
	if p := creds.DiscoverCredentials(); p != conf {
		t.Fatalf("inside the repository the live source %s must win, got %q", conf, p)
	}

	outside := t.TempDir()
	stubExeDir(t, filepath.Join(outside, "bin"))
	t.Chdir(outside)
	if p := creds.DiscoverCredentials(); p != snap {
		t.Fatalf("outside the repository the user-level snapshot %s should match, got %q", snap, p)
	}
}

// TestCredentialsRemove：remove 只删用户级快照（仓库源不动）；未安装报错。
func TestCredentialsRemove(t *testing.T) {
	clearCredentialFlags(t)
	stubUserHome(t)
	stubExeDir(t, filepath.Join(t.TempDir(), "cli"))
	repo := t.TempDir()
	conf := makeRepoCredentials(t, repo)
	t.Chdir(repo)

	target, _, err := installCredentialsFrom(creds.DiscoverCredentials(), "")
	if err != nil {
		t.Fatal(err)
	}
	if err := removeCredentials(); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if fsx.FileExists(target) || fsx.FileExists(filepath.Join(filepath.Dir(target), "pki")) {
		t.Fatal("snapshot was not fully removed")
	}
	if !fsx.FileExists(conf) {
		t.Fatal("remove must not touch the repository source")
	}

	outside := t.TempDir()
	t.Chdir(outside)
	if p := creds.DiscoverCredentials(); p != "" {
		t.Fatalf("no credentials should be discovered outside the repository after removal, got %q", p)
	}
	if err := removeCredentials(); err == nil || !strings.Contains(err.Error(), "not installed") {
		t.Fatalf("a repeated remove must report not installed, got err=%v", err)
	}
}

// TestCredentialsShow：show 汇报生效凭据层级、快照状态与溯源。
func TestCredentialsShow(t *testing.T) {
	clearCredentialFlags(t)
	stubUserHome(t)
	stubExeDir(t, filepath.Join(t.TempDir(), "cli"))
	repo := t.TempDir()
	conf := makeRepoCredentials(t, repo)
	t.Chdir(repo)

	out, err := credentialsShowText()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, conf) || !strings.Contains(out, "not installed") {
		t.Fatalf("show must include the active credentials and the not-installed hint: %s", out)
	}

	if _, _, err := installCredentialsFrom(creds.DiscoverCredentials(), ""); err != nil {
		t.Fatal(err)
	}
	outside := t.TempDir()
	stubExeDir(t, filepath.Join(outside, "bin"))
	t.Chdir(outside)
	out, err = credentialsShowText()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, "user-level snapshot") || !strings.Contains(out, "installed-from: "+conf) {
		t.Fatalf("show must include the snapshot source and provenance: %s", out)
	}
}
