// kurrent —— Kurrent 控制面 CLI（进程入口）。
//
// 命令实现按域位于 internal/command；本文件只负责把发版注入的版本号交给命令层。
package main

import (
	"os"

	"kurrent/cli/internal/command"
)

var version = "dev"

func main() {
	command.Version = version
	command.Main(os.Args[1:])
}
