// 管理凭据发现与 mTLS。
//
// CLI 自动发现管理凭据文件（默认 control_plane/state/admin.conf，kurrent 原生扁平
// YAML：server + 三个证书路径键），携带 admin 客户端证书经 443 mTLS 访问控制面：
// nginx 校验客户端证书链（内部 CA）后透传 DN，控制面认证器链据此识别管理身份；
// 服务端证书用凭据文件内 CA（TOFU server.crt）校验。
// 发现顺序：--credentials ＞ KURRENT_CREDENTIALS ＞ 自动发现（cwd 逐级上溯 control_plane/state/
// admin.conf → 可执行文件旁 → 用户级快照 ~/.kurrent/admin.conf，见 discoverCredentials）；
// 未发现时回退 Bearer/明文通道（--server + --token），此时 401 会附带 authHint 指明是
// "没找到凭据"而非控制面拒绝。
// 用户级快照由 kurrent credentials install 复制生成（自包含：admin.conf + pki/ 证书副本），
// 使二进制脱离仓库后（PATH 安装/异地执行）仍可 mTLS；仓库源在场时优先实时源，轮换后重跑
// install 刷新快照（快照是快照：不跟随源，过期靠重新获取）。
package main

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// credentialsFile：管理凭据文件结构（kurrent 原生扁平 YAML，四键；证书路径相对本文件目录）。
// installed-from/installed-at 仅用户级快照写入（kurrent credentials install 溯源 + 陈旧判断）。
type credentialsFile struct {
	Server        string `yaml:"server"`
	CA            string `yaml:"certificate-authority"`
	ClientCert    string `yaml:"client-certificate"`
	ClientKey     string `yaml:"client-key"`
	InstalledFrom string `yaml:"installed-from,omitempty"`
	InstalledAt   string `yaml:"installed-at,omitempty"`
}

type credentialsClient struct {
	path     string
	server   string
	caFile   string
	certFile string
	keyFile  string
}

// exeDir 返回可执行文件所在目录（软链已解析）；变量化以便测试注入。
var exeDir = func() string {
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		exe = resolved
	}
	return filepath.Dir(exe)
}

// userHomeDir 返回用户主目录；变量化以便测试注入。
var userHomeDir = func() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return home
}

// userCredentialsDir 用户级凭据目录（~/.kurrent）；主目录不可知时返回 ""。
func userCredentialsDir() string {
	if home := userHomeDir(); home != "" {
		return filepath.Join(home, ".kurrent")
	}
	return ""
}

// userCredentialsPath 用户级快照路径（~/.kurrent/admin.conf）；主目录不可知时返回 ""。
func userCredentialsPath() string {
	if dir := userCredentialsDir(); dir != "" {
		return filepath.Join(dir, "admin.conf")
	}
	return ""
}

// fileExists 判断路径存在且为普通文件。
func fileExists(path string) bool {
	fi, err := os.Stat(path)
	return err == nil && !fi.IsDir()
}

// discoverCredentials 按 --credentials ＞ KURRENT_CREDENTIALS ＞ 自动发现发现凭据文件。
//
// 自动发现分三步（只认 cwd 会导致从仓库子目录 cli/ 调用时静默回退成无凭据通道，
// 表现为难以定位的 HTTP 401）：
//  1. 从 cwd 逐级上溯 control_plane/state/admin.conf（git 式，命中最近的祖先）；
//  2. 退回可执行文件旁 <exe 目录>/../control_plane/state/admin.conf
//     （仓库布局 cli/kurrent → 仓库根；覆盖从仓库外调用该二进制的情形）；
//  3. 垫底用户级快照 ~/.kurrent/admin.conf（kurrent credentials install 产物）——
//     仓库源在场时优先实时源（不经快照、不受陈旧影响），脱离仓库才吃快照。
//
// 均不存在返回 ""（回退 Bearer/明文通道）；命中返回绝对路径（错误提示据此指明实际使用的凭据）。
func discoverCredentials() string {
	if flagCredentials != "" {
		return flagCredentials
	}
	if env := os.Getenv("KURRENT_CREDENTIALS"); env != "" {
		return env
	}
	rel := filepath.Join("control_plane", "state", "admin.conf")
	if wd, err := os.Getwd(); err == nil {
		for dir := wd; ; {
			if candidate := filepath.Join(dir, rel); fileExists(candidate) {
				return candidate
			}
			parent := filepath.Dir(dir)
			if parent == dir { // 已到文件系统根
				break
			}
			dir = parent
		}
	}
	if dir := exeDir(); dir != "" {
		if candidate := filepath.Join(filepath.Dir(dir), rel); fileExists(candidate) {
			return candidate
		}
	}
	if candidate := userCredentialsPath(); candidate != "" && fileExists(candidate) {
		return candidate
	}
	return ""
}

