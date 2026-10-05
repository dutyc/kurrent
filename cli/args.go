// 子命令参数解析：位置参数与 --key value / --key=value / --flag 任意混排
// （Go flag 包遇第一个位置参数即停止解析，不满足混排习惯）。
package main

import "strings"

type parsedArgs struct {
	pos   []string
	opts  map[string]string
	flags map[string]bool
}

func parseArgs(args []string) parsedArgs {
	p := parsedArgs{opts: map[string]string{}, flags: map[string]bool{}}
	for i := 0; i < len(args); i++ {
		a := args[i]
		if strings.HasPrefix(a, "-") && len(a) > 1 && a != "-" {
			key := strings.TrimLeft(a, "-")
			if j := strings.IndexByte(key, '='); j >= 0 {
				p.opts[key[:j]] = key[j+1:]
			} else if i+1 < len(args) && !strings.HasPrefix(args[i+1], "-") {
				p.opts[key] = args[i+1]
				i++
			} else {
				p.flags[key] = true
			}
			continue
		}
		p.pos = append(p.pos, a)
	}
	return p
}

func (p parsedArgs) opt(key, def string) string {
	if v, ok := p.opts[key]; ok {
		return v
	}
	return def
}

func (p parsedArgs) has(key string) bool {
	return p.flags[key] || p.opts[key] != ""
}
