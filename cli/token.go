// token 子命令：签发 / 列表 / 吊销凭据。
// usage=bootstrap（默认）：引导 token——绑定节点身份（--agent-id，省略则生成随机短标识），
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

func cmdToken(args []string) {
	if len(args) < 1 {
		tokenUsage()
	}
	switch args[0] {
	case "create":
		tokenCreate(args[1:])
	case "list":
		tokenList(args[1:])
	case "revoke":
		tokenRevoke(args[1:])
	default:
		tokenUsage()
	}
}

func tokenUsage() {
	fatal("用法: kurrent token create [--usage bootstrap|admin] [--agent-id ID] [--cp-url URL]\n" +
		"                          [--uses N] [--scope admin,readonly,enroll] [--ttl 24h]\n" +
		"      kurrent token list [-o table|json]   列出未过期 token（台账可视，不返回明文）\n" +
		"      kurrent token revoke <token_id>      吊销：删条目即失效\n" +
		"      create: bootstrap（默认）引导凭据，绑定节点身份（= 节点声明 metadata.name；\n" +
		"      --agent-id 省略时生成随机短标识）；默认 60 分钟有效、一次性（--uses N\n" +
		"      提高可用次数）；--cp-url 指定节点侧可达的控制面 HTTPS 入口；\n" +
		"      输出可直接粘贴的 join 命令（缺省按 --server 主机推导 https://<host>）\n" +
		"      admin——管理 token（认证器链 Bearer：脚本调用 / WebUI 登录粘贴）；\n" +
		"      --scope 收窄权限（admin 默认 | readonly | enroll=仅签发节点引导 token），\n" +
		"      --ttl 默认 24h、上限 720h（30 天）")
}

func tokenCreate(args []string) {
	p := parseArgs(args)
	usage := p.opt("usage", "bootstrap")
	if usage != "bootstrap" && usage != "admin" {
		fatal("--usage 仅支持 bootstrap|admin（收到 %q）", usage)
	}
	var body any
	if usage == "admin" {
		req := map[string]any{"usage": "admin"}
		if raw := strings.TrimSpace(p.opt("scope", "")); raw != "" {
			var scopes []string
			for _, scope := range strings.Split(raw, ",") {
				if scope = strings.TrimSpace(scope); scope != "" {
					scopes = append(scopes, scope)
				}
			}
			req["scopes"] = scopes
		}
		if raw := strings.TrimSpace(p.opt("ttl", "")); raw != "" {
			d, err := time.ParseDuration(raw)
			if err != nil || d <= 0 {
				fatal("--ttl 格式无效（例：24h、720h）: %s", raw)
			}
			hours := int((d + time.Hour - 1) / time.Hour)
			if hours > 720 {
				fatal("--ttl 超过上限 720h（30 天）: %s", raw)
			}
			req["ttl_hours"] = hours
		}
		body = req
	} else {
		agentID := strings.TrimSpace(p.opt("agent-id", ""))
		if agentID == "" {
			// 身份机器生成：省略 --agent-id 时生成随机短标识（与 join 同构，12 位 hex）
			id, err := newNodeID()
			if err != nil {
				fatal("生成节点 id: %v", err)
			}
			agentID = id
		} else if normalizeAgentID(agentID) != agentID {
			fatal("--agent-id 含非法字符（字符集仅允许小写字母/数字/./-）: %s", agentID)
		}
		if p.opt("ttl", "") != "" {
			fatal("--ttl 仅适用于 admin token；bootstrap TTL 由 spec.pki.bootstrapTokenTtlMinutes 决定")
		}
		req := map[string]any{"usage": "bootstrap", "agent_id": agentID}
		if raw := strings.TrimSpace(p.opt("uses", "")); raw != "" {
			uses, err := strconv.Atoi(raw)
			if err != nil || uses < 1 || uses > 100 {
				fatal("--uses 需为 1..100 的整数: %s", raw)
			}
			req["max_uses"] = uses
		}
		body = req
	}
	var out tokenResp
	if err := json.Unmarshal(api("POST", "/pki/tokens", body), &out); err != nil {
		fatal("解析 token 响应: %v", err)
	}
	if usage == "admin" {
		fmt.Printf("admin token: %s（scopes %s，expires %s）\n",
			out.Token, strings.Join(out.Scopes, ","), out.ExpiresAt)
		fmt.Printf("\n# 用途（管理凭据，等同 admin.conf 证书身份）：\n")
		fmt.Printf("#   CLI：kurrent --server <CP> --token %s agents list\n", out.Token)
		fmt.Printf("#   WebUI：登录页粘贴；脚本：curl -H \"Authorization: Bearer <token>\"\n")
		fmt.Printf("#   吊销：kurrent token revoke %s（或 WebUI「凭据」页）\n", strings.SplitN(out.Token, ".", 2)[0])
		return
	}
	fmt.Printf("bootstrap token: %s（expires %s，绑定节点 %s，%s）\n",
		out.Token, out.ExpiresAt, out.AgentID, tokenUseLimit(out.Uses, out.MaxUses))
	fmt.Printf("\n# 在绑定节点上执行（--agent-id 写入节点声明 metadata.name）：\n")
	fmt.Printf("kurrent join %s --token %s --agent-id %s\n",
		joinCpURL(p.opt("cp-url", "")), out.Token, out.AgentID)
	fmt.Printf("# 加入后验证：kurrent agents list（或 WebUI「Agent 列表」health=ok）\n")
}

// tokenList 列出未过期 token（表格 / -o json）
func tokenList(args []string) {
	p := parseArgs(args)
	var items []tokenInfo
	if err := json.Unmarshal(api("GET", "/pki/tokens", nil), &items); err != nil {
		fatal("解析 token 列表: %v", err)
	}
	if p.opt("o", "table") == "json" {
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
			strings.Join(t.Groups, ","), tokenUseLimit(t.Uses, t.MaxUses), owner,
			dashIfEmpty(t.Name), shortTS(t.ExpiresAt), shortTS(t.LastUsedAt), shortTS(t.CreatedAt))
	}
	w.Flush()
}

// tokenRevoke 吊销 token：删除台账条目（删除即失效；404 由 api() 兜底 fatal）
func tokenRevoke(args []string) {
	id := firstPos(parseArgs(args))
	if id == "" {
		fatal("用法: kurrent token revoke <token_id>（kurrent token list 查看 id）")
	}
	api("DELETE", "/pki/tokens/"+url.PathEscape(id), nil)
	fmt.Printf("token %s revoked（登记条目已删除，Bearer 校验立即失效）\n", id)
}

func dashIfEmpty(s string) string {
	if s == "" {
		return "—"
	}
	return s
}

// tokenUseLimit 展示使用额度：0 = 不限（旧条目/管理 token），否则 uses/max。
func tokenUseLimit(uses, maxUses int) string {
	if maxUses == 0 {
		return "不限"
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
// 显式 --cp-url 优先，否则按 --server 主机推 https://<host>。
func joinCpURL(explicit string) string {
	if explicit != "" {
		return strings.TrimRight(explicit, "/")
	}
	server := strings.TrimRight(flagServer, "/")
	if server == "" {
		return "https://127.0.0.1"
	}
	u, err := url.Parse(server)
	if err != nil || u.Host == "" {
		return "https://127.0.0.1"
	}
	return "https://" + u.Hostname()
}
