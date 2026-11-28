// host.go：kurrent host 命令组（只读宿主基线检查）。
package command

import (
	"fmt"
	"kurrent/cli/internal/cli"
	"kurrent/cli/internal/deploy"
	"kurrent/cli/internal/hostcheck"
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
		cli.UsageError(hostGroupUsage, fmt.Errorf("unknown host subcommand: %s", args[0]))
	}
}

func hostCheckCmd(args []string) {
	p, help := mustParse(args, hostCheckSpec)
	if help {
		return
	}
	root, err := deploy.ResolveRepoRoot(p.opt("dir", ""))
	if err != nil {
		cli.Fatal("%v", err)
	}
	env := hostcheck.HostEnvPaths
	stateDirFlag := p.opt("state-dir", "")
	q, qerr := deploy.ResolveDeployment(root, stateDirFlag)
	rec, recErr := deploy.ReadDeploymentRecord()
	env.DeploymentChecked = true
	env.Deployment = rec
	if recErr != nil {
		env.DeploymentErr = recErr.Error()
	}
	switch {
	case qerr != nil:
		env.DeploymentErr = qerr.Error()
		env.ControlStateDir = filepath.Join(root, "control_plane", "state")
		env.StateRoot = root
	case rec == nil && stateDirFlag == "" && strings.TrimSpace(os.Getenv("KURRENT_STATE_DIR")) == "":
		// 旧布局（无记录、未指定状态根）：按仓库内状态检查，并把“状态在仓库内”报为失败
		env.ControlStateDir = filepath.Join(root, "control_plane", "state")
		env.StateRoot = root
	default:
		env.ControlStateDir = q.ControlPlane().State
		env.StateRoot = q.StateDir
	}

	var role hostcheck.HostRole
	if raw := p.opt("role", ""); raw != "" {
		role, err = hostcheck.ParseHostRole(raw)
		if err != nil {
			cli.UsageError(hostCheckUsage, err)
		}
	} else {
		role, err = hostcheck.InferHostRole(env)
		if err != nil {
			cli.Fatal("%v", err)
		}
	}

	format := p.opt("output", "text")
	if format != "text" && format != "json" {
		cli.UsageError(hostCheckUsage, fmt.Errorf("invalid output format %q (expected text|json)", format))
	}

	report := hostcheck.RunHostChecks(env, root, role)
	report.Strict = p.flags["strict"]
	if err := hostcheck.PrintHostReport(os.Stdout, report, format == "json"); err != nil {
		cli.Fatal("write report: %v", err)
	}
	if report.Blocks(report.Strict) {
		cli.Exit(1)
	}
}
