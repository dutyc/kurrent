// sessions 子命令：列出 / 逐个吊销 / 全部踢下线管理面会话。
// 会话登记在控制面 state/sessions.yml（key = sha256(jti)）：删条目即下一请求失效；
// revoke-all 同时递增 epoch，旧 Cookie 即使被重放也失效。
package main

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"strings"
	"text/tabwriter"
)

type sessionInfo struct {
	SessionID string   `json:"session_id"`
	User      string   `json:"user"`
	Groups    []string `json:"groups"`
	CreatedAt string   `json:"created_at"`
	LastSeen  string   `json:"last_seen"`
	ExpiresAt string   `json:"expires_at"`
	CertFP    string   `json:"cert_fp"`
}

func cmdSessions(args []string) {
	if len(args) < 1 {
		sessionsUsage()
	}
	switch args[0] {
	case "list":
		sessionsList(args[1:])
	case "revoke":
		sessionsRevoke(args[1:])
	case "revoke-all":
		sessionsRevokeAll()
	default:
		sessionsUsage()
	}
}

func sessionsUsage() {
	fatal("用法: kurrent sessions list [-o table|json]   列出未过期会话（管理员）\n" +
		"      kurrent sessions revoke <session_id>     逐个吊销：删登记，下一请求立即失效\n" +
		"      kurrent sessions revoke-all              全部踢下线：清空登记并递增 epoch")
}

func sessionsList(args []string) {
	p := parseArgs(args)
	var items []sessionInfo
	if err := json.Unmarshal(api("GET", "/sessions", nil), &items); err != nil {
		fatal("解析会话列表: %v", err)
	}
	if p.opt("o", "table") == "json" {
		printJSON(items)
		return
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 4, 2, ' ', 0)
	fmt.Fprintln(w, "SESSION_ID\tUSER\tGROUPS\tCREATED\tLAST_SEEN\tEXPIRES")
	for _, s := range items {
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%s\n",
			s.SessionID, s.User, strings.Join(s.Groups, ","),
			shortTS(s.CreatedAt), shortTS(s.LastSeen), shortTS(s.ExpiresAt))
	}
	w.Flush()
}

func sessionsRevoke(args []string) {
	id := firstPos(parseArgs(args))
	if id == "" {
		fatal("用法: kurrent sessions revoke <session_id>（kurrent sessions list 查看）")
	}
	var out map[string]any
	if err := json.Unmarshal(api("DELETE", "/sessions/"+url.PathEscape(id), nil), &out); err != nil {
		fatal("解析响应: %v", err)
	}
	fmt.Printf("session %s revoked（登记已删除，下一请求 401）\n", id)
}

func sessionsRevokeAll() {
	var out struct {
		Revoked int `json:"revoked"`
	}
	if err := json.Unmarshal(api("DELETE", "/sessions", nil), &out); err != nil {
		fatal("解析响应: %v", err)
	}
	fmt.Printf("%d 个会话已全部下线（epoch 已递增，旧 Cookie 重放同样失效）\n", out.Revoked)
}
