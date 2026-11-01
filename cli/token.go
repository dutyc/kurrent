// token 子命令：签发 / 列表 / 吊销凭据。
// usage=bootstrap（默认）：引导 token——绑定节点身份（--agent-id，12 位小写 hex；省略则生成），
// enroll 只接受同一节点身份；默认一次性、60 分钟（--uses 放开次数）。
// usage=admin：管理 token（认证器链 Bearer）——供脚本调用与 WebUI 登录（粘贴）。
// list：列出未过期台账条目（无明文/hash）；revoke：删条目即失效。
// nvmet-host 组件凭据不在此签发：agent enroll 上报 backend=nvmet 时控制面
// 派生随响应下发（能力上报驱动，签发不预知后端）。
package main

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
	"text/tabwriter"
	"time"
)

type tokenResp struct {
	Token     string   `json:"token"`
	ExpiresAt string   `json:"expires_at"`
	Usage     []string `json:"usage"`
	Groups    []string `json:"groups"`
	Scopes    []string `json:"scopes"`
	Uses      int      `json:"uses"`
	MaxUses   int      `json:"max_uses"`
	AgentID   string   `json:"agent_id"`
	Component string   `json:"component"`
}

// tokenInfo：GET /pki/tokens 列表条目（与后端字段一一对应）
type tokenInfo struct {
	TokenID    string   `json:"token_id"`
	Usage      []string `json:"usage"`
	Groups     []string `json:"groups"`
	Scopes     []string `json:"scopes"`
	Uses       int      `json:"uses"`
	MaxUses    int      `json:"max_uses"`
	Component  string   `json:"component"`
	AgentID    string   `json:"agent_id"`
	Name       string   `json:"name"`
	CreatedAt  string   `json:"created_at"`
	ExpiresAt  string   `json:"expires_at"`
	LastUsedAt string   `json:"last_used_at"`
}

var (
	tokenCreateSpec = cmdSpec{
		usage: tokenCreateUsage,
		flags: []flagSpec{
			{name: "usage", kind: flagString},
			{name: "agent-id", kind: flagString},
			{name: "cp-url", kind: flagString},
			{name: "uses", kind: flagString},
			{name: "scope", kind: flagString},
			{name: "ttl", kind: flagString},
		},
		maxPos: 0,
	}
	tokenListSpec   = cmdSpec{usage: tokenListUsage, flags: []flagSpec{{name: "output", aliases: []string{"o"}, kind: flagString}}, maxPos: 0}
	tokenRevokeSpec = cmdSpec{usage: tokenRevokeUsage, minPos: 1, maxPos: 1, posNames: []string{"token_id"}}
)

func cmdToken(args []string) {
	if groupHelp("token", args, tokenGroupUsage) {
		return
	}
	switch args[0] {
	case "create":
		tokenCreate(args[1:])
	case "list":
		tokenList(args[1:])
	case "revoke":
		tokenRevoke(args[1:])
	default:
		usageError(tokenGroupUsage, fmt.Errorf("unknown token subcommand: %s", args[0]))
	}
}

