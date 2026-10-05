package main

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
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
		t.Fatalf("生成密钥: %v", err)
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
		t.Fatalf("签发证书: %v", err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatalf("序列化私钥: %v", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
		pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
}

func writeFile(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatalf("写文件 %s: %v", path, err)
	}
}

// stubUserHome 注入临时用户主目录（隔离真实 ~/.kurrent；返回注入的 home）。
func stubUserHome(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	old := userHomeDir
	userHomeDir = func() string { return home }
	t.Cleanup(func() { userHomeDir = old })
	return home
}

// clearCredentialFlags 清空凭据相关全局 flag 与环境变量（发现链前两跳归零，走纯自动发现）。
func clearCredentialFlags(t *testing.T) {
	t.Helper()
	t.Setenv("KURRENT_CREDENTIALS", "")
	oldCred, oldServer, oldToken := flagCredentials, flagServer, flagToken
	flagCredentials, flagServer, flagToken = "", "", ""
	t.Cleanup(func() { flagCredentials, flagServer, flagToken = oldCred, oldServer, oldToken })
}

// stubExeDir 注入可执行文件目录（隔离真实二进制位置）。
func stubExeDir(t *testing.T, dir string) {
	t.Helper()
	old := exeDir
	exeDir = func() string { return dir }
	t.Cleanup(func() { exeDir = old })
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
func readConf(t *testing.T, path string) credentialsFile {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读取 %s: %v", path, err)
	}
	var cf credentialsFile
	if err := yaml.Unmarshal(data, &cf); err != nil {
		t.Fatalf("解析 %s: %v", path, err)
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
		t.Fatalf("复制目录 %s → %s: %v", src, dst, err)
	}
}

// adminConfBody 完整的管理凭据文件内容（三键齐备，避免 loadCredentials 因缺键 fatal 退出测试进程）；
// 证书路径只做发现/解析用，测试不实际加载证书，故指向不存在的相对路径即可。
const adminConfBody = "server: https://127.0.0.1/api/cp\n" +
	"client-certificate: admin.crt\nclient-key: admin.key\n"

