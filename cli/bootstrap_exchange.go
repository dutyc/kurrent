// bootstrap_exchange.go：首次部署的管理凭据交换（一次性、可校验）。
//
// 控制面在容器内生成 admin.conf 后，按 <state>/control-plane/bootstrap/request
// 的存在把凭据与证书复制到交换目录，重写为相对路径并写 ready（含 SHA-256）
// 后删除 request。CLI 校验哈希、证书/私钥配对与 server 地址，安装到
// ~/.kurrent 后用安装副本做一次 mTLS GET /whoami，全部通过才删除交换包。
package main

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

const (
	exchangeRequest  = "request"
	exchangeReady    = "ready"
	exchangeConsumed = "consumed"
	exchangeAdmin    = "admin.conf"
	exchangePKI      = "pki"
)

// exchangeReadyFile 控制面发布的就绪清单（写 ready 先于删除 request）。
type exchangeReadyFile struct {
	CreatedAt string            `json:"createdAt"`
	Files     []string          `json:"files"`
	SHA256    map[string]string `json:"sha256"`
}

// bootstrapBundle 校验通过、可安装的交换包。
type bootstrapBundle struct {
	Dir       string
	AdminConf string
	Server    string
	CA        string
	Cert      string
	Key       string
}

// requestBootstrapExchange 清掉上一次交换残留并写入本次请求。
func requestBootstrapExchange(bootstrapDir string) error {
	if err := ensurePrivateDir(bootstrapDir); err != nil {
		return err
	}
	for _, name := range []string{exchangeReady, exchangeConsumed, exchangeAdmin, exchangePKI} {
		if err := os.RemoveAll(filepath.Join(bootstrapDir, name)); err != nil {
			return fmt.Errorf("clear stale bootstrap exchange %s: %w", name, err)
		}
	}
	body := fmt.Sprintf("requested-at: %s\n", time.Now().UTC().Format(time.RFC3339))
	if err := writeFileAtomic(filepath.Join(bootstrapDir, exchangeRequest), []byte(body), 0o600); err != nil {
		return fmt.Errorf("write bootstrap request: %w", err)
	}
	return nil
}

// waitBootstrapExchange 轮询 ready；超时返回错误（保留交换包供排查/重跑）。
func waitBootstrapExchange(dir string, timeout time.Duration) error {
	ready := filepath.Join(dir, exchangeReady)
	deadline := time.Now().Add(timeout)
	for {
		if fi, err := os.Stat(ready); err == nil && fi.Mode().IsRegular() {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("the control plane did not publish management credentials within %s (looked for %s); inspect docker logs kurrent-control-plane", timeout, ready)
		}
		time.Sleep(500 * time.Millisecond)
	}
}

