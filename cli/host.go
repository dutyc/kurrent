// host.go：kurrent host 命令组（只读宿主基线检查）。
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

const hostGroupUsage = `Usage:
  kurrent host check [--role control|storage|all] [--dir REPO] [-o text|json] [--strict]

  Runs the read-only host baseline locally; it does not call the management API.
  Exit codes: 0 all checks pass, 1 FAIL (or SKIP with --strict), 2 usage error.
`

const hostCheckUsage = `Usage: kurrent host check [--role control|storage|all] [--dir REPO] [--state-dir PATH]
                       [-o text|json] [--strict]

  --role       Role to check (default: inferred from running compose services)
  --dir        Repository root (default: discovered from the working directory)
  --state-dir  State root to inspect (default: deployment record, then KURRENT_STATE_DIR)
  -o           Output format: text (default) or json
  --strict     Treat SKIP as a failure (for unattended checks)

  Read-only: no system state is modified. Storage checks (kernel modules, configfs,
  tlshd) are the same ones kurrent join preflights; complete them with
  storager/nvmeof/host-prepare.sh.
`

var hostCheckSpec = cmdSpec{
	usage: hostCheckUsage,
	flags: []flagSpec{
		{name: "role", kind: flagString},
		{name: "dir", kind: flagString},
		{name: "state-dir", kind: flagString},
		{name: "output", aliases: []string{"o"}, kind: flagString},
		{name: "strict", kind: flagBool},
	},
}

func cmdHost(args []string) {
	if groupHelp("host", args, hostGroupUsage) {
		return
	}
	switch args[0] {
	case "check":
		hostCheckCmd(args[1:])
	default:
		usageError(hostGroupUsage, fmt.Errorf("unknown host subcommand: %s", args[0]))
	}
}

func hostCheckCmd(args []string) {
	p, help := mustParse(args, hostCheckSpec)
	if help {
		return
	}
	root, err := resolveRepoRoot(p.opt("dir", ""))
	if err != nil {
		fatal("%v", err)
	}
	env := hostEnvPaths
	stateDirFlag := p.opt("state-dir", "")
	q, qerr := resolveDeployment(root, stateDirFlag)
	rec, recErr := readDeploymentRecord()
	env.deploymentChecked = true
	env.deployment = rec
	if recErr != nil {
		env.deploymentErr = recErr.Error()
	}
	switch {
	case qerr != nil:
		env.deploymentErr = qerr.Error()
		env.controlStateDir = filepath.Join(root, "control_plane", "state")
		env.stateRoot = root
	case rec == nil && stateDirFlag == "" && strings.TrimSpace(os.Getenv("KURRENT_STATE_DIR")) == "":
		// 旧布局（无记录、未指定状态根）：按仓库内状态检查，并把“状态在仓库内”报为失败
		env.controlStateDir = filepath.Join(root, "control_plane", "state")
		env.stateRoot = root
	default:
		env.controlStateDir = q.controlPlane().state
		env.stateRoot = q.stateDir
	}

	var role hostRole
	if raw := p.opt("role", ""); raw != "" {
		role, err = parseHostRole(raw)
		if err != nil {
			usageError(hostCheckUsage, err)
		}
	} else {
		role, err = inferHostRole(env)
		if err != nil {
			fatal("%v", err)
		}
	}

	format := p.opt("output", "text")
	if format != "text" && format != "json" {
		usageError(hostCheckUsage, fmt.Errorf("invalid output format %q (expected text|json)", format))
	}

	report := runHostChecks(env, root, role)
	report.Strict = p.flags["strict"]
	if err := printHostReport(os.Stdout, report, format == "json"); err != nil {
		fatal("write report: %v", err)
	}
	if report.blocks(report.Strict) {
		exit(1)
	}
}
