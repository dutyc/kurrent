// 版本号：发版构建时经 go build -ldflags "-X main.version=vX.Y.Z" 注入；
// 日常构建未注入时显示 dev。
package main

import "fmt"

var version = "dev"

var versionSpec = cmdSpec{usage: versionUsage, maxPos: 0}

func cmdVersion(args []string) {
	if _, help := mustParse(args, versionSpec); help {
		return
	}
	fmt.Printf("kurrent %s\n", version)
}
