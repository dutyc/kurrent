// CLI 进程级公共设施：退出与用法错误、全局标志、JSON 输出。
package cli

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

// 全局标志（命令组之前解析；server/token 也接受环境变量初始化）。
var (
	Server      string
	Token       string
	Credentials string
)

// Exit 变量化：测试注入以验证“用法错误/帮助不触发副作用”；生产为 os.Exit。
var Exit = os.Exit

// Fatal 运行时失败：打印错误并以退出码 1 结束。
func Fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "kurrent: "+format+"\n", args...)
	Exit(1)
}

// UsageError 用法错误：错误信息 + 该命令用法，退出码 2（区别于运行时失败的 1）。
func UsageError(usage string, err error) {
	fmt.Fprintf(os.Stderr, "kurrent: %v\n\n%s", err, strings.TrimRight(usage, "\n")+"\n")
	Exit(2)
}

// PrintJSON 以两空格缩进打印 JSON。
func PrintJSON(v any) {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		Fatal("encode output: %v", err)
	}
	fmt.Println(string(data))
}
