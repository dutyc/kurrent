// 管理凭据发现与 mTLS：发现顺序、地址覆盖、401 归因提示。
package creds

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"kurrent/cli/internal/cli"
	"kurrent/cli/internal/fsx"
)

func writeFile(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
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

func stubExeDir(t *testing.T, dir string) {
	t.Helper()
	old := fsx.ExeDir
	fsx.ExeDir = func() string { return dir }
	t.Cleanup(func() { fsx.ExeDir = old })
}

func clearCredentialFlags(t *testing.T) {
	t.Helper()
	t.Setenv("KURRENT_CREDENTIALS", "")
	oldCred, oldServer, oldToken := cli.Credentials, cli.Server, cli.Token
	cli.Credentials, cli.Server, cli.Token = "", "", ""
	t.Cleanup(func() { cli.Credentials, cli.Server, cli.Token = oldCred, oldServer, oldToken })
}

// adminConfBody 完整的管理凭据文件内容（三键齐备，避免 LoadCredentials 因缺键 fatal）；
// 证书路径只做发现/解析用，测试不实际加载证书，故指向不存在的相对路径即可。
const adminConfBody = "server: https://127.0.0.1/api/cp\n" +
	"client-certificate: admin.crt\nclient-key: admin.key\n"

// TestCredentialsMTLSRequest：admin.conf（扁平 YAML，相对路径）→ mTLS 请求，服务端验证客户端证书 CN。
func TestCredentialsMTLSRequest(t *testing.T) {
	ca := newTestCA(t, "bootstrap-ca")
	clientCertPEM, clientKeyPEM := ca.issue(t, "admin", false)
	clientPool := x509.NewCertPool()
	if !clientPool.AppendCertsFromPEM(ca.pem) {
		t.Fatal("failed to add the CA to the client pool")
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

	old := cli.Credentials
	cli.Credentials = confPath
	t.Cleanup(func() { cli.Credentials = old })

	cc := LoadCredentials()
	if cc == nil {
		t.Fatal("management credentials were not loaded")
	}
	if cc.CertFile != filepath.Join(dir, "admin.crt") {
		t.Fatalf("relative path not resolved against the credential file: %s", cc.CertFile)
	}
	serverURL, err := cc.ServerURL()
	if err != nil {
		t.Fatalf("credential server URL: %v", err)
	}
	resp, err := cc.Client().Get(serverURL + "/whoami")
	if err != nil {
		t.Fatalf("mTLS request failed: %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK || peerCN != "admin" {
		t.Fatalf("want 200 with CN=admin, got status=%d cn=%q body=%s", resp.StatusCode, peerCN, body)
	}
}

// TestCredentialsServerOverride：--server 显式覆盖凭据文件的 server 地址。
func TestCredentialsServerOverride(t *testing.T) {
	c := &CredentialsClient{Server: "https://127.0.0.1"}
	old := cli.Server
	t.Cleanup(func() { cli.Server = old })
	cli.Server = ""
	if got, err := c.ServerURL(); err != nil || got != "https://127.0.0.1" {
		t.Fatalf("default should use the credential server, got %s (err=%v)", got, err)
	}
	cli.Server = "https://cp.example.com/"
	if got, err := c.ServerURL(); err != nil || got != "https://cp.example.com" {
		t.Fatalf("--server should override and trim the trailing slash, got %s (err=%v)", got, err)
	}
}

func TestCredentialsDiscovery(t *testing.T) {
	clearCredentialFlags(t)
	stubUserHome(t)

	dir := t.TempDir()
	t.Chdir(dir)
	if p := DiscoverCredentials(); p != "" {
		t.Fatalf("empty directory must not discover credentials: %s", p)
	}
	if c := LoadCredentials(); c != nil {
		t.Fatal("LoadCredentials should return nil without credentials (Bearer fallback)")
	}

	def := filepath.Join(dir, "control_plane", "state")
	conf := filepath.Join(def, "admin.conf")
	writeFile(t, conf, []byte("server: https://127.0.0.1/api/cp\n"))
	if p := DiscoverCredentials(); p != conf {
		t.Fatalf("default path discovery failed: %s", p)
	}

	cli.Credentials = "/custom/admin.conf"
	if p := DiscoverCredentials(); p != "/custom/admin.conf" {
		t.Fatalf("--credentials must win: %s", p)
	}
}

// TestCredentialsDiscoveryUpward：从仓库子目录调用也能上溯找到凭据。
func TestCredentialsDiscoveryUpward(t *testing.T) {
	clearCredentialFlags(t)
	stubUserHome(t)

	root := t.TempDir()
	def := filepath.Join(root, "control_plane", "state")
	conf := filepath.Join(def, "admin.conf")
	writeFile(t, conf, []byte(adminConfBody))

	sub := filepath.Join(root, "cli")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(sub)
	if p := DiscoverCredentials(); p != conf {
		t.Fatalf("subdirectory should walk up to %s, got %q", conf, p)
	}
	if LoadCredentials() == nil {
		t.Fatal("subdirectory should use the mTLS credential channel")
	}
}

// TestCredentialsDiscoveryNearest：多级祖先都放凭据时取最近的一级，不越级。
func TestCredentialsDiscoveryNearest(t *testing.T) {
	clearCredentialFlags(t)
	stubUserHome(t)

	root := t.TempDir()
	outer := filepath.Join(root, "control_plane", "state")
	inner := filepath.Join(root, "sub", "control_plane", "state")
	for _, d := range []string{outer, inner} {
		writeFile(t, filepath.Join(d, "admin.conf"), []byte(adminConfBody))
	}
	deep := filepath.Join(root, "sub", "cli")
	if err := os.MkdirAll(deep, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(deep)
	if p := DiscoverCredentials(); p != filepath.Join(inner, "admin.conf") {
		t.Fatalf("should pick the nearest ancestor, got %q", p)
	}
}

// TestCredentialsDiscoveryExeFallback：cwd 与祖先均无凭据时，按可执行文件旁发现。
func TestCredentialsDiscoveryExeFallback(t *testing.T) {
	clearCredentialFlags(t)
	stubUserHome(t)

	root := t.TempDir()
	def := filepath.Join(root, "control_plane", "state")
	conf := filepath.Join(def, "admin.conf")
	writeFile(t, conf, []byte(adminConfBody))
	stubExeDir(t, filepath.Join(root, "cli"))

	t.Chdir(t.TempDir())
	if p := DiscoverCredentials(); p != conf {
		t.Fatalf("should discover %s next to the executable, got %q", conf, p)
	}
}

// TestAuthHint：两条凭据通道都空时给出发现提示；有凭据或显式 token 时不干扰。
func TestAuthHint(t *testing.T) {
	clearCredentialFlags(t)
	stubUserHome(t)

	empty := t.TempDir()
	t.Chdir(empty)
	stubExeDir(t, filepath.Join(empty, "cli"))
	hint := AuthHint()
	if !strings.Contains(hint, "No management credentials found") || !strings.Contains(hint, "kurrent credentials install") {
		t.Fatalf("missing credentials should hint the discovery path and install command, got %q", hint)
	}
	cli.Token = "1a78c3.2dea22c2a4888435"
	if hint := AuthHint(); hint != "" {
		t.Fatalf("--token given: no hint expected, got %q", hint)
	}
}

// TestCredentialsDiscoveryPriority：显式 --credentials ＞ 环境变量 ＞ 部署记录。
func TestCredentialsDiscoveryPriority(t *testing.T) {
	clearCredentialFlags(t)
	stubUserHome(t)
	dir := t.TempDir()
	t.Chdir(dir)
	stubExeDir(t, filepath.Join(dir, "cli"))

	env := filepath.Join(dir, "env.conf")
	writeFile(t, env, []byte(adminConfBody))
	t.Setenv("KURRENT_CREDENTIALS", env)
	if p := DiscoverCredentials(); p != env {
		t.Fatalf("env var should win over records: %q", p)
	}
	explicit := filepath.Join(dir, "explicit.conf")
	writeFile(t, explicit, []byte(adminConfBody))
	cli.Credentials = explicit
	if p := DiscoverCredentials(); p != explicit {
		t.Fatalf("--credentials should win over env: %q", p)
	}
}
