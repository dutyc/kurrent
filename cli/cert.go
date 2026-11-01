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

var (
	certKeygenSpec = cmdSpec{
		usage: certKeygenUsage,
		flags: []flagSpec{
			{name: "out", kind: flagString},
			{name: "force", kind: flagBool},
		},
		minPos: 1, maxPos: 1, posNames: []string{"cn"},
	}
	certIssueSpec = cmdSpec{
		usage: certIssueUsage,
		flags: []flagSpec{
			{name: "csr", kind: flagString, required: true},
			{name: "days", kind: flagString},
			{name: "out", kind: flagString},
			{name: "force", kind: flagBool},
		},
		minPos: 1, maxPos: 1, posNames: []string{"cn"},
	}
	certListSpec = cmdSpec{
		usage:  certListUsage,
		flags:  []flagSpec{{name: "output", aliases: []string{"o"}, kind: flagString}},
		maxPos: 0,
	}
	certRevokeSpec = cmdSpec{
		usage: certRevokeUsage,
		flags: []flagSpec{
			{name: "serial", kind: flagString},
			{name: "reason", kind: flagString},
		},
		minPos: 1, maxPos: 1, posNames: []string{"cn"},
	}
	certComponentsSpec = cmdSpec{
		usage:  certComponentsUsage,
		flags:  []flagSpec{{name: "output", aliases: []string{"o"}, kind: flagString}},
		maxPos: 0,
	}
	certRevokeComponentSpec = cmdSpec{
		usage: certRevokeComponentUsage,
		flags: []flagSpec{
			{name: "serial", kind: flagString},
			{name: "reason", kind: flagString},
		},
		minPos: 1, maxPos: 1, posNames: []string{"agent_id"},
	}
)

func cmdCert(args []string) {
	if groupHelp("cert", args, certGroupUsage) {
		return
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
		usageError(certGroupUsage, fmt.Errorf("unknown cert subcommand: %s", args[0]))
	}
}

func certKeygen(args []string) {
	p, help := mustParse(args, certKeygenSpec)
	if help {
		return
	}
	cn := strings.TrimSpace(p.pos[0])
	if !userCNPattern.MatchString(cn) {
		usageError(certKeygenUsage, fmt.Errorf("invalid cn: only letters, digits, dots, underscores, hyphens and @ are allowed (max 64)"))
	}
	dir := strings.TrimSpace(p.opt("out", "."))
	if dir == "" {
		usageError(certKeygenUsage, fmt.Errorf("--out must not be empty"))
	}
	keyPath := filepath.Join(dir, cn+".key")
	csrPath := filepath.Join(dir, cn+".csr")
	if !p.bool("force") {
		for _, path := range []string{keyPath, csrPath} {
			if _, err := os.Stat(path); err == nil {
				fatal("%s already exists; choose another --out or add --force (the old private key cannot be recovered)", path)
			}
		}
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		fatal("create output directory: %v", err)
	}
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		fatal("generate private key: %v", err)
	}
	csrDER, err := x509.CreateCertificateRequest(rand.Reader,
		&x509.CertificateRequest{Subject: pkix.Name{CommonName: cn}}, key)
	if err != nil {
		fatal("generate CSR: %v", err)
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{
		Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	csrPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: csrDER})
	if err := writeFileAtomic(keyPath, keyPEM, 0o600); err != nil {
		fatal("write private key: %v", err)
	}
	if err := writeFileAtomic(csrPath, csrPEM, 0o644); err != nil {
		fatal("write CSR: %v", err)
	}
	fmt.Printf("private key and CSR generated: cn=%s\n", cn)
	fmt.Printf("  private key: %s (0600)\n", keyPath)
	fmt.Printf("  CSR:         %s\n", csrPath)
	fmt.Printf("  next (register the user in users.yml first): kurrent cert issue %s --csr %s\n", cn, csrPath)
}