// validateExchangeBundle 校验交换包：哈希、路径不越界、证书/私钥配对、CA 可解析、server 合法。
func validateExchangeBundle(dir string) (*bootstrapBundle, error) {
	raw, err := os.ReadFile(filepath.Join(dir, exchangeReady))
	if err != nil {
		return nil, fmt.Errorf("read bootstrap ready file: %w", err)
	}
	var ready exchangeReadyFile
	if err := json.Unmarshal(raw, &ready); err != nil {
		return nil, fmt.Errorf("parse bootstrap ready file: %w", err)
	}
	if len(ready.Files) == 0 || len(ready.SHA256) == 0 {
		return nil, fmt.Errorf("bootstrap ready file lists no files")
	}
	for name := range ready.SHA256 {
		rel := filepath.Clean(name)
		if filepath.IsAbs(rel) || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
			return nil, fmt.Errorf("bootstrap bundle path escapes its directory: %s", name)
		}
		path := filepath.Join(dir, rel)
		fi, err := os.Lstat(path)
		if err != nil {
			return nil, fmt.Errorf("bootstrap bundle is missing %s: %w", name, err)
		}
		if !fi.Mode().IsRegular() {
			return nil, fmt.Errorf("bootstrap bundle entry is not a regular file: %s", name)
		}
		actual, err := hashFile(path)
		if err != nil {
			return nil, fmt.Errorf("hash %s: %w", name, err)
		}
		if actual != ready.SHA256[name] {
			return nil, fmt.Errorf("bootstrap bundle hash mismatch for %s (expected %s, got %s)", name, ready.SHA256[name], actual)
		}
	}

	adminConf := filepath.Join(dir, exchangeAdmin)
	data, err := os.ReadFile(adminConf)
	if err != nil {
		return nil, fmt.Errorf("read exchanged admin.conf: %w", err)
	}
	var cf credentialsFile
	if err := yaml.Unmarshal(data, &cf); err != nil {
		return nil, fmt.Errorf("parse exchanged admin.conf: %w", err)
	}
	if cf.Server == "" || cf.CA == "" || cf.ClientCert == "" || cf.ClientKey == "" {
		return nil, fmt.Errorf("exchanged admin.conf is missing server/certificate-authority/client-certificate/client-key")
	}
	server, err := validateHTTPURL("exchanged admin server", cf.Server)
	if err != nil {
		return nil, err
	}
	parsed, err := url.Parse(server)
	if err != nil || parsed.Scheme != "https" {
		return nil, fmt.Errorf("exchanged admin server must be an https URL: %s", cf.Server)
	}
	abs := func(p string) (string, error) {
		rel := filepath.Clean(p)
		if filepath.IsAbs(rel) || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
			return "", fmt.Errorf("exchanged admin.conf path escapes the bundle: %s", p)
		}
		return filepath.Join(dir, rel), nil
	}
	caPath, err := abs(cf.CA)
	if err != nil {
		return nil, err
	}
	certPath, err := abs(cf.ClientCert)
	if err != nil {
		return nil, err
	}
	keyPath, err := abs(cf.ClientKey)
	if err != nil {
		return nil, err
	}
	if _, err := tls.LoadX509KeyPair(certPath, keyPath); err != nil {
		return nil, fmt.Errorf("exchanged management certificate/private key unusable: %w", err)
	}
	caPEM, err := os.ReadFile(caPath)
	if err != nil {
		return nil, fmt.Errorf("read exchanged management CA: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("exchanged management CA contains no valid certificate")
	}
	return &bootstrapBundle{
		Dir: dir, AdminConf: adminConf, Server: server,
		CA: caPath, Cert: certPath, Key: keyPath,
	}, nil
}

// finishBootstrapExchange 标记已消费并删除凭据副本（只留 consumed 审计标记）。
func finishBootstrapExchange(dir string) error {
	marker := fmt.Sprintf("consumed-at: %s\n", time.Now().UTC().Format(time.RFC3339))
	if err := writeFileAtomic(filepath.Join(dir, exchangeConsumed), []byte(marker), 0o600); err != nil {
		return fmt.Errorf("write bootstrap consumed marker: %w", err)
	}
	for _, name := range []string{exchangeRequest, exchangeReady, exchangeAdmin, exchangePKI} {
		if err := os.RemoveAll(filepath.Join(dir, name)); err != nil {
			return fmt.Errorf("clean bootstrap exchange %s: %w", name, err)
		}
	}
	return nil
}

// managementSelfTest 用给定凭据做一次 mTLS GET /whoami；有限重试覆盖容器刚重启的窗口。
func managementSelfTest(server, ca, cert, key string, timeout time.Duration) error {
	certificate, err := tls.LoadX509KeyPair(cert, key)
	if err != nil {
		return fmt.Errorf("load management certificate: %w", err)
	}
	caPEM, err := os.ReadFile(ca)
	if err != nil {
		return fmt.Errorf("read management CA: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return fmt.Errorf("management CA contains no valid certificate")
	}
	client := &http.Client{
		Timeout: 10 * time.Second,
		Transport: &http.Transport{TLSClientConfig: &tls.Config{
			Certificates: []tls.Certificate{certificate},
			RootCAs:      pool,
		}},
	}
	endpoint := strings.TrimRight(server, "/") + "/whoami"
	deadline := time.Now().Add(timeout)
	var last error
	for {
		resp, err := client.Get(endpoint)
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return nil
			}
			last = fmt.Errorf("GET %s returned HTTP %d", endpoint, resp.StatusCode)
		} else {
			last = err
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("management credential self-test failed: %v", last)
		}
		time.Sleep(1 * time.Second)
	}
}