// authHint 认证失败且两条凭据通道都空时的排查提示：此类失败最易误判为控制面问题，
// 实际是未发现 admin.conf、静默退回无凭据通道（见 discoverCredentials 注释）。
// 已发现凭据或已显式给 --token 时返回 ""（此时 401 是凭据本身的问题，不是发现的问题）。
func authHint() string {
	if flagToken != "" || loadCredentials() != nil {
		return ""
	}
	server := strings.TrimRight(flagServer, "/")
	if server == "" {
		server = "(--server not specified)"
	}
	return fmt.Sprintf("\n  No management credentials found (checked control_plane/state/admin.conf by walking up from the cwd, next to the executable, and ~/.kurrent/admin.conf), and no --token was given;\n"+
		"  this request uses the credential-less channel against %s. Fix: kurrent credentials install (one-time inside the repository, then works anywhere) | --credentials <admin.conf path> | --server + --token (the management API no longer listens on a local loopback port)",
		server)
}

// loadCredentials 解析管理凭据文件（证书路径相对文件所在目录）；不可用时返回 nil。
func loadCredentials() *credentialsClient {
	path := discoverCredentials()
	if path == "" {
		return nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		fatal("read management credentials %s: %v (pass --credentials with another path, or omit it to fall back to --server/--token)", path, err)
	}
	var cf credentialsFile
	if err := yaml.Unmarshal(data, &cf); err != nil {
		fatal("parse management credentials %s: %v", path, err)
	}
	if cf.Server == "" || cf.ClientCert == "" || cf.ClientKey == "" {
		fatal("management credentials %s are missing server/client-certificate/client-key", path)
	}
	c := &credentialsClient{
		path:     path,
		server:   cf.Server,
		caFile:   cf.CA,
		certFile: cf.ClientCert,
		keyFile:  cf.ClientKey,
	}
	dir := filepath.Dir(path)
	abs := func(p string) string {
		if p == "" || filepath.IsAbs(p) {
			return p
		}
		return filepath.Join(dir, p)
	}
	c.caFile = abs(c.caFile)
	c.certFile = abs(c.certFile)
	c.keyFile = abs(c.keyFile)
	return c
}

// client 构建 mTLS http 客户端：客户端证书（admin 身份）+ CA 校验服务端（TOFU 锚）。
func (c *credentialsClient) client() *http.Client {
	cert, err := tls.LoadX509KeyPair(c.certFile, c.keyFile)
	if err != nil {
		fatal("load management certificate %s: %v", c.certFile, err)
	}
	tlsCfg := &tls.Config{Certificates: []tls.Certificate{cert}}
	if c.caFile != "" {
		caPEM, err := os.ReadFile(c.caFile)
		if err != nil {
			fatal("read management CA %s: %v", c.caFile, err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(caPEM) {
			fatal("management CA %s contains no valid certificate", c.caFile)
		}
		tlsCfg.RootCAs = pool
	}
	return &http.Client{
		Timeout:   15 * time.Second,
		Transport: &http.Transport{TLSClientConfig: tlsCfg},
	}
}

// serverURL：凭据文件的 server（nginx 唯一入口 API 根，含 /api/cp 前缀）；
// --server 显式指定时覆盖（远程管理员指向 https://<host>/api/cp）。
// 凭据文件是外部输入，地址在这里统一校验（缺 host/坏 scheme 直接报错，不发出请求）。
func (c *credentialsClient) serverURL() (string, error) {
	server := c.server
	if flagServer != "" {
		server = flagServer
	}
	return validateHTTPURL("management credential server (or --server)", server)
}

// credentialsHint 返回当前发现的管理凭据路径（连接失败提示用）；未启用返回 ""。
func credentialsHint() string {
	if path := discoverCredentials(); path != "" {
		return fmt.Sprintf(" (management credentials: %s; --server overrides the server address)", path)
	}
	return ""
}