// TestCredentialsMTLSRequest：admin.conf（扁平 YAML，相对路径）→ mTLS 请求，服务端验证客户端证书 CN。
func TestCredentialsMTLSRequest(t *testing.T) {
	clientCertPEM, clientKeyPEM := selfSignedCert(t, "admin", nil)
	clientPool := x509.NewCertPool()
	if !clientPool.AppendCertsFromPEM(clientCertPEM) {
		t.Fatal("客户端证书入池失败")
	}

	var peerCN string
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.TLS != nil && len(r.TLS.PeerCertificates) > 0 {
			peerCN = r.TLS.PeerCertificates[0].Subject.CommonName
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"name":"admin","groups":["admin"],"method":"cert"}`))
	}))
	srv.TLS = &tls.Config{ClientAuth: tls.RequireAndVerifyClientCert, ClientCAs: clientPool}
	srv.StartTLS()
	defer srv.Close()

	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "admin.crt"), clientCertPEM)
	writeFile(t, filepath.Join(dir, "admin.key"), clientKeyPEM)
	writeFile(t, filepath.Join(dir, "server.crt"), pem.EncodeToMemory(
		&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw}))
	confPath := filepath.Join(dir, "admin.conf")
	writeFile(t, confPath, []byte("server: "+srv.URL+"\ncertificate-authority: server.crt\n"+
		"client-certificate: admin.crt\nclient-key: admin.key\n"))

	old := flagCredentials
	flagCredentials = confPath
	t.Cleanup(func() { flagCredentials = old })

	cc := loadCredentials()
	if cc == nil {
		t.Fatal("管理凭据未加载")
	}
	if cc.certFile != filepath.Join(dir, "admin.crt") {
		t.Fatalf("相对路径未按凭据文件目录解析: %s", cc.certFile)
	}
	resp, err := cc.client().Get(cc.serverURL() + "/whoami")
	if err != nil {
		t.Fatalf("mTLS 请求失败: %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK || peerCN != "admin" {
		t.Fatalf("期望 200 且服务端收到 CN=admin，实际 status=%d cn=%q body=%s", resp.StatusCode, peerCN, body)
	}
}

// TestCredentialsServerOverride：--server 显式覆盖凭据文件的 server 地址。
func TestCredentialsServerOverride(t *testing.T) {
	c := &credentialsClient{server: "https://127.0.0.1"}
	old := flagServer
	t.Cleanup(func() { flagServer = old })
	flagServer = ""
	if got := c.serverURL(); got != "https://127.0.0.1" {
		t.Fatalf("缺省应取凭据文件 server，实际 %s", got)
	}
	flagServer = "https://cp.example.com/"
	if got := c.serverURL(); got != "https://cp.example.com" {
		t.Fatalf("--server 应覆盖并去尾斜杠，实际 %s", got)
	}
}

// TestCredentialsDiscovery：发现顺序与回退（无配置返回 nil）。
func TestCredentialsDiscovery(t *testing.T) {
	t.Setenv("KURRENT_CREDENTIALS", "")
	stubUserHome(t)
	old := flagCredentials
	flagCredentials = ""
	t.Cleanup(func() { flagCredentials = old })

	dir := t.TempDir()
	t.Chdir(dir)
	if p := discoverCredentials(); p != "" {
		t.Fatalf("空目录不应发现管理凭据: %s", p)
	}
	if c := loadCredentials(); c != nil {
		t.Fatal("无管理凭据时 loadCredentials 应返回 nil（回退 Bearer 通道）")
	}

	def := filepath.Join(dir, "control_plane", "state")
	if err := os.MkdirAll(def, 0o755); err != nil {
		t.Fatal(err)
	}
	conf := filepath.Join(def, "admin.conf")
	writeFile(t, conf, []byte("server: https://127.0.0.1/api/cp\n"))
	if p := discoverCredentials(); p != conf {
		t.Fatalf("默认路径发现失败: %s", p)
	}

	// --credentials 优先于默认路径与 env
	flagCredentials = "/custom/admin.conf"
	if p := discoverCredentials(); p != "/custom/admin.conf" {
		t.Fatalf("--credentials 应优先: %s", p)
	}
}

// TestCredentialsDiscoveryUpward：从仓库子目录调用也能上溯找到凭据。
// 回归：只认 cwd 时在 cli/ 下静默回退成无凭据通道，表现为无从定位的 HTTP 401。
func TestCredentialsDiscoveryUpward(t *testing.T) {
	t.Setenv("KURRENT_CREDENTIALS", "")
	stubUserHome(t)
	old := flagCredentials
	flagCredentials = ""
	t.Cleanup(func() { flagCredentials = old })

	root := t.TempDir()
	def := filepath.Join(root, "control_plane", "state")
	if err := os.MkdirAll(def, 0o755); err != nil {
		t.Fatal(err)
	}
	conf := filepath.Join(def, "admin.conf")
	writeFile(t, conf, []byte(adminConfBody))

	sub := filepath.Join(root, "cli")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(sub)
	if p := discoverCredentials(); p != conf {
		t.Fatalf("子目录应上溯发现 %s，实际 %q", conf, p)
	}
	if loadCredentials() == nil {
		t.Fatal("子目录应走管理凭据（mTLS）通道")
	}
}

// TestCredentialsDiscoveryNearest：多级祖先都放凭据时取最近的一级，不越级。
func TestCredentialsDiscoveryNearest(t *testing.T) {
	t.Setenv("KURRENT_CREDENTIALS", "")
	stubUserHome(t)
	old := flagCredentials
	flagCredentials = ""
	t.Cleanup(func() { flagCredentials = old })

	root := t.TempDir()
	outer := filepath.Join(root, "control_plane", "state")
	inner := filepath.Join(root, "sub", "control_plane", "state")
	for _, d := range []string{outer, inner} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
		writeFile(t, filepath.Join(d, "admin.conf"), []byte(adminConfBody))
	}
	deep := filepath.Join(root, "sub", "cli")
	if err := os.MkdirAll(deep, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(deep)
	if p := discoverCredentials(); p != filepath.Join(inner, "admin.conf") {
		t.Fatalf("应取最近祖先 %s，实际 %q", filepath.Join(inner, "admin.conf"), p)
	}
}

// TestCredentialsDiscoveryExeFallback：cwd 与祖先均无凭据时，按可执行文件旁（仓库布局 cli/kurrent → 仓库根）发现。
func TestCredentialsDiscoveryExeFallback(t *testing.T) {
	t.Setenv("KURRENT_CREDENTIALS", "")
	stubUserHome(t)
	old := flagCredentials
	flagCredentials = ""
	t.Cleanup(func() { flagCredentials = old })
	oldExe := exeDir
	t.Cleanup(func() { exeDir = oldExe })

	root := t.TempDir()
	def := filepath.Join(root, "control_plane", "state")
	if err := os.MkdirAll(def, 0o755); err != nil {
		t.Fatal(err)
	}
	conf := filepath.Join(def, "admin.conf")
	writeFile(t, conf, []byte(adminConfBody))
	exeDir = func() string { return filepath.Join(root, "cli") }

	t.Chdir(t.TempDir()) // cwd 及各级祖先均无凭据
	if p := discoverCredentials(); p != conf {
		t.Fatalf("应按可执行文件旁发现 %s，实际 %q", conf, p)
	}
}

// TestAuthHint：两条凭据通道都空时给出发现提示；有凭据或显式 token 时不干扰（401 归因于凭据本身）。
func TestAuthHint(t *testing.T) {
	t.Setenv("KURRENT_CREDENTIALS", "")
	stubUserHome(t)
	oldCred, oldToken := flagCredentials, flagToken
	flagCredentials, flagToken = "", ""
	t.Cleanup(func() { flagCredentials, flagToken = oldCred, oldToken })
	oldExe := exeDir
	t.Cleanup(func() { exeDir = oldExe })

	empty := t.TempDir()
	t.Chdir(empty)
	exeDir = func() string { return filepath.Join(empty, "cli") }
	hint := authHint()
	if !strings.Contains(hint, "未发现管理凭据") || !strings.Contains(hint, "kurrent credentials install") {
		t.Fatalf("无凭据无 token 应提示发现路径与安装命令，实际 %q", hint)
	}
	flagToken = "1a78c3.2dea22c2a4888435"
	if hint := authHint(); hint != "" {
		t.Fatalf("已给 --token 时不应追加提示，实际 %q", hint)
	}
}

// TestCredentialsInstall：install 复制为自包含快照（相对键 + 溯源 + 0700/0600 权限），
// 仓库外命中快照且证书可加载；整目录拷贝到别处仍可用。
func TestCredentialsInstall(t *testing.T) {
	clearCredentialFlags(t)
	home := stubUserHome(t)
	stubExeDir(t, filepath.Join(t.TempDir(), "cli"))
	repo := t.TempDir()
	conf := makeRepoCredentials(t, repo)
	t.Chdir(repo)

	target, source, err := installCredentials()
	if err != nil {
		t.Fatalf("install: %v", err)
	}
	wantTarget := filepath.Join(home, ".kurrent", "admin.conf")
	if target != wantTarget || source != conf {
		t.Fatalf("install 返回 target=%q source=%q，want %q / %q", target, source, wantTarget, conf)
	}

	snapDir := filepath.Dir(target)
	if fi, err := os.Stat(snapDir); err != nil || fi.Mode().Perm() != 0o700 {
		t.Fatalf("快照目录权限异常（err=%v）", err)
	}
	if fi, err := os.Stat(target); err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("快照 admin.conf 权限异常（err=%v）", err)
	}

	cf := readConf(t, target)
	if cf.Server != "https://127.0.0.1/api/cp" ||
		cf.CA != "pki/server.crt" || cf.ClientCert != "pki/client.crt" || cf.ClientKey != "pki/client.key" {
		t.Fatalf("快照证书键应重写为 pki/<文件名> 相对路径: %+v", cf)
	}
	if cf.InstalledFrom != conf || cf.InstalledAt == "" {
		t.Fatalf("快照缺溯源键: %+v", cf)
	}
	for rel, srcPath := range map[string]string{
		"pki/server.crt": filepath.Join(repo, "control_plane/state/certs/server.crt"),
		"pki/client.crt": filepath.Join(repo, "control_plane/state/pki/users/admin/client.crt"),
		"pki/client.key": filepath.Join(repo, "control_plane/state/pki/users/admin/client.key"),
	} {
		got, err := os.ReadFile(filepath.Join(snapDir, rel))
		if err != nil {
			t.Fatalf("快照缺 %s: %v", rel, err)
		}
		want, _ := os.ReadFile(srcPath)
		if !bytes.Equal(got, want) {
			t.Fatalf("%s 与源内容不一致", rel)
		}
		if fi, _ := os.Stat(filepath.Join(snapDir, rel)); fi.Mode().Perm() != 0o600 {
			t.Fatalf("%s 权限 %v，want 0600", rel, fi.Mode().Perm())
		}
	}

	// 脱离仓库：仓库外命中用户级快照，证书经快照内副本加载
	outside := t.TempDir()
	t.Chdir(outside)
	if p := discoverCredentials(); p != wantTarget {
		t.Fatalf("仓库外应命中用户级快照 %s，实际 %q", wantTarget, p)
	}
	cc := loadCredentials()
	if cc == nil || cc.certFile != filepath.Join(snapDir, "pki", "client.crt") {
		t.Fatalf("快照凭据解析错误: %+v", cc)
	}
	_ = cc.client() // 证书副本可加载（失败会以 fatal 终止测试进程）

	// 自包含：整目录拷贝到别处，凭相对键仍可用
	moved := filepath.Join(t.TempDir(), "admin-creds")
	copyDir(t, snapDir, moved)
	flagCredentials = filepath.Join(moved, "admin.conf")
	cc2 := loadCredentials()
	if cc2 == nil || cc2.certFile != filepath.Join(moved, "pki", "client.crt") {
		t.Fatalf("拷贝后的快照不可用: %+v", cc2)
	}
	_ = cc2.client()
}

// TestCredentialsInstallRefresh：重跑 install 刷新快照（快照被篡改后恢复为源内容）。
func TestCredentialsInstallRefresh(t *testing.T) {
	clearCredentialFlags(t)
	stubUserHome(t)
	stubExeDir(t, filepath.Join(t.TempDir(), "cli"))
	repo := t.TempDir()
	makeRepoCredentials(t, repo)
	t.Chdir(repo)

	target, _, err := installCredentials()
	if err != nil {
		t.Fatal(err)
	}
	copied := filepath.Join(filepath.Dir(target), "pki", "client.key")
	if err := os.WriteFile(copied, []byte("tampered"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := installCredentials(); err != nil {
		t.Fatalf("重跑 install: %v", err)
	}
	got, _ := os.ReadFile(copied)
	want, _ := os.ReadFile(filepath.Join(repo, "control_plane/state/pki/users/admin/client.key"))
	if !bytes.Equal(got, want) {
		t.Fatal("重跑 install 未刷新证书副本")
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
	if _, _, err := installCredentials(); err == nil || !strings.Contains(err.Error(), "用户级快照") {
		t.Fatalf("应拒绝以快照为源，实际 err=%v", err)
	}
}

// TestCredentialsDiscoveryPriority：仓库源（cwd 上溯）优先于用户级快照；仓库外才吃快照。
func TestCredentialsDiscoveryPriority(t *testing.T) {
	clearCredentialFlags(t)
	stubUserHome(t)
	repo := t.TempDir()
	conf := makeRepoCredentials(t, repo)

	snap := userCredentialsPath()
	if err := os.MkdirAll(filepath.Dir(snap), 0o700); err != nil {
		t.Fatal(err)
	}
	writeFile(t, snap, []byte(adminConfBody))

	sub := filepath.Join(repo, "cli")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(sub)
	if p := discoverCredentials(); p != conf {
		t.Fatalf("仓库内应优先实时源 %s，实际 %q", conf, p)
	}

	outside := t.TempDir()
	stubExeDir(t, filepath.Join(outside, "bin"))
	t.Chdir(outside)
	if p := discoverCredentials(); p != snap {
		t.Fatalf("仓库外应命中用户级快照 %s，实际 %q", snap, p)
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

	target, _, err := installCredentials()
	if err != nil {
		t.Fatal(err)
	}
	if err := removeCredentials(); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if fileExists(target) || fileExists(filepath.Join(filepath.Dir(target), "pki")) {
		t.Fatal("快照未删净")
	}
	if !fileExists(conf) {
		t.Fatal("remove 不应动仓库源")
	}

	outside := t.TempDir()
	t.Chdir(outside)
	if p := discoverCredentials(); p != "" {
		t.Fatalf("移除后仓库外不应发现凭据，实际 %q", p)
	}
	if err := removeCredentials(); err == nil || !strings.Contains(err.Error(), "未安装") {
		t.Fatalf("重复 remove 应报未安装，实际 err=%v", err)
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

	out, err := credentialsShow()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, conf) || !strings.Contains(out, "未安装") {
		t.Fatalf("show 应含生效凭据与未安装提示: %s", out)
	}

	if _, _, err := installCredentials(); err != nil {
		t.Fatal(err)
	}
	outside := t.TempDir()
	stubExeDir(t, filepath.Join(outside, "bin"))
	t.Chdir(outside)
	out, err = credentialsShow()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, "用户级快照") || !strings.Contains(out, "installed-from: "+conf) {
		t.Fatalf("show 应含快照来源与溯源: %s", out)
	}
}
