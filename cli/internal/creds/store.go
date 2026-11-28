// 管理凭据发现与 mTLS。
//
// CLI 自动发现管理凭据文件（部署记录指向的 <state>/control-plane/state/admin.conf，
// kurrent 原生扁平 YAML：server + 三个证书路径键），携带 admin 客户端证书经 443 mTLS 访问控制面：
// nginx 校验客户端证书链（内部 CA）后透传 DN，控制面认证器链据此识别管理身份；
// 服务端证书用凭据文件内 CA（TOFU server.crt）校验。
// 发现顺序：--credentials ＞ KURRENT_CREDENTIALS ＞ 部署记录指向的实时 admin.conf ＞
// cwd 逐级上溯/可执行文件旁的旧布局 → 用户级快照 ~/.kurrent/admin.conf（见 DiscoverCredentials）；
// 未发现时回退 Bearer/明文通道（--server + --token），此时 401 会附带 AuthHint 指明是
// "没找到凭据"而非控制面拒绝。
// 用户级快照由 kurrent credentials install 复制生成（自包含：admin.conf + pki/ 证书副本），
// 使二进制脱离仓库后（PATH 安装/异地执行）仍可 mTLS；仓库源在场时优先实时源，轮换后重跑
// install 刷新快照（快照是快照：不跟随源，过期靠重新获取）。
package creds

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"kurrent/cli/internal/cli"
	"kurrent/cli/internal/deploy"
	"kurrent/cli/internal/fsx"
	"kurrent/cli/internal/validate"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// CredentialsFile：管理凭据文件结构（kurrent 原生扁平 YAML，四键；证书路径相对本文件目录）。
// installed-from/installed-at 仅用户级快照写入（kurrent credentials install 溯源 + 陈旧判断）。
type CredentialsFile struct {
	Server        string `yaml:"server"`
	CA            string `yaml:"certificate-authority"`
	ClientCert    string `yaml:"client-certificate"`
	ClientKey     string `yaml:"client-key"`
	InstalledFrom string `yaml:"installed-from,omitempty"`
	InstalledAt   string `yaml:"installed-at,omitempty"`
}

type CredentialsClient struct {
	Path     string
	Server   string
	CAFile   string
	CertFile string
	KeyFile  string
}

// RecordCredentialsPath 部署记录指向的实时管理凭据（<state>/control-plane/state/admin.conf）；
// 记录缺失/非法时返回 ""（发现链继续走旧布局与用户快照）。
func RecordCredentialsPath() string {
	rec, err := deploy.ReadDeploymentRecord()
	if err != nil || rec == nil {
		return ""
	}
	candidate := deploy.NewControlPlanePaths(rec.StateDir).AdminConf
	if fsx.FileExists(candidate) {
		return candidate
	}
	return ""
}

// DiscoverCredentials 按 --credentials ＞ KURRENT_CREDENTIALS ＞ 部署记录 ＞ 自动发现发现凭据文件。
//
// 自动发现顺序（只认 cwd 会导致从仓库子目录 cli/ 调用时静默回退成无凭据通道，
// 表现为难以定位的 HTTP 401）：
//  1. 部署记录指向的实时 admin.conf（新布局的权威位置）；
//  2. 从 cwd 逐级上溯 control_plane/state/admin.conf（git 式，兼容未迁移的旧布局）；
//  3. 可执行文件旁 <exe 目录>/../control_plane/state/admin.conf；
//  4. 垫底用户级快照 ~/.kurrent/admin.conf（kurrent init 安装）——实时源在场时
//     优先实时源（不经快照、不受陈旧影响），脱离仓库才吃快照。
//
// 均不存在返回 ""（回退 Bearer/明文通道）；命中返回绝对路径（错误提示据此指明实际使用的凭据）。
func DiscoverCredentials() string {
	if cli.Credentials != "" {
		return cli.Credentials
	}
	if env := os.Getenv("KURRENT_CREDENTIALS"); env != "" {
		return env
	}
	if candidate := RecordCredentialsPath(); candidate != "" {
		return candidate
	}
	rel := filepath.Join("control_plane", "state", "admin.conf")
	if wd, err := os.Getwd(); err == nil {
		for dir := wd; ; {
			if candidate := filepath.Join(dir, rel); fsx.FileExists(candidate) {
				return candidate
			}
			parent := filepath.Dir(dir)
			if parent == dir { // 已到文件系统根
				break
			}
			dir = parent
		}
	}
	if dir := fsx.ExeDir(); dir != "" {
		if candidate := filepath.Join(filepath.Dir(dir), rel); fsx.FileExists(candidate) {
			return candidate
		}
	}
	if candidate := fsx.UserCredentialsPath(); candidate != "" && fsx.FileExists(candidate) {
		return candidate
	}
	return ""
}

