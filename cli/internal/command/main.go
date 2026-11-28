// 命令分发与全局标志解析（进程入口在 cli/main.go）。
package command

import (
	"errors"
	"fmt"
	"os"
	"strings"

	"kurrent/cli/internal/cli"
	"kurrent/cli/internal/validate"
)

func Main(args []string) {
	cli.Server = strings.TrimSpace(os.Getenv("KURRENT_CP_URL"))
	cli.Token = strings.TrimSpace(os.Getenv("KURRENT_CP_TOKEN"))
	cli.Credentials = os.Getenv("KURRENT_CREDENTIALS")

	rest, err := parseGlobalFlags(args)
	if errors.Is(err, errHelp) {
		fmt.Print(usageText)
		return
	}
	if err != nil {
		cli.UsageError(usageText, err)
	}
	if len(rest) == 0 {
		fmt.Print(usageText)
		return
	}
	if rest[0] == "help" {
		helpCommand(rest[1:])
		return
	}
	switch rest[0] {
	case "agents":
		cmdAgents(rest[1:])
	case "token":
		cmdToken(rest[1:])
	case "sessions":
		cmdSessions(rest[1:])
	case "cert":
		cmdCert(rest[1:])
	case "credentials":
		cmdCredentials(rest[1:])
	case "config":
		cmdConfig(rest[1:])
	case "init":
		cmdInit(rest[1:])
	case "join":
		cmdJoin(rest[1:])
	case "deployment":
		cmdDeployment(rest[1:])
	case "host":
		cmdHost(rest[1:])
	case "workers":
		cmdWorkers(rest[1:])
	case "ops":
		cmdOps(rest[1:])
	case "version":
		cmdVersion(rest[1:])
	default:
		cli.UsageError(usageText, fmt.Errorf("unknown command group: %s", rest[0]))
	}
}

// parseGlobalFlags 解析命令组之前的全局标志；遇到第一个位置参数立即停止，
// 其余参数原样交给命令解析（全局标志写在其后会被子命令以明确提示拒绝）。
func parseGlobalFlags(args []string) ([]string, error) {
	seen := map[string]bool{}
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--" {
			return args[i+1:], nil
		}
		if !strings.HasPrefix(a, "-") || a == "-" {
			return args[i:], nil
		}
		raw := strings.TrimLeft(a, "-")
		if raw == "h" || raw == "help" {
			return nil, errHelp
		}
		name, value, hasValue := strings.Cut(raw, "=")
		switch name {
		case "server", "credentials", "token":
		default:
			return nil, fmt.Errorf("unknown global flag: %s", a)
		}
		if seen[name] {
			return nil, fmt.Errorf("global flag --%s specified more than once", name)
		}
		seen[name] = true
		if !hasValue {
			if i+1 >= len(args) || (strings.HasPrefix(args[i+1], "-") && args[i+1] != "-") {
				return nil, fmt.Errorf("global flag --%s requires a value", name)
			}
			i++
			value = args[i]
		}
		if strings.TrimSpace(value) == "" {
			return nil, fmt.Errorf("global flag --%s must not be empty", name)
		}
		switch name {
		case "server":
			validated, err := validate.ValidateHTTPURL("--server", value)
			if err != nil {
				return nil, err
			}
			cli.Server = validated
		case "credentials":
			cli.Credentials = value
		case "token":
			cli.Token = strings.TrimSpace(value)
		}
	}
	return nil, nil
}
