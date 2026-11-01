// workers / ops 子命令（workers 凭据动词见 workers_credential.go）。
package main

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
	"text/tabwriter"
)

var (
	workersListSpec = cmdSpec{
		usage:  workersListUsage,
		flags:  []flagSpec{{name: "output", aliases: []string{"o"}, kind: flagString}},
		maxPos: 0,
	}
	workersGetSpec = cmdSpec{usage: workersGetUsage, minPos: 1, maxPos: 1, posNames: []string{"id"}}
	opsListSpec    = cmdSpec{
		usage: opsListUsage,
		flags: []flagSpec{
			{name: "limit", kind: flagString},
			{name: "output", aliases: []string{"o"}, kind: flagString},
		},
		maxPos: 0,
	}
)

// ── workers ──

func cmdWorkers(args []string) {
	if groupHelp("workers", args, workersGroupUsage) {
		return
	}
	switch args[0] {
	case "list":
		workersList(args[1:])
	case "get":
		workersGet(args[1:])
	case "credential":
		workersCredential(args[1:])
	default:
		usageError(workersGroupUsage, fmt.Errorf("unknown workers subcommand: %s", args[0]))
	}
}

func workersList(args []string) {
	p, help := mustParse(args, workersListSpec)
	if help {
		return
	}
	format, err := p.outputFormat()
	if err != nil {
		usageError(workersListUsage, err)
	}
	var items []map[string]any
	if err := json.Unmarshal(api("GET", "/workers", nil), &items); err != nil {
		fatal("parse workers response: %v", err)
	}
	if format == "json" {
		printJSON(items)
		return
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

func workersGet(args []string) {
	p, help := mustParse(args, workersGetSpec)
	if help {
		return
	}
	workerID := strings.TrimSpace(p.pos[0])
	if workerID == "" {
		usageError(workersGetUsage, fmt.Errorf("worker id must not be empty"))
	}
	var out map[string]any
	if err := json.Unmarshal(api("GET", "/workers/"+url.PathEscape(workerID), nil), &out); err != nil {
		fatal("parse worker response: %v", err)
	}
	printJSON(out)
}

// ── ops ──

func cmdOps(args []string) {
	if groupHelp("ops", args, opsGroupUsage) {
		return
	}
	if args[0] != "list" {
		usageError(opsGroupUsage, fmt.Errorf("unknown ops subcommand: %s", args[0]))
	}
	opsList(args[1:])
}

func opsList(args []string) {
	p, help := mustParse(args, opsListSpec)
	if help {
		return
	}
	format, err := p.outputFormat()
	if err != nil {
		usageError(opsListUsage, err)
	}
	limit := 50
	if p.has("limit") {
		raw := strings.TrimSpace(p.opt("limit", ""))
		limit, err = strconv.Atoi(raw)
		if err != nil || limit <= 0 {
			usageError(opsListUsage, fmt.Errorf("--limit must be a positive integer (got %q)", raw))
		}
	}

	var out struct {
		Entries []map[string]any `json:"entries"`
	}
	if err := json.Unmarshal(api("GET", "/operations?limit="+strconv.Itoa(limit), nil), &out); err != nil {
		fatal("parse operations response: %v", err)
	}
	if format == "json" {
		printJSON(out.Entries)
		return
	}
	if len(out.Entries) == 0 {
		fmt.Println("(no operation records)")
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