// AuthHint 认证失败且两条凭据通道都空时的排查提示：此类失败最易误判为控制面问题，
// 实际是未发现 admin.conf、静默退回无凭据通道（见 DiscoverCredentials 注释）。
// 已发现凭据或已显式给 --token 时返回 ""（此时 401 是凭据本身的问题，不是发现的问题）。
func AuthHint() string {
	if cli.Token != "" || LoadCredentials() != nil {
		return ""
	}
	server := strings.TrimRight(cli.Server, "/")
	if server == "" {
		server = "(--server not specified)"
	}
	return fmt.Sprintf("\n  No management credentials found (checked the deployment record, control_plane/state/admin.conf by walking up from the cwd, next to the executable, and ~/.kurrent/admin.conf), and no --token was given;\n"+
		"  this request uses the credential-less channel against %s. Fix: kurrent init (installs credentials) | kurrent credentials install (refresh) | --credentials <admin.conf path> | --server + --token (the management API no longer listens on a local loopback port)",
		server)
}

// LoadCredentials 解析发现链选中的管理凭据文件；不可用时返回 nil。
func LoadCredentials() *CredentialsClient {
	return LoadCredentialsFile(DiscoverCredentials())
}

// LoadCredentialsFile 解析指定管理凭据文件（证书路径相对文件所在目录）；不可用时返回 nil。
func LoadCredentialsFile(path string) *CredentialsClient {
	if path == "" {
		return nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		cli.Fatal("read management credentials %s: %v (pass --credentials with another path, or omit it to fall back to --server/--token)", path, err)
	}
	var cf CredentialsFile
	if err := yaml.Unmarshal(data, &cf); err != nil {
		cli.Fatal("parse management credentials %s: %v", path, err)
	}
	if cf.Server == "" || cf.ClientCert == "" || cf.ClientKey == "" {
		cli.Fatal("management credentials %s are missing server/client-certificate/client-key", path)
	}
	c := &CredentialsClient{
		Path:     path,
		Server:   cf.Server,
		CAFile:   cf.CA,
		CertFile: cf.ClientCert,
		KeyFile:  cf.ClientKey,
	}
	dir := filepath.Dir(path)
	abs := func(p string) string {
		if p == "" || filepath.IsAbs(p) {
			return p
		}
		return filepath.Join(dir, p)
	}
	c.CAFile = abs(c.CAFile)
	c.CertFile = abs(c.CertFile)
	c.KeyFile = abs(c.KeyFile)
	return c
}

// client 构建 mTLS http 客户端：客户端证书（admin 身份）+ CA 校验服务端（TOFU 锚）。
func (c *CredentialsClient) Client() *http.Client {
	cert, err := tls.LoadX509KeyPair(c.CertFile, c.KeyFile)
	if err != nil {
		cli.Fatal("load management certificate %s: %v", c.CertFile, err)
	}
	tlsCfg := &tls.Config{Certificates: []tls.Certificate{cert}}
	if c.CAFile != "" {
		caPEM, err := os.ReadFile(c.CAFile)
		if err != nil {
			cli.Fatal("read management CA %s: %v", c.CAFile, err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(caPEM) {
			cli.Fatal("management CA %s contains no valid certificate", c.CAFile)
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
func (c *CredentialsClient) ServerURL() (string, error) {
	server := c.Server
	if cli.Server != "" {
		server = cli.Server
	}
	return validate.ValidateHTTPURL("management credential server (or --server)", server)
}

// CredentialsHint 返回当前发现的管理凭据路径（连接失败提示用）；未启用返回 ""。
func CredentialsHint() string {
	if path := DiscoverCredentials(); path != "" {
		return fmt.Sprintf(" (management credentials: %s; --server overrides the server address)", path)
	}
	return ""
}
