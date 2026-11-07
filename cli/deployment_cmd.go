// deployment 子命令：只读展示本机部署身份（记录、仓库/状态根、凭据路径、compose 变量）。
//
// kurrent deployment show [-o yaml|env]
// env 输出可直接用于手工 docker compose（状态根绝对路径是挂载源）。
package main

import (
	"fmt"
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
		usageError(deploymentGroupUsage, fmt.Errorf("unknown deployment subcommand: %s", args[0]))
	}
}

func deploymentShow(args []string) {
	p, help := mustParse(args, deploymentShowSpec)
	if help {
		return
	}
	format := p.opt("output", "yaml")
	if format != "yaml" && format != "env" {
		usageError(deploymentShowUsage, fmt.Errorf("--output must be yaml or env: %s", format))
	}
	q, err := resolveDeployment("", "")
	if err != nil {
		fatal("%v", err)
	}
	switch format {
	case "yaml":
		rec := deploymentRecord{
			Project:        q.project(),
			StorageProject: q.storageProject(),
			RepoRoot:       q.repoRoot,
			StateDir:       q.stateDir,
			Status:         deployStatusReady,
		}
		if q.record != nil {
			rec = *q.record
		}
		rec.APIVersion = deploymentAPIVersion
		rec.Kind = deploymentKind
		body, err := yaml.Marshal(rec)
		if err != nil {
			fatal("serialize deployment: %v", err)
		}
		fmt.Print(string(body))
		if creds := discoverCredentials(); creds != "" {
			fmt.Printf("# management credentials: %s\n", creds)
		}
	case "env":
		node := q.storager("")
		env := q.composeEnv()
		env = append(env,
			"KURRENT_NODE_STATE_DIR="+node.root,
			"KURRENT_CP_CA_FILE="+node.cpCA,
			"KURRENT_DEPLOYMENT_FILE="+deploymentRecordPath(),
		)
		if id, ok, err := readNodeID(filepath.Join(node.state, "agent.id")); err == nil && ok {
			env = append(env, "KURRENT_AGENT_ID="+id)
		}
		if data, err := os.ReadFile(filepath.Join(q.repoRoot, "storager", "kurrent.yaml")); err == nil {
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
