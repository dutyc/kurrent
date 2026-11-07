// 引导交换的真实机制测试：就绪清单/哈希、路径逃逸、消费清理与 mTLS 自检。
// 编排类测试（init/join）通过 stubBootstrapExchange 注入固定交换包，避免每个用例
// 都生成证书；这里直接调用真实函数，覆盖交换包的可信性边界。
package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type testCA struct {
	cert *x509.Certificate
	key  *ecdsa.PrivateKey
	pem  []byte
}

func newTestCA(t *testing.T, cn string) *testCA {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tpl := &x509.Certificate{
		SerialNumber:          big.NewInt(time.Now().UnixNano()),
		Subject:               pkix.Name{CommonName: cn},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
	}
	der, err := x509.CreateCertificate(rand.Reader, tpl, tpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return &testCA{cert: cert, key: key, pem: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})}
}

// issue 签发叶子证书；server=true 时带 127.0.0.1 SAN 与 serverAuth。
func (ca *testCA) issue(t *testing.T, cn string, server bool) ([]byte, []byte) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()),
		Subject:      pkix.Name{CommonName: cn},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
	}
	if server {
		tpl.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
		tpl.IPAddresses = []net.IP{net.ParseIP("127.0.0.1")}
	} else {
		tpl.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
	}
	der, err := x509.CreateCertificate(rand.Reader, tpl, ca.cert, &key.PublicKey, ca.key)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
	return certPEM, keyPEM
}

// writeSelfSignedPEM 写一张自签 CA 证书（信任锚占位）。
func writeSelfSignedPEM(t *testing.T, path, cn string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, newTestCA(t, cn).pem, 0o644); err != nil {
		t.Fatal(err)
	}
}

// writeTestBundle 构造一份通过校验的交换包（ca + client cert/key + admin.conf + ready）。
func writeTestBundle(t *testing.T, dir string) error {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, "pki"), 0o700); err != nil {
		return err
	}
	ca := newTestCA(t, "bootstrap-ca")
	certPEM, keyPEM := ca.issue(t, "admin", false)
	files := map[string][]byte{
		"pki/server.crt": ca.pem,
		"pki/client.crt": certPEM,
		"pki/client.key": keyPEM,
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), body, 0o600); err != nil {
			return err
		}
	}
	admin := "server: https://127.0.0.1/api/cp\n" +
		"certificate-authority: pki/server.crt\n" +
		"client-certificate: pki/client.crt\n" +
		"client-key: pki/client.key\n"
	if err := os.WriteFile(filepath.Join(dir, exchangeAdmin), []byte(admin), 0o600); err != nil {
		return err
	}
	ready := exchangeReadyFile{
		CreatedAt: time.Now().UTC().Format(time.RFC3339),
		Files:     []string{exchangeAdmin, "pki/server.crt", "pki/client.crt", "pki/client.key"},
		SHA256:    map[string]string{},
	}
	for _, name := range ready.Files {
		sum, err := hashFile(filepath.Join(dir, name))
		if err != nil {
			return err
		}
		ready.SHA256[name] = sum
	}
	body, err := json.MarshalIndent(ready, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, exchangeReady), append(body, '\n'), 0o600)
}

// stubBootstrapExchange 让 init 编排测试走固定交换包：wait 生成包，自检视为通过。
// 真实校验/自检由本文件的独立用例覆盖。
func stubBootstrapExchange(t *testing.T) {
	t.Helper()
	oldWait, oldVerify := waitExchange, verifyCredentials
	waitExchange = func(dir string, _ time.Duration) error { return writeTestBundle(t, dir) }
	verifyCredentials = func(string, string, string, string, time.Duration) error { return nil }
	t.Cleanup(func() { waitExchange, verifyCredentials = oldWait, oldVerify })
}

