// workers 子命令（workers 凭据动词见 workers_credential.go）。
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
		cli.UsageError(workersGroupUsage, fmt.Errorf("unknown workers subcommand: %s", args[0]))
	}
}

func workersList(args []string) {
	p, help := mustParse(args, workersListSpec)
	if help {
		return
	}
	format, err := p.outputFormat()
	if err != nil {
		cli.UsageError(workersListUsage, err)
	}
	var items []map[string]any
	if err := json.Unmarshal(cpapi.Do("GET", "/workers", nil), &items); err != nil {
		cli.Fatal("parse workers response: %v", err)
	}
	if format == "json" {
		cli.PrintJSON(items)
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
			cli.Dash(cli.StrOf(wk["worker_id"])), cli.Dash(cli.StrOf(wk["hostname"])),
			cli.Dash(cli.StrOf(wk["mac"])), cli.Dash(cli.StrOf(wk["os"])), cli.Dash(cli.StrOf(wk["state"])))
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
		cli.UsageError(workersGetUsage, fmt.Errorf("worker id must not be empty"))
	}
	var out map[string]any
	if err := json.Unmarshal(cpapi.Do("GET", "/workers/"+url.PathEscape(workerID), nil), &out); err != nil {
		cli.Fatal("parse worker response: %v", err)
	}
	cli.PrintJSON(out)
}