func tokenCreate(args []string) {
	p, help := mustParse(args, tokenCreateSpec)
	if help {
		return
	}
	usage := p.opt("usage", "bootstrap")
	if usage != "bootstrap" && usage != "admin" {
		usageError(tokenCreateUsage, fmt.Errorf("--usage accepts only bootstrap or admin (got %q)", usage))
	}

	var body any
	cpURL := ""
	joinURL := ""
	agentID := ""
	if usage == "admin" {
		for _, flag := range []string{"agent-id", "cp-url", "uses"} {
			if p.has(flag) {
				usageError(tokenCreateUsage, fmt.Errorf("--%s applies only to bootstrap tokens", flag))
			}
		}
		req := map[string]any{"usage": "admin"}
		if p.has("scope") {
			scopes, err := parseScopes(p.opt("scope", ""))
			if err != nil {
				usageError(tokenCreateUsage, err)
			}
			req["scopes"] = scopes
		}
		if p.has("ttl") {
			raw := strings.TrimSpace(p.opt("ttl", ""))
			if raw == "" {
				usageError(tokenCreateUsage, fmt.Errorf("--ttl requires a duration (e.g. 24h)"))
			}
			d, err := time.ParseDuration(raw)
			if err != nil || d <= 0 {
				usageError(tokenCreateUsage, fmt.Errorf("--ttl must be a positive duration (e.g. 24h): %q", raw))
			}
			// 上限是控制面策略（tokenMaxTtlHours，默认 720h，schema 绝对上限 8760h），
			// CLI 只挡住超出 schema 的值，具体策略由服务端裁决。先比较再向上取整，
			// 避免接近 Duration 上限时加法溢出。
			if d > 8760*time.Hour {
				usageError(tokenCreateUsage, fmt.Errorf("--ttl exceeds the 8760h (365-day) limit: %s", raw))
			}
			hours := int((d + time.Hour - 1) / time.Hour)
			req["ttl_hours"] = hours
		}
		body = req
	} else {
		for _, flag := range []string{"scope", "ttl"} {
			if p.has(flag) {
				usageError(tokenCreateUsage, fmt.Errorf("--%s applies only to admin tokens", flag))
			}
		}
		if p.has("agent-id") {
			var err error
			if agentID, err = validateAgentID(p.opt("agent-id", "")); err != nil {
				usageError(tokenCreateUsage, err)
			}
		} else {
			id, err := newNodeID()
			if err != nil {
				fatal("generate node id: %v", err)
			}
			agentID = id
		}
		if p.has("cp-url") {
			var err error
			if cpURL, err = normalizeCpURL("--cp-url", p.opt("cp-url", "")); err != nil {
				usageError(tokenCreateUsage, err)
			}
		}
		req := map[string]any{"usage": "bootstrap", "agent_id": agentID}
		if p.has("uses") {
			raw := strings.TrimSpace(p.opt("uses", ""))
			uses, err := strconv.Atoi(raw)
			// 1000 是控制面 schema 的绝对值；实际上限由 bootstrapTokenMaxUses 决定
			if err != nil || uses < 1 || uses > 1000 {
				usageError(tokenCreateUsage, fmt.Errorf("--uses must be an integer in 1..1000: %q", raw))
			}
			req["max_uses"] = uses
		}
		body = req
	}

	// 先确定 join 命令里的控制面入口再签发：入口缺失/不可解析时不应先产生一张 token。
	if usage == "bootstrap" {
		var err error
		if joinURL, err = joinCpURL(cpURL); err != nil {
			fatal("%v", err)
		}
	}
	var out tokenResp
	if err := json.Unmarshal(api("POST", "/pki/tokens", body), &out); err != nil {
		fatal("parse token response: %v", err)
	}
	if strings.TrimSpace(out.Token) == "" {
		fatal("the control plane returned no token (missing token field)")
	}
	if usage == "bootstrap" && out.AgentID != agentID {
		fatal("server returned agent_id %q, request was %q", out.AgentID, agentID)
	}
	if usage == "admin" {
		fmt.Printf("admin token: %s (scopes %s, expires %s)\n",
			out.Token, strings.Join(out.Scopes, ","), out.ExpiresAt)
		fmt.Printf("\n# Usage (management credential equivalent to the admin.conf certificate identity):\n")
		fmt.Printf("#   CLI: kurrent --server <CP> --token %s agents list\n", out.Token)
		fmt.Printf("#   WebUI: paste on the sign-in page; scripts: curl -H \"Authorization: Bearer <token>\"\n")
		fmt.Printf("#   Revoke: kurrent token revoke %s (or the WebUI Credentials page)\n", strings.SplitN(out.Token, ".", 2)[0])
		return
	}
	fmt.Printf("bootstrap token: %s (expires %s, bound node %s, %s)\n",
		out.Token, out.ExpiresAt, out.AgentID, tokenUseLimit(out.Uses, out.MaxUses))
	fmt.Printf("\n# Run on the bound node (--agent-id is written to metadata.name):\n")
	fmt.Printf("kurrent join %s --token %s --agent-id %s\n",
		joinURL, out.Token, out.AgentID)
	fmt.Printf("# After joining, verify with: kurrent agents list (or the WebUI Agents page, health=ok)\n")
}

