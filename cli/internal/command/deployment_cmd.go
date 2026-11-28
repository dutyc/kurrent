// deployment 子命令：只读展示本机部署身份（记录、仓库/状态根、凭据路径、compose 变量）。
//
// kurrent deployment show [-o yaml|env]
// env 输出可直接用于手工 docker compose（状态根绝对路径是挂载源）。
package command

import (
	"fmt"
	"kurrent/cli/internal/cli"
	"kurrent/cli/internal/creds"
	"kurrent/cli/internal/deploy"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

var deploymentShowSpec = cmdSpec{
	usage:  deploymentShowUsage,
	flags:  []flagSpec{{name: "output", aliases: []string{"o"}, kind: flagString}},
	maxPos: 0,
}

func cmdDeployment(args []string) {
	if groupHelp("deployment", args, deploymentGroupUsage) {
		return
	}
	switch args[0] {
	case "show":
		deploymentShow(args[1:])
	default:
		cli.UsageError(deploymentGroupUsage, fmt.Errorf("unknown deployment subcommand: %s", args[0]))
	}
}

func deploymentShow(args []string) {
	p, help := mustParse(args, deploymentShowSpec)
	if help {
		return
	}
	format := p.opt("output", "yaml")
	if format != "yaml" && format != "env" {
		cli.UsageError(deploymentShowUsage, fmt.Errorf("--output must be yaml or env: %s", format))
	}
	q, err := deploy.ResolveDeployment("", "")
	if err != nil {
		cli.Fatal("%v", err)
	}
	switch format {
	case "yaml":
		rec := deploy.DeploymentRecord{
			Project:        q.Project(),
			StorageProject: q.StorageProject(),
			RepoRoot:       q.RepoRoot,
			StateDir:       q.StateDir,
			Status:         deploy.StatusReady,
		}
		if q.Record != nil {
			rec = *q.Record
		}
		rec.APIVersion = deploy.DeploymentAPIVersion
		rec.Kind = deploy.DeploymentKind
		body, err := yaml.Marshal(rec)
		if err != nil {
			cli.Fatal("serialize deployment: %v", err)
		}
		fmt.Print(string(body))
		if creds := creds.DiscoverCredentials(); creds != "" {
			fmt.Printf("# management credentials: %s\n", creds)
		}
	case "env":
		node := q.Storager("")
		env := q.ComposeEnv()
		env = append(env,
			"KURRENT_NODE_STATE_DIR="+node.Root,
			"KURRENT_CP_CA_FILE="+node.CPCA,
			"KURRENT_DEPLOYMENT_FILE="+deploy.DeploymentRecordPath(),
		)
		if id, ok, err := readNodeID(filepath.Join(node.State, "agent.id")); err == nil && ok {
			env = append(env, "KURRENT_AGENT_ID="+id)
		}
		if data, err := os.ReadFile(filepath.Join(q.RepoRoot, "storager", "kurrent.yaml")); err == nil {
			if diskDir := strings.TrimSpace(nodeValueFromYAML(data, "spec", "agent", "diskDir")); diskDir != "" {
				env = append(env, "KURRENT_DISK_DIR="+diskDir)
			}
		}
		for _, kv := range env {
			fmt.Println("export " + shellQuoteAssignment(kv))
		}
	}
}

// shellQuoteAssignment 把 KEY=VALUE 渲染为 shell 单引号赋值（单引号内无变量展开）。
func shellQuoteAssignment(kv string) string {
	key, value, ok := strings.Cut(kv, "=")
	if !ok {
		return "'" + kv + "'"
	}
	return key + "='" + strings.ReplaceAll(value, "'", `'\''`) + "'"
}
