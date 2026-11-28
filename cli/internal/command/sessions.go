// sessions 子命令：列出 / 逐个吊销 / 全部踢下线管理面会话。
// 会话登记在控制面 state/sessions.yml（key = sha256(jti)）：删条目即下一请求失效；
// revoke-all 同时递增 epoch，旧 Cookie 即使被重放也失效。
package command

import (
	"encoding/json"
	"fmt"
	"kurrent/cli/internal/cli"
	"kurrent/cli/internal/cpapi"
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

var (
	sessionsListSpec = cmdSpec{
		usage:  sessionsListUsage,
		flags:  []flagSpec{{name: "output", aliases: []string{"o"}, kind: flagString}},
		maxPos: 0,
	}
	sessionsRevokeSpec    = cmdSpec{usage: sessionsRevokeUsage, minPos: 1, maxPos: 1, posNames: []string{"session_id"}}
	sessionsRevokeAllSpec = cmdSpec{usage: sessionsRevokeAllUsage, maxPos: 0}
)

func cmdSessions(args []string) {
	if groupHelp("sessions", args, sessionsGroupUsage) {
		return
	}
	switch args[0] {
	case "list":
		sessionsList(args[1:])
	case "revoke":
		sessionsRevoke(args[1:])
	case "revoke-all":
		sessionsRevokeAll(args[1:])
	default:
		cli.UsageError(sessionsGroupUsage, fmt.Errorf("unknown sessions subcommand: %s", args[0]))
	}
}

func sessionsList(args []string) {
	p, help := mustParse(args, sessionsListSpec)
	if help {
		return
	}
	format, err := p.outputFormat()
	if err != nil {
		cli.UsageError(sessionsListUsage, err)
	}
	var items []sessionInfo
	if err := json.Unmarshal(cpapi.Do("GET", "/sessions", nil), &items); err != nil {
		cli.Fatal("parse session list: %v", err)
	}
	if format == "json" {
		cli.PrintJSON(items)
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
	p, help := mustParse(args, sessionsRevokeSpec)
	if help {
		return
	}
	id := strings.TrimSpace(p.pos[0])
	if id == "" {
		cli.UsageError(sessionsRevokeUsage, fmt.Errorf("session_id must not be empty"))
	}
	var out map[string]any
	if err := json.Unmarshal(cpapi.Do("DELETE", "/sessions/"+url.PathEscape(id), nil), &out); err != nil {
		cli.Fatal("parse response: %v", err)
	}
	fmt.Printf("session %s revoked (registration deleted; the next request fails with 401)\n", id)
}

func sessionsRevokeAll(args []string) {
	if _, help := mustParse(args, sessionsRevokeAllSpec); help {
		return
	}
	var out struct {
		Revoked int `json:"revoked"`
	}
	if err := json.Unmarshal(cpapi.Do("DELETE", "/sessions", nil), &out); err != nil {
		cli.Fatal("parse response: %v", err)
	}
	fmt.Printf("%d session(s) signed out (epoch bumped; replayed old cookies fail too)\n", out.Revoked)
}
