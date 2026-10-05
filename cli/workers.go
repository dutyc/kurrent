// workers / ops 子命令（workers 凭据动词见 workers_credential.go）。
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"text/tabwriter"
)

// ── workers ──

func cmdWorkers(args []string) {
	if len(args) == 0 || args[0] == "list" {
		workersList()
		return
	}
	if args[0] == "get" && len(args) == 2 {
		workersGet(args[1])
		return
	}
	if args[0] == "credential" {
		workersCredential(args[1:])
		return
	}
	fatal("用法: kurrent workers list | workers get <id> | workers credential show|set|revoke <id>")
}

func workersList() {
	var items []map[string]any
	if err := json.Unmarshal(api("GET", "/workers", nil), &items); err != nil {
		fatal("解析 workers 响应: %v", err)
	}
	if len(items) == 0 {
		fmt.Println("(no workers)")
		return
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 4, 2, ' ', 0)
	fmt.Fprintln(w, "ID\tHOSTNAME\tMAC\tOS\tSTATE")
	for _, wk := range items {
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\n",
			dash(strOf(wk["worker_id"])), dash(strOf(wk["hostname"])),
			dash(strOf(wk["mac"])), dash(strOf(wk["os"])), dash(strOf(wk["state"])))
	}
	w.Flush()
}

func workersGet(workerID string) {
	var out map[string]any
	if err := json.Unmarshal(api("GET", "/workers/"+workerID, nil), &out); err != nil {
		fatal("解析 worker 响应: %v", err)
	}
	printJSON(out)
}

// ── ops ──

func cmdOps(args []string) {
	p := parseArgs(args)
	limit := p.opt("limit", "50")
	if _, err := strconv.Atoi(limit); err != nil {
		fatal("无效 limit: %s", limit)
	}

	var out struct {
		Entries []map[string]any `json:"entries"`
	}
	if err := json.Unmarshal(api("GET", "/operations?limit="+limit, nil), &out); err != nil {
		fatal("解析 operations 响应: %v", err)
	}
	if p.opt("o", "table") == "json" {
		printJSON(out.Entries)
		return
	}
	if len(out.Entries) == 0 {
		fmt.Println("(no operations)")
		return
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 4, 2, ' ', 0)
	fmt.Fprintln(w, "ID\tTS\tOP\tSTATUS\tDETAIL")
	for _, e := range out.Entries {
		detail := opDetail(e)
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\n",
			strOf(e["id"]), strOf(e["ts"]), strOf(e["op"]), strOf(e["status"]), detail)
	}
	w.Flush()
}

// opDetail 拼审计条目里的业务字段（agent/mac/error 等）。
func opDetail(e map[string]any) string {
	var parts []string
	for _, key := range []string{"agent", "mac", "worker", "error", "iqn"} {
		if v := strOf(e[key]); v != "" {
			parts = append(parts, key+"="+v)
		}
	}
	return strings.Join(parts, " ")
}

// dash 空值显示占位符（表格对齐）。
func dash(s string) string {
	if s == "" {
		return "—"
	}
	return s
}

func strOf(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case float64:
		return strconv.FormatFloat(t, 'f', -1, 64)
	default:
		b, err := json.Marshal(t)
		if err != nil {
			return ""
		}
		return string(b)
	}
}