func certIssue(args []string) {
	p, help := mustParse(args, certIssueSpec)
	if help {
		return
	}
	cn := strings.TrimSpace(p.pos[0])
	if !userCNPattern.MatchString(cn) {
		usageError(certIssueUsage, fmt.Errorf("invalid cn: only letters, digits, dots, underscores, hyphens and @ are allowed (max 64)"))
	}
	csrPath := p.opt("csr", "")
	csrPEM, err := os.ReadFile(csrPath)
	if err != nil {
		fatal("read CSR: %v", err)
	}
	days := 0
	if p.has("days") {
		raw := strings.TrimSpace(p.opt("days", ""))
		days, err = strconv.Atoi(raw)
		if err != nil || days <= 0 {
			usageError(certIssueUsage, fmt.Errorf("--days must be a positive integer (got %q)", raw))
		}
	}
	outDir := strings.TrimSpace(p.opt("out", "."))
	if outDir == "" {
		usageError(certIssueUsage, fmt.Errorf("--out must not be empty"))
	}
	certPath := filepath.Join(outDir, cn+".crt")
	if !p.bool("force") {
		if _, err := os.Stat(certPath); err == nil {
			fatal("%s already exists; choose another --out or add --force", certPath)
		}
	}
	if err := os.MkdirAll(outDir, 0o700); err != nil {
		fatal("create output directory: %v", err)
	}

	body := map[string]any{"csr": string(csrPEM)}
	if days > 0 {
		body["days"] = days
	}
	var out certIssueResp
	if err := json.Unmarshal(api("POST", "/pki/users/"+url.PathEscape(cn), body), &out); err != nil {
		fatal("parse issuance response: %v", err)
	}
	if out.CN != cn || !userCNPattern.MatchString(out.CN) {
		fatal("server returned cn %q, request was %q", out.CN, cn)
	}
	if block, _ := pem.Decode([]byte(out.Certificate)); block == nil || block.Type != "CERTIFICATE" {
		fatal("server returned a certificate that is not PEM CERTIFICATE")
	}
	if err := writeFileAtomic(certPath, []byte(out.Certificate), 0o644); err != nil {
		fatal("write certificate: %v", err)
	}
	fmt.Printf("certificate issued: cn=%s, serial=%s, expires %s\n", out.CN, out.Serial, shortTS(out.ExpiresAt))
	fmt.Printf("  certificate: %s\n", certPath)
	fmt.Printf("  fingerprint: %s\n", out.Fingerprint)
	fmt.Printf("  the private key was never uploaded and remains local; if lost, run keygen + issue again and revoke the old certificate with --serial\n")
}

func certList(args []string) {
	p, help := mustParse(args, certListSpec)
	if help {
		return
	}
	format, err := p.outputFormat()
	if err != nil {
		usageError(certListUsage, err)
	}
	var items []certInfo
	if err := json.Unmarshal(api("GET", "/pki/users", nil), &items); err != nil {
		fatal("parse certificate list: %v", err)
	}
	if format == "json" {
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

// serialFlag 解析 --serial：显式空值报用法错误（省略才是“全部未吊销证书”，
// 避免 --serial= 被误当成省略而扩大吊销范围）。
func serialFlag(p parsedArgs, usage string) string {
	raw := p.opt("serial", "")
	if p.has("serial") && strings.TrimSpace(raw) == "" {
		usageError(usage, fmt.Errorf("--serial must not be empty (omit the flag to target every unrevoked certificate)"))
	}
	serial, err := validateSerial(raw)
	if err != nil {
		usageError(usage, err)
	}
	return serial
}

func certRevoke(args []string) {
	p, help := mustParse(args, certRevokeSpec)
	if help {
		return
	}
	cn := strings.TrimSpace(p.pos[0])
	if !userCNPattern.MatchString(cn) {
		usageError(certRevokeUsage, fmt.Errorf("invalid cn: only letters, digits, dots, underscores, hyphens and @ are allowed (max 64)"))
	}
	reason, err := validateRevocationReason(p.opt("reason", "unspecified"))
	if err != nil {
		usageError(certRevokeUsage, err)
	}
	serial := serialFlag(p, certRevokeUsage)
	path := "/pki/users/" + url.PathEscape(cn) + "?reason=" + url.QueryEscape(reason)
	if serial != "" {
		path += "&serial=" + url.QueryEscape(serial)
	}
	var out certRevokeResp
	if err := json.Unmarshal(api("DELETE", path, nil), &out); err != nil {
		fatal("parse revocation response: %v", err)
	}
	fmt.Printf("certificate(s) revoked: %s (serial %s, reason %s)\n",
		out.Revoked, strings.Join(out.Serials, ","), out.Reason)
	fmt.Printf("sessions revoked: %d; CRL re-signed and nginx reloaded\n", out.Sessions)
}

func certComponents(args []string) {
	p, help := mustParse(args, certComponentsSpec)
	if help {
		return
	}
	format, err := p.outputFormat()
	if err != nil {
		usageError(certComponentsUsage, err)
	}
	var items []componentCertInfo
	if err := json.Unmarshal(api("GET", "/pki/components", nil), &items); err != nil {
		fatal("parse component certificate list: %v", err)
	}
	if format == "json" {
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
	p, help := mustParse(args, certRevokeComponentSpec)
	if help {
		return
	}
	agentID, err := validateAgentID(p.pos[0])
	if err != nil {
		usageError(certRevokeComponentUsage, err)
	}
	reason, err := validateRevocationReason(p.opt("reason", "unspecified"))
	if err != nil {
		usageError(certRevokeComponentUsage, err)
	}
	serial := serialFlag(p, certRevokeComponentUsage)
	path := "/pki/components/" + url.PathEscape(agentID) + "?reason=" + url.QueryEscape(reason)
	if serial != "" {
		path += "&serial=" + url.QueryEscape(serial)
	}
	var out componentRevokeResp
	if err := json.Unmarshal(api("DELETE", path, nil), &out); err != nil {
		fatal("parse revocation response: %v", err)
	}
	fmt.Printf("component certificate(s) revoked: %s (serial %s, reason %s)\n",
		out.Revoked, strings.Join(out.Serials, ","), out.Reason)
	fmt.Printf("kinds: %s; nodes CRL re-signed and nginx reloaded, bootstrap token deleted\n",
		strings.Join(out.Kinds, ","))
}