func TestValidateExchangeBundle(t *testing.T) {
	dir := t.TempDir()
	if err := writeTestBundle(t, dir); err != nil {
		t.Fatal(err)
	}
	bundle, err := validateExchangeBundle(dir)
	if err != nil {
		t.Fatal(err)
	}
	if bundle.Server != "https://127.0.0.1/api/cp" {
		t.Errorf("server = %q", bundle.Server)
	}

	// 篡改 admin.conf → 哈希不匹配
	if err := os.WriteFile(filepath.Join(dir, exchangeAdmin), []byte("server: https://evil/api/cp\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := validateExchangeBundle(dir); err == nil || !strings.Contains(err.Error(), "hash mismatch") {
		t.Fatalf("err = %v, want hash mismatch", err)
	}
}

func TestValidateExchangeBundleRejectsPathEscape(t *testing.T) {
	dir := t.TempDir()
	if err := writeTestBundle(t, dir); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(dir, exchangeReady))
	if err != nil {
		t.Fatal(err)
	}
	var ready exchangeReadyFile
	if err := json.Unmarshal(raw, &ready); err != nil {
		t.Fatal(err)
	}
	ready.SHA256["../escape.crt"] = strings.Repeat("0", 64)
	body, _ := json.Marshal(ready)
	if err := os.WriteFile(filepath.Join(dir, exchangeReady), body, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := validateExchangeBundle(dir); err == nil || !strings.Contains(err.Error(), "escapes") {
		t.Fatalf("err = %v, want path escape rejection", err)
	}
}

func TestWaitBootstrapExchangeTimeout(t *testing.T) {
	err := waitBootstrapExchange(t.TempDir(), 50*time.Millisecond)
	if err == nil || !strings.Contains(err.Error(), "did not publish") {
		t.Fatalf("err = %v, want timeout", err)
	}
}

func TestFinishBootstrapExchange(t *testing.T) {
	dir := t.TempDir()
	if err := writeTestBundle(t, dir); err != nil {
		t.Fatal(err)
	}
	if err := requestBootstrapExchange(dir); err != nil {
		t.Fatal(err)
	}
	if err := finishBootstrapExchange(dir); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{exchangeRequest, exchangeReady, exchangeAdmin, exchangePKI} {
		if _, err := os.Stat(filepath.Join(dir, name)); !os.IsNotExist(err) {
			t.Errorf("%s should be removed, stat err = %v", name, err)
		}
	}
	if _, err := os.Stat(filepath.Join(dir, exchangeConsumed)); err != nil {
		t.Errorf("consumed marker missing: %v", err)
	}
}

func TestRequestBootstrapExchangeClearsStale(t *testing.T) {
	dir := t.TempDir()
	if err := writeTestBundle(t, dir); err != nil {
		t.Fatal(err)
	}
	if err := requestBootstrapExchange(dir); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{exchangeReady, exchangeAdmin, exchangePKI} {
		if _, err := os.Stat(filepath.Join(dir, name)); !os.IsNotExist(err) {
			t.Errorf("stale %s should be cleared", name)
		}
	}
	if _, err := os.Stat(filepath.Join(dir, exchangeRequest)); err != nil {
		t.Errorf("request should exist: %v", err)
	}
}

func TestManagementSelfTest(t *testing.T) {
	ca := newTestCA(t, "selftest-ca")
	serverCertPEM, serverKeyPEM := ca.issue(t, "server", true)
	clientCertPEM, clientKeyPEM := ca.issue(t, "admin", false)

	clientCA := x509.NewCertPool()
	clientCA.AppendCertsFromPEM(ca.pem)
	serverCert, err := tls.X509KeyPair(serverCertPEM, serverKeyPEM)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/whoami" || r.TLS == nil || len(r.TLS.PeerCertificates) == 0 {
			http.Error(w, "unauthenticated", http.StatusUnauthorized)
			return
		}
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"principal":"admin"}`))
	}))
	server.TLS = &tls.Config{Certificates: []tls.Certificate{serverCert}, ClientAuth: tls.RequireAndVerifyClientCert, ClientCAs: clientCA}
	server.StartTLS()
	defer server.Close()

	dir := t.TempDir()
	caPath := filepath.Join(dir, "ca.crt")
	certPath := filepath.Join(dir, "client.crt")
	keyPath := filepath.Join(dir, "client.key")
	for path, body := range map[string][]byte{caPath: ca.pem, certPath: clientCertPEM, keyPath: clientKeyPEM} {
		if err := os.WriteFile(path, body, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := managementSelfTest(server.URL, caPath, certPath, keyPath, 2*time.Second); err != nil {
		t.Fatalf("self-test should pass: %v", err)
	}

	// 错误信任锚 → 失败（证明自检真的校验服务端证书）
	otherCA := filepath.Join(dir, "other-ca.crt")
	writeSelfSignedPEM(t, otherCA, "other-ca")
	if err := managementSelfTest(server.URL, otherCA, certPath, keyPath, 200*time.Millisecond); err == nil {
		t.Fatal("self-test should fail with the wrong CA")
	}
}
