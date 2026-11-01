// kurrent —— Kurrent 控制面 CLI。
// 零第三方依赖（net/http + encoding/json + yaml.v3），go build 出单二进制。
//
// 管理凭据：自动发现管理凭据文件（control_plane/state/admin.conf → 可执行文件旁 →
// ~/.kurrent/admin.conf 用户级快照，admin 证书 mTLS；kurrent credentials install 安装快照）；
// 环境变量 KURRENT_CP_URL / KURRENT_CP_TOKEN / KURRENT_CREDENTIALS 兜底。
// 全局标志只在命令组之前解析；子命令使用严格白名单（未知/缺值/重复一律报用法错误）。
package main

import (
	"errors"
	"fmt"
	"os"
	"strings"
)

var (
	flagServer      string
	flagToken       string
	flagCredentials string
)

// exit 变量化：测试注入以验证“用法错误/帮助不触发副作用”；生产为 os.Exit。
var exit = os.Exit

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "kurrent: "+format+"\n", args...)
	exit(1)
}

// usageError 用法错误：错误信息 + 该命令用法，退出码 2（区别于运行时失败的 1）。
func usageError(usage string, err error) {
	fmt.Fprintf(os.Stderr, "kurrent: %v\n\n%s", err, strings.TrimRight(usage, "\n")+"\n")
	exit(2)
}

func main() {
	flagServer = strings.TrimSpace(os.Getenv("KURRENT_CP_URL"))
	flagToken = strings.TrimSpace(os.Getenv("KURRENT_CP_TOKEN"))
	flagCredentials = os.Getenv("KURRENT_CREDENTIALS")

	args, err := parseGlobalFlags(os.Args[1:])
	if errors.Is(err, errHelp) {
		fmt.Print(usageText)
		return
	}
	if err != nil {
		usageError(usageText, err)
	}
	if len(args) == 0 {
		fmt.Print(usageText)
		return
	}
	if args[0] == "help" {
		helpCommand(args[1:])
		return
	}
	switch args[0] {
	case "agents":
		cmdAgents(args[1:])
	case "token":
		cmdToken(args[1:])
	case "sessions":
		cmdSessions(args[1:])
	case "cert":
		cmdCert(args[1:])
	case "credentials":
		cmdCredentials(args[1:])
	case "config":
		cmdConfig(args[1:])
	case "init":
		cmdInit(args[1:])
	case "join":
		cmdJoin(args[1:])
	case "workers":
		cmdWorkers(args[1:])
	case "ops":
		cmdOps(args[1:])
	case "version":
		cmdVersion(args[1:])
	default:
		usageError(usageText, fmt.Errorf("unknown command group: %s", args[0]))
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
			validated, err := validateHTTPURL("--server", value)
			if err != nil {
				return nil, err
			}
			flagServer = validated
		case "credentials":
			flagCredentials = value
		case "token":
			flagToken = strings.TrimSpace(value)
		}
	}
	return nil, nil
}
