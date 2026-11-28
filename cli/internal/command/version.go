// Version 由进程入口注入（cli/main.go 的 main.version，发版经 -ldflags 注入）；
// 日常构建未注入时显示 dev。
package command

import "fmt"

var Version = "dev"

var versionSpec = cmdSpec{usage: versionUsage, maxPos: 0}

func cmdVersion(args []string) {
	if _, help := mustParse(args, versionSpec); help {
		return
	}
	fmt.Printf("kurrent %s\n", Version)
}
