// kurrent —— Kurrent 控制面 CLI。
// 零第三方依赖（net/http + encoding/json + yaml.v3），go build 出单二进制。
//
// 管理凭据：自动发现管理凭据文件（control_plane/state/admin.conf → 可执行文件旁 →
// ~/.kurrent/admin.conf 用户级快照，admin 证书 mTLS；kurrent credentials install 安装快照）；
// 环境变量 KURRENT_CP_URL / KURRENT_CP_TOKEN / KURRENT_CREDENTIALS 兜底。
package main

import (
	"flag"
	"fmt"
	"os"
)

var (
	flagServer      string
	flagToken       string
	flagCredentials string
)

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "kurrent: "+format+"\n", args...)
	os.Exit(1)
}

const usageText = `kurrent —— Kurrent 控制面 CLI

用法：
  kurrent [--credentials PATH] [--server URL] [--token T] <group> <verb> [args]

全局选项（缺省读环境变量 KURRENT_CREDENTIALS / KURRENT_CP_URL / KURRENT_CP_TOKEN）：
  --credentials 管理凭据文件（admin 客户端证书走 443 mTLS；默认自动发现
                control_plane/state/admin.conf → 可执行文件旁 → ~/.kurrent/admin.conf）
  --server      控制面 API 根（有管理凭据时覆盖其 server 地址，远程管理员用；
                无管理凭据时默认 http://127.0.0.1:4839，经 nginx 入口传
                https://<host>/api/cp）
  --token       管理 token（kurrent token create --usage admin 产物）

命令组：
  agents list [--no-live] [-o table|json]    列出 Agent（含存活探测）
  agents get <id>                             查看单个 Agent
  agents add <id> --base-url URL [--role disk,cd] [--storager-ip IP] [--tags a,b] [--remark TEXT] [--disabled]
  agents edit <id> --base-url URL [...]       更新 Agent（全量覆盖，参数同 add；不带
                                              --remark/--tags 即清空对应字段）
  agents remove <id>                          删除 Agent 台账（含母盘标签）
  agents probe --base-url URL                 探测 Agent 能力（不落盘）
  token create [--usage bootstrap|admin] [--cp-url URL]
                                              签发凭据：
                                              bootstrap（默认）集群级通用引导 token——
                                              不绑节点、TTL 内可复用，输出带地址的 join
                                              命令；admin 管理 token（认证器链 Bearer，供
                                              脚本与 WebUI 登录；--cp-url/--server 推导同前）
  token list [-o table|json]                  列出未过期 token 台账（不返回明文）
  token revoke <token_id>                     吊销 token：删条目即失效
  credentials install | show | remove         管理凭据用户级快照：install 复制仓库
                                              admin.conf（含证书副本）到 ~/.kurrent，
                                              此后任意目录可运行 kurrent（凭据轮换后重跑
                                              install 刷新）；show 查看生效凭据与快照状态；
                                              remove 删除快照（仓库内凭据不受影响）
  config print init-defaults|node-defaults   输出声明模板（重定向为
                                              control_plane/kurrent.yaml 或
                                              storager/kurrent.yaml 后编辑）
  init [--config PATH] [--dir PATH]          控制面初始化：校验声明并收敛启动控制面
                                              （默认读 control_plane/kurrent.yaml；
                                              --config 指定其他声明文件则应用之）
  join <cp-url> [--config PATH] [--token T] [--dir PATH]
                                              节点侧加入：
                                              声明 kurrent.yaml 缺失自动生成、已存在则读入并
                                              同步地址，收敛启动 agent（幂等可重跑）；
                                              nvmet 后端预检宿主前置（内核模块 + configfs），
                                              未就绪指路 nvmeof/host-prepare.sh
  workers list | workers get <id>             Worker 台账（只读）
  workers credential show <id> [-o json]      查看 NVMe-oF 认证密钥与推送状态
  workers credential set <id> --secret DHHC-1:... | --generate
                                              轮换认证密钥（--generate 就地生成 32 字节
                                              随机密钥，不打印明文；固件经 /boot-vars
                                              注入）。nvmet 建盘已自动签发，此处只用于
                                              显式轮换
  workers credential revoke <id>              吊销认证密钥（回落明文连接；未送达由
                                              对账自动重试）
  ops list [--limit N] [-o table|json]        操作审计日志（只读）
  version                                     输出版本号（发版构建 -ldflags 注入，如 v0.3.0）
`

func main() {
	fs := flag.NewFlagSet("kurrent", flag.ExitOnError)
	fs.StringVar(&flagServer, "server", os.Getenv("KURRENT_CP_URL"), "控制面 API 根")
	fs.StringVar(&flagToken, "token", os.Getenv("KURRENT_CP_TOKEN"), "管理 token")
	fs.StringVar(&flagCredentials, "credentials", os.Getenv("KURRENT_CREDENTIALS"), "管理凭据文件（默认发现 control_plane/state/admin.conf）")
	fs.Parse(os.Args[1:])

	args := fs.Args()
	if len(args) == 0 || args[0] == "help" || args[0] == "-h" {
		fmt.Print(usageText)
		return
	}
	switch args[0] {
	case "agents":
		cmdAgents(args[1:])
	case "token":
		cmdToken(args[1:])
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
		fatal("unknown command group: %s（help 查看用法）", args[0])
	}
}
