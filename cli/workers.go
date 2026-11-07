// workers 子命令（workers 凭据动词见 workers_credential.go）。
package main

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
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
