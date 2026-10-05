// token 子命令：签发 / 列表 / 吊销凭据。
// usage=bootstrap（默认）：集群级通用引导 token——不绑节点（节点身份由 join 自动生成，
// 随机短标识；人工输入归属 metadata.remark）+ enroll 自动登记；TTL 内可被多次 enroll 复用。
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
	"strings"
	"text/tabwriter"
)

type tokenResp struct {
	Token     string   `json:"token"`
	ExpiresAt string   `json:"expires_at"`
	Usage     []string `json:"usage"`
	Groups    []string `json:"groups"`
}

// tokenInfo：GET /pki/tokens 列表条目（与后端字段一一对应）
type tokenInfo struct {
	TokenID   string   `json:"token_id"`
	Usage     []string `json:"usage"`
	Groups    []string `json:"groups"`
	Component string   `json:"component"`
	AgentID   string   `json:"agent_id"`
	Name      string   `json:"name"`
	CreatedAt string   `json:"created_at"`
	ExpiresAt string   `json:"expires_at"`
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
	fatal("用法: kurrent token create [--usage bootstrap|admin] [--cp-url URL]\n" +
		"      kurrent token list [-o table|json]   列出未过期 token（台账可视，不返回明文）\n" +
		"      kurrent token revoke <token_id>      吊销：删条目即失效\n" +
		"      create: bootstrap（默认）集群级通用引导凭据，不绑节点、TTL 内可复用；\n" +
		"      --cp-url 指定节点侧可达的控制面 HTTPS 入口\n" +
		"      以输出可直接粘贴的 join 命令（缺省按 --server 主机推导 https://<host>）\n" +
		"      admin——管理 token（认证器链 Bearer：脚本调用 / WebUI 登录粘贴）")
}

func tokenCreate(args []string) {
	p := parseArgs(args)
	usage := p.opt("usage", "bootstrap")
	if usage != "bootstrap" && usage != "admin" {
		fatal("--usage 仅支持 bootstrap|admin（收到 %q）", usage)
	}
	var body any
	if usage == "admin" {
		body = map[string]string{"usage": "admin"}
	}
	var out tokenResp
	if err := json.Unmarshal(api("POST", "/pki/tokens", body), &out); err != nil {
		fatal("解析 token 响应: %v", err)
	}
	if usage == "admin" {
		fmt.Printf("admin token: %s（expires %s）\n", out.Token, out.ExpiresAt)
		fmt.Printf("\n# 用途（管理凭据，等同 admin.conf 证书身份）：\n")
		fmt.Printf("#   CLI：kurrent --server <CP> --token %s agents list\n", out.Token)
		fmt.Printf("#   WebUI：登录页粘贴；脚本：curl -H \"Authorization: Bearer <token>\"\n")
		fmt.Printf("#   吊销：kurrent token revoke %s（或 WebUI「凭据」页）\n", strings.SplitN(out.Token, ".", 2)[0])
		return
	}
	fmt.Printf("bootstrap token: %s（expires %s，TTL 内可被多次 enroll 复用）\n", out.Token, out.ExpiresAt)
	fmt.Printf("\n# 存储节点上执行（命令已携带控制面地址，节点执行即自动生成/更新声明）：\n")
	fmt.Printf("kurrent join %s --token %s\n", joinCpURL(p.opt("cp-url", "")), out.Token)
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
	fmt.Fprintln(w, "TOKEN_ID\tUSAGE\tGROUPS\tCOMPONENT-AGENT\tNAME\tEXPIRES\tCREATED")
	for _, t := range items {
		owner := t.Component
		if t.AgentID != "" {
			owner = t.Component + "/" + t.AgentID
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
			t.TokenID, strings.Join(t.Usage, ","), strings.Join(t.Groups, ","),
			owner, dashIfEmpty(t.Name), shortTS(t.ExpiresAt), shortTS(t.CreatedAt))
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
