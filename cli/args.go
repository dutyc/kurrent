// 子命令参数解析：按命令声明白名单严格解析。
//
// 规则：
//   - 位置参数与 --key value / --key=value / --flag 任意混排（go flag 遇第一个
//     位置参数即停止，不满足混排习惯）；
//   - 未知标志、缺值、重复标志、多余/不足位置参数一律报用法错误（退出码 2），
//     绝不静默忽略——例如 --agent-id 拼错不能退化成“自动生成身份并签发”；
//   - 布尔标志不吞下一个 token；--flag=false 显式关闭，--flag value 报错；
//   - -- 之后全部按位置参数处理（值以 - 开头时用 --key=value 或 -- 分隔）；
//   - -h/--help 在任意位置优先，打印该命令用法并返回 0，不执行任何副作用。
package main

import (
	"errors"
	"fmt"
	"strings"
)

type flagKind int

const (
	flagString flagKind = iota
	flagBool
)

type flagSpec struct {
	name     string   // 规范名（不带 --）
	aliases  []string // 短名/别名（如 o 对应 output）
	kind     flagKind
	required bool
}

type cmdSpec struct {
	usage    string
	flags    []flagSpec
	minPos   int
	maxPos   int
	posNames []string
}

type parsedArgs struct {
	pos   []string
	opts  map[string]string
	flags map[string]bool
	set   map[string]bool
}

var errHelp = errors.New("help requested")

// 全局标志名：子命令里出现时给出“必须放在命令组之前”的明确提示，而不是泛泛的未知标志。
var globalFlagNames = map[string]string{
	"server":      "--server",
	"credentials": "--credentials",
	"token":       "--token",
}

func parseCommand(args []string, spec cmdSpec) (parsedArgs, error) {
	p := parsedArgs{opts: map[string]string{}, flags: map[string]bool{}, set: map[string]bool{}}
	for _, a := range args {
		if a == "--" {
			break
		}
		if a == "-h" || a == "--help" {
			return p, errHelp
		}
	}

	known := make(map[string]flagSpec, len(spec.flags)*2)
	for _, f := range spec.flags {
		known[f.name] = f
		for _, alias := range f.aliases {
			known[alias] = f
		}
	}

	positionalOnly := false
	for i := 0; i < len(args); i++ {
		a := args[i]
		if positionalOnly {
			p.pos = append(p.pos, a)
			continue
		}
		if a == "--" {
			positionalOnly = true
			continue
		}
		if !strings.HasPrefix(a, "-") || a == "-" {
			p.pos = append(p.pos, a)
			continue
		}
		raw := strings.TrimLeft(a, "-")
		if raw == "" {
			return p, fmt.Errorf("invalid flag %q", a)
		}
		name, value, hasValue := strings.Cut(raw, "=")
		if name == "h" || name == "help" {
			return p, errHelp
		}
		f, ok := known[name]
		if !ok {
			if global, isGlobal := globalFlagNames[name]; isGlobal {
				return p, fmt.Errorf("global flag %s must precede the command group", global)
			}
			if name == "secret" {
				return p, fmt.Errorf("--secret does not accept plaintext on the command line (it leaks into the process list and shell history); use --secret-file PATH or --secret-stdin")
			}
			return p, fmt.Errorf("unknown flag: %s", flagLabel(a, name))
		}
		if p.set[f.name] {
			return p, fmt.Errorf("flag %s specified more than once", flagLabel(a, f.name))
		}
		p.set[f.name] = true

		switch f.kind {
		case flagBool:
			if hasValue {
				if value != "true" && value != "false" {
					return p, fmt.Errorf("%s accepts only true or false (got %q)", flagLabel(a, f.name), value)
				}
				p.flags[f.name] = value == "true"
				continue
			}
			if i+1 < len(args) && (args[i+1] == "true" || args[i+1] == "false") {
				return p, fmt.Errorf("boolean flag %s does not take a separate value; use %s=%s",
					flagLabel(a, f.name), flagLabel(a, f.name), args[i+1])
			}
			p.flags[f.name] = true
		case flagString:
			if hasValue {
				p.opts[f.name] = value
				continue
			}
			if i+1 >= len(args) || (strings.HasPrefix(args[i+1], "-") && args[i+1] != "-") {
				return p, fmt.Errorf("flag %s requires a value", flagLabel(a, f.name))
			}
			i++
			p.opts[f.name] = args[i]
		}
	}

	for _, f := range spec.flags {
		if !f.required {
			continue
		}
		if !p.set[f.name] {
			return p, fmt.Errorf("missing required flag: --%s", f.name)
		}
		if f.kind == flagString && strings.TrimSpace(p.opts[f.name]) == "" {
			return p, fmt.Errorf("flag --%s must not be empty", f.name)
		}
	}
	if len(p.pos) < spec.minPos {
		if missing := spec.minPos - len(p.pos); missing == 1 && len(spec.posNames) >= spec.minPos {
			return p, fmt.Errorf("missing argument <%s>", spec.posNames[len(p.pos)])
		}
		return p, fmt.Errorf("expected %d positional arguments, got %d", spec.minPos, len(p.pos))
	}
	if len(p.pos) > spec.maxPos {
		return p, fmt.Errorf("unexpected positional argument: %s", p.pos[spec.maxPos])
	}
	return p, nil
}

// mustParse 解析并处理帮助/用法错误：帮助打印到 stdout 并返回 help=true；
// 用法错误打印到 stderr、退出码 2。调用方拿到 help=true 必须直接返回。
func mustParse(args []string, spec cmdSpec) (parsedArgs, bool) {
	p, err := parseCommand(args, spec)
	if errors.Is(err, errHelp) {
		fmt.Print(spec.usage)
		return parsedArgs{}, true
	}
	if err != nil {
		usageError(spec.usage, err)
	}
	return p, false
}

func flagLabel(raw, canonical string) string {
	if strings.HasPrefix(raw, "--") {
		return "--" + canonical
	}
	return "-" + canonical
}

func (p parsedArgs) opt(key, def string) string {
	if v, ok := p.opts[key]; ok {
		return v
	}
	return def
}

func (p parsedArgs) bool(key string) bool {
	return p.flags[key]
}

// has 表示该标志是否显式出现（布尔 false 也算出现）。
func (p parsedArgs) has(key string) bool {
	return p.set[key]
}

// outputFormat 统一 -o/--output：只允许 table|json。
func (p parsedArgs) outputFormat() (string, error) {
	v := p.opt("output", "table")
	if v != "table" && v != "json" {
		return "", fmt.Errorf("--output accepts only table or json (got %q)", v)
	}
	return v, nil
}
