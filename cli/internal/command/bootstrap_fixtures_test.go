// init 编排测试的交换包替身：生成通过校验的固定凭据包；
// 真实交换/校验/自检由 internal/creds 的独立用例覆盖。
package command

import (
	"encoding/json"
	"kurrent/cli/internal/fsx"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func writeTestBundle(t *testing.T, dir string) error {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, "pki"), 0o700); err != nil {
		return err
	}
	caPEM, _ := selfSignedCert(t, "bootstrap-ca", nil)
	certPEM, keyPEM := selfSignedCert(t, "admin", nil)
	files := map[string][]byte{
		"pki/server.crt": caPEM,
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
	if err := os.WriteFile(filepath.Join(dir, "admin.conf"), []byte(admin), 0o600); err != nil {
		return err
	}
	ready := map[string]any{
		"createdAt": time.Now().UTC().Format(time.RFC3339),
		"files":     []string{"admin.conf", "pki/server.crt", "pki/client.crt", "pki/client.key"},
		"sha256":    map[string]string{},
	}
	hashes := ready["sha256"].(map[string]string)
	for _, name := range ready["files"].([]string) {
		sum, err := fsx.HashFile(filepath.Join(dir, name))
		if err != nil {
			return err
		}
		hashes[name] = sum
	}
	body, err := json.MarshalIndent(ready, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, "ready"), append(body, '\n'), 0o600)
}

// stubBootstrapExchange 让 init 编排测试走固定交换包：wait 生成包，自检视为通过。
func stubBootstrapExchange(t *testing.T) {
	t.Helper()
	oldWait, oldVerify := waitExchange, verifyCredentials
	waitExchange = func(dir string, _ time.Duration) error { return writeTestBundle(t, dir) }
	verifyCredentials = func(string, string, string, string, time.Duration) error { return nil }
	t.Cleanup(func() { waitExchange, verifyCredentials = oldWait, oldVerify })
}

// writeSelfSignedPEM 写一张自签 CA 证书（信任锚占位）。
func writeSelfSignedPEM(t *testing.T, path, cn string) {
	t.Helper()
	caPEM, _ := selfSignedCert(t, cn, nil)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, caPEM, 0o644); err != nil {
		t.Fatal(err)
	}
}
