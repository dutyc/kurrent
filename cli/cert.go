// cert 子命令：users 域证书签发、台账与吊销；nodes 域组件证书台账与吊销。
// keygen：本地生成 RSA 私钥与 CSR（私钥永不上送）。
// issue：把本地 CSR 交给 users CA 签发（可登记用户），只回传证书。
// list：列出 users CA 签发的证书（含已吊销/已过期；serial 为 hex，fingerprint 为 sha256）。
// revoke：吊销某 CN 的全部未吊销证书（或 --serial 指定一张）——登记标记 + 重签 CRL
// + reload nginx + 踢相关会话；吊销后证书在下一次 TLS 握手即被拒绝（无需等过期）。
// 不能吊销当前凭据自身。
// components / revoke-component：agent 与 nvmet-host 的 nodes 域证书台账与吊销
// （吊销写 nodes CRL，并删除该节点的 bootstrap token）。
package main

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"text/tabwriter"
)

// 与控制面 USER_CN_PATTERN 一致：CN 同时用作本地文件名，先在这里挡一次路径穿越
var userCNPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$`)

type certInfo struct {
	CN          string `json:"cn"`
	Serial      string `json:"serial"`
	Fingerprint string `json:"fingerprint"`
	CreatedAt   string `json:"created_at"`
	ExpiresAt   string `json:"expires_at"`
	RevokedAt   string `json:"revoked_at"`
	Reason      string `json:"reason"`
	Expired     bool   `json:"expired"`
}

type certRevokeResp struct {
	Revoked  string   `json:"revoked"`
	Serials  []string `json:"serials"`
	Sessions int      `json:"sessions"`
	Reason   string   `json:"reason"`
}

type certIssueResp struct {
	CN          string `json:"cn"`
	Serial      string `json:"serial"`
	Fingerprint string `json:"fingerprint"`
	CreatedAt   string `json:"created_at"`
	ExpiresAt   string `json:"expires_at"`
	Certificate string `json:"certificate"`
}

type componentCertInfo struct {
	CN          string `json:"cn"`
	AgentID     string `json:"agent_id"`
	Kind        string `json:"kind"`
	Serial      string `json:"serial"`
	Fingerprint string `json:"fingerprint"`
	CreatedAt   string `json:"created_at"`
	ExpiresAt   string `json:"expires_at"`
	RevokedAt   string `json:"revoked_at"`
	Reason      string `json:"reason"`
	Expired     bool   `json:"expired"`
}

type componentRevokeResp struct {
	Revoked string   `json:"revoked"`
	Serials []string `json:"serials"`
	Kinds   []string `json:"kinds"`
	Reason  string   `json:"reason"`
}

func cmdCert(args []string) {
	if len(args) < 1 {
		certUsage()
	}
	switch args[0] {
	case "keygen":
		certKeygen(args[1:])
	case "issue":
		certIssue(args[1:])
	case "list":
		certList(args[1:])
	case "revoke":
		certRevoke(args[1:])
	case "components":
		certComponents(args[1:])
	case "revoke-component":
		certRevokeComponent(args[1:])
	default:
		certUsage()
	}
}

func certUsage() {
	fatal("用法: kurrent cert keygen <cn> [--out DIR] [--force]\n" +
		"      本地生成 RSA-2048 私钥与 CSR：写 <DIR>/<cn>.key（0600）与 <cn>.csr，默认 DIR=.\n" +
		"      kurrent cert issue <cn> --csr FILE [--days N] [--out DIR] [--force]\n" +
		"      为 users.yml 中已登记的用户签发：提交 CSR，写 <DIR>/<cn>.crt（私钥不上送）\n" +
		"      kurrent cert list [-o table|json]   列出 users CA 签发的证书（含已吊销/已过期）\n" +
		"      kurrent cert revoke <cn> [--serial HEX] [--reason unspecified|key_compromise|superseded|cessation_of_operation]\n" +
		"      吊销：标记登记 + 重签 CRL + reload nginx + 踢相关会话，下一次握手即拒绝\n" +
		"      kurrent cert components [-o table|json]\n" +
		"      列出 agent / nvmet-host 的 nodes 域证书（含已吊销/已过期）\n" +
		"      kurrent cert revoke-component <agent_id> [--serial HEX] [--reason ...]\n" +
		"      吊销组件证书：写 nodes CRL + reload nginx + 删除该节点的 bootstrap token")
}

func certKeygen(args []string) {
	p := parseArgs(args)
	cn := strings.TrimSpace(firstPos(p))
	if cn == "" {
		fatal("用法: kurrent cert keygen <cn> [--out DIR] [--force]")
	}
	if !userCNPattern.MatchString(cn) {
		fatal("非法 cn: 只允许字母、数字、点、下划线、短横线与 @（最长 64）")
	}
	dir := p.opt("out", ".")
	keyPath := filepath.Join(dir, cn+".key")
	csrPath := filepath.Join(dir, cn+".csr")
	if !p.flags["force"] {
		for _, path := range []string{keyPath, csrPath} {
			if _, err := os.Stat(path); err == nil {
				fatal("%s 已存在；换 --out 目录或用 --force 覆盖（覆盖后旧私钥不可恢复）", path)
			}
		}
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		fatal("创建输出目录: %v", err)
	}
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		fatal("生成私钥: %v", err)
	}
	csrDER, err := x509.CreateCertificateRequest(rand.Reader,
		&x509.CertificateRequest{Subject: pkix.Name{CommonName: cn}}, key)
	if err != nil {
		fatal("生成 CSR: %v", err)
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{
		Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	csrPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: csrDER})
	if err := os.WriteFile(keyPath, keyPEM, 0o600); err != nil {
		fatal("写私钥: %v", err)
	}
	if err := os.Chmod(keyPath, 0o600); err != nil {
		fatal("设置私钥权限: %v", err)
	}
	if err := os.WriteFile(csrPath, csrPEM, 0o644); err != nil {
		fatal("写 CSR: %v", err)
	}
	fmt.Printf("key and CSR generated: cn=%s\n", cn)
	fmt.Printf("  private key: %s (0600)\n", keyPath)
	fmt.Printf("  csr:         %s\n", csrPath)
	fmt.Printf("  下一步（用户须先在 users.yml 登记）: kurrent cert issue %s --csr %s\n", cn, csrPath)
}

func certIssue(args []string) {
	p := parseArgs(args)
	cn := strings.TrimSpace(firstPos(p))
	if cn == "" {
		fatal("用法: kurrent cert issue <cn> --csr FILE [--days N] [--out DIR] [--force]（用户须先在 users.yml 中登记）")
	}
	csrPath := p.opt("csr", "")
	if csrPath == "" {
		fatal("缺少 --csr：先用 kurrent cert keygen %s 在本地生成私钥与 CSR", cn)
	}
	csrPEM, err := os.ReadFile(csrPath)
	if err != nil {
		fatal("读取 CSR: %v", err)
	}
	body := map[string]any{"csr": string(csrPEM)}
	if raw := p.opt("days", ""); raw != "" {
		days, err := strconv.Atoi(raw)
		if err != nil {
			fatal("--days 必须是整数: %v", err)
		}
		body["days"] = days
	}
	var out certIssueResp
	if err := json.Unmarshal(api("POST", "/pki/users/"+url.PathEscape(cn), body), &out); err != nil {
		fatal("解析签发响应: %v", err)
	}
	// 文件名用服务端校验过的 CN，且再拒一次路径分隔符（绝不把 argv 直接拼进路径）
	if !userCNPattern.MatchString(out.CN) {
		fatal("服务端返回了非法的 cn: %q", out.CN)
	}
	dir := p.opt("out", ".")
	certPath := filepath.Join(dir, out.CN+".crt")
	if !p.flags["force"] {
		if _, err := os.Stat(certPath); err == nil {
			fatal("%s 已存在；换 --out 目录或用 --force 覆盖", certPath)
		}
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		fatal("创建输出目录: %v", err)
	}
	if err := os.WriteFile(certPath, []byte(out.Certificate), 0o644); err != nil {
		fatal("写证书: %v", err)
	}
	fmt.Printf("certificate issued: cn=%s serial=%s expires=%s\n", out.CN, out.Serial, shortTS(out.ExpiresAt))
	fmt.Printf("  certificate: %s\n", certPath)
	fmt.Printf("  fingerprint: %s\n", out.Fingerprint)
	fmt.Printf("  私钥未上送、仍在本机；丢失私钥就重新 keygen + issue，旧证书用 --serial 单独吊销\n")
}

func certList(args []string) {
	p := parseArgs(args)
	var items []certInfo
	if err := json.Unmarshal(api("GET", "/pki/users", nil), &items); err != nil {
		fatal("解析证书列表: %v", err)
	}
	if p.opt("o", "table") == "json" {
		printJSON(items)
		return
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 4, 2, ' ', 0)
	fmt.Fprintln(w, "CN\tSERIAL\tFINGERPRINT\tSTATUS\tEXPIRES\tCREATED\tREASON")
	for _, c := range items {
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
			c.CN, c.Serial, shortFingerprint(c.Fingerprint), certStatus(c),
			shortTS(c.ExpiresAt), shortTS(c.CreatedAt), dashIfEmpty(c.Reason))
	}
	w.Flush()
}

func certStatus(c certInfo) string {
	switch {
	case c.RevokedAt != "":
		return "revoked"
	case c.Expired:
		return "expired"
	default:
		return "active"
	}
}

// shortFingerprint 表格展示用：sha256 前 16 hex（完整值走 -o json）
func shortFingerprint(s string) string {
	if len(s) > 16 {
		return s[:16]
	}
	return s
}

func certRevoke(args []string) {
	p := parseArgs(args)
	cn := strings.TrimSpace(firstPos(p))
	if cn == "" {
		fatal("用法: kurrent cert revoke <cn> [--reason ...]（kurrent cert list 查看 cn）")
	}
	reason := p.opt("reason", "unspecified")
	path := "/pki/users/" + url.PathEscape(cn) + "?reason=" + url.QueryEscape(reason)
	if serial := strings.TrimSpace(p.opt("serial", "")); serial != "" {
		path += "&serial=" + url.QueryEscape(serial)
	}
	var out certRevokeResp
	if err := json.Unmarshal(api("DELETE", path, nil), &out); err != nil {
		fatal("解析吊销响应: %v", err)
	}
	fmt.Printf("certificate for %s revoked（serial %s，reason %s）\n",
		out.Revoked, strings.Join(out.Serials, ","), out.Reason)
	fmt.Printf("sessions revoked: %d；CRL 已重签并 reload nginx，下一次握手即拒绝\n", out.Sessions)
}

func certComponents(args []string) {
	p := parseArgs(args)
	var items []componentCertInfo
	if err := json.Unmarshal(api("GET", "/pki/components", nil), &items); err != nil {
		fatal("解析组件证书列表: %v", err)
	}
	if p.opt("o", "table") == "json" {
		printJSON(items)
		return
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 4, 2, ' ', 0)
	fmt.Fprintln(w, "AGENT\tKIND\tCN\tSERIAL\tFINGERPRINT\tSTATUS\tEXPIRES\tCREATED\tREASON")
	for _, c := range items {
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
			dashIfEmpty(c.AgentID), c.Kind, c.CN, c.Serial, shortFingerprint(c.Fingerprint),
			componentCertStatus(c), shortTS(c.ExpiresAt), shortTS(c.CreatedAt),
			dashIfEmpty(c.Reason))
	}
	w.Flush()
}

func componentCertStatus(c componentCertInfo) string {
	return certStatus(certInfo{RevokedAt: c.RevokedAt, Expired: c.Expired})
}

func certRevokeComponent(args []string) {
	p := parseArgs(args)
	agentID := strings.TrimSpace(firstPos(p))
	if agentID == "" {
		fatal("用法: kurrent cert revoke-component <agent_id> [--serial HEX] [--reason ...]（kurrent cert components 查看）")
	}
	reason := p.opt("reason", "unspecified")
	path := "/pki/components/" + url.PathEscape(agentID) + "?reason=" + url.QueryEscape(reason)
	if serial := strings.TrimSpace(p.opt("serial", "")); serial != "" {
		path += "&serial=" + url.QueryEscape(serial)
	}
	var out componentRevokeResp
	if err := json.Unmarshal(api("DELETE", path, nil), &out); err != nil {
		fatal("解析吊销响应: %v", err)
	}
	fmt.Printf("component certificates for %s revoked（serial %s，reason %s）\n",
		out.Revoked, strings.Join(out.Serials, ","), out.Reason)
	fmt.Printf("kinds: %s；nodes CRL 已重签并 reload nginx，bootstrap token 已删除\n",
		strings.Join(out.Kinds, ","))
}
