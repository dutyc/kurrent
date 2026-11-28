// ops 子命令：操作审计日志（只读）。
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"text/tabwriter"
)

var opsListSpec = cmdSpec{
	usage: opsListUsage,
	flags: []flagSpec{
		{name: "limit", kind: flagString},
		{name: "output", aliases: []string{"o"}, kind: flagString},
	},
	maxPos: 0,
}

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