func parseScopes(raw string) ([]string, error) {
	allowed := map[string]bool{"admin": true, "readonly": true, "enroll": true}
	seen := map[string]bool{}
	scopes := []string{}
	for _, part := range strings.Split(raw, ",") {
		scope := strings.TrimSpace(part)
		if scope == "" {
			return nil, fmt.Errorf("--scope contains an empty item: %q", raw)
		}
		if !allowed[scope] {
			return nil, fmt.Errorf("--scope contains an unknown scope %q (allowed: admin,readonly,enroll)", scope)
		}
		if seen[scope] {
			return nil, fmt.Errorf("--scope contains a duplicate scope %q", scope)
		}
		seen[scope] = true
		scopes = append(scopes, scope)
	}
	if len(scopes) == 0 {
		return nil, fmt.Errorf("--scope requires at least one of admin,readonly,enroll")
	}
	return scopes, nil
}

func tokenList(args []string) {
	p, help := mustParse(args, tokenListSpec)
	if help {
		return
	}
	format, err := p.outputFormat()
	if err != nil {
		usageError(tokenListUsage, err)
	}
	var items []tokenInfo
	if err := json.Unmarshal(api("GET", "/pki/tokens", nil), &items); err != nil {
		fatal("parse token list: %v", err)
	}
	if format == "json" {
		printJSON(items)
		return
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 4, 2, ' ', 0)
	fmt.Fprintln(w, "TOKEN_ID\tUSAGE\tSCOPES\tGROUPS\tUSES\tCOMPONENT-AGENT\tNAME\tEXPIRES\tLAST_USED\tCREATED")
	for _, t := range items {
		owner := t.Component
		if t.AgentID != "" {
			owner = t.Component + "/" + t.AgentID
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
			t.TokenID, strings.Join(t.Usage, ","), dashIfEmpty(strings.Join(t.Scopes, ",")),
			strings.Join(t.Groups, ","), tokenUseLimit(t.Uses, t.MaxUses), dashIfEmpty(owner),
			dashIfEmpty(t.Name), shortTS(t.ExpiresAt), shortTS(t.LastUsedAt), shortTS(t.CreatedAt))
	}
	w.Flush()
}

func tokenRevoke(args []string) {
	p, help := mustParse(args, tokenRevokeSpec)
	if help {
		return
	}
	id, err := validateTokenID(p.pos[0])
	if err != nil {
		usageError(tokenRevokeUsage, err)
	}
	api("DELETE", "/pki/tokens/"+url.PathEscape(id), nil)
	fmt.Printf("token %s revoked (ledger entry deleted; Bearer validation fails immediately)\n", id)
}

func dashIfEmpty(s string) string {
	if s == "" {
		return "—"
	}
	return s
}

func tokenUseLimit(uses, maxUses int) string {
	if maxUses == 0 {
		return "unlimited"
	}
	return fmt.Sprintf("%d/%d", uses, maxUses)
}

// shortTS 表格展示用：ISO 时间截到分钟（完整值走 -o json）
func shortTS(s string) string {
	if s == "" {
		return "—"
	}
	if len(s) >= 16 {
		return s[:16]
	}
	return s
}

// joinCpURL 推导节点侧控制面入口（输出带地址的 join 命令，可直接粘贴）：
// 显式 --cp-url 优先，否则从当前实际使用的 API 入口（凭据文件或 --server）取
// scheme/host（保留非默认端口，去掉 /api/cp 路径）；无法确定时返回错误，
// 由调用方在签发 token 之前失败。
func joinCpURL(explicit string) (string, error) {
	if explicit != "" {
		return strings.TrimRight(explicit, "/"), nil
	}
	server, err := apiServerURL()
	if err != nil {
		return "", err
	}
	u, err := url.Parse(server)
	if err != nil || u.Host == "" {
		return "", fmt.Errorf("cannot derive the join address from control-plane entry %q; specify --cp-url explicitly", server)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", fmt.Errorf("invalid control-plane entry scheme: %q (must be http or https)", server)
	}
	return u.Scheme + "://" + u.Host, nil
}
