// workers credential 子命令：NVMe-oF（DHHC-1）凭据的轮换 / 查看 / 吊销。
// 密钥在 nvmet 节点建系统盘时由控制面自动签发（单盘与批量同一语义），本子命令用于显式轮换：
// set 的密钥可粘贴（--secret）或就地生成（--generate：32 字节随机 + CRC32 终值，
// 对齐固件契约；不打印明文——固件经 /boot-vars 注入，人不需要看到）。
// 三个动词的响应都带逐 Agent 推送状态（ok=已送达 / failed=未送达，对账自动重试 /
// pending=待对账重放 / n/a=目标非 nvmet 后端 / none=无同步目标（无盘 / 未设凭据 / Agent 未登记）。
package main

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"hash/crc32"
)

func workersCredential(args []string) {
	if len(args) < 2 {
		workersCredentialUsage()
	}
	verb, workerID := args[0], args[1]
	switch verb {
	case "show":
		workersCredentialShow(workerID, args[2:])
	case "set":
		workersCredentialSet(workerID, args[2:])
	case "revoke":
		workersCredentialRevoke(workerID, args[2:])
	default:
		workersCredentialUsage()
	}
}

func workersCredentialUsage() {
	fatal("用法: kurrent workers credential show <worker_id> [-o json]\n" +
		"      kurrent workers credential set <worker_id> (--secret DHHC-1:... | --generate)\n" +
		"      kurrent workers credential revoke <worker_id>\n" +
		"      set --generate 就地生成 32 字节随机密钥（不打印明文，固件经 /boot-vars 注入）\n" +
		"      nvmet 建盘即自动签发密钥，set 用于显式轮换")
}

func workersCredentialShow(workerID string, rest []string) {
	p := parseArgs(rest)
	var out map[string]any
	if err := json.Unmarshal(api("GET", "/workers/"+workerID+"/credential", nil), &out); err != nil {
		fatal("解析凭据响应: %v", err)
	}
	if p.opt("o", "table") == "json" {
		printJSON(out)
		return
	}
	if exists, _ := out["exists"].(bool); !exists {
		fmt.Printf("%s: 未设置认证密钥（引导走明文连接）\n", workerID)
	} else {
		fmt.Printf("%s: 已设置（%s，更新于 %s）\n",
			workerID, strOf(out["secret_hash"]), shortTS(strOf(out["updated_at"])))
	}
	printCredentialPush(out["push"])
}

func workersCredentialSet(workerID string, rest []string) {
	p := parseArgs(rest)
	secret := p.opt("secret", "")
	if secret == "" && p.has("generate") {
		var err error
		if secret, err = generateDHHC1(); err != nil {
			fatal("生成密钥: %v", err)
		}
	}
	if secret == "" {
		fatal("需提供 --secret DHHC-1:... 或 --generate（就地生成，见 workers credential 用法）")
	}
	var out map[string]any
	if err := json.Unmarshal(api("PUT", "/workers/"+workerID+"/credential",
		map[string]string{"secret": secret}), &out); err != nil {
		fatal("解析凭据响应: %v", err)
	}
	fmt.Printf("%s: 凭据已设置（%s）\n", workerID, strOf(out["secret_hash"]))
	printCredentialPush(out["push"])
}

func workersCredentialRevoke(workerID string, rest []string) {
	var out map[string]any
	if err := json.Unmarshal(api("DELETE", "/workers/"+workerID+"/credential", nil), &out); err != nil {
		fatal("解析凭据响应: %v", err)
	}
	fmt.Printf("%s: 凭据已吊销（下次引导回落明文连接）\n", workerID)
	printCredentialPush(out["push"])
}

// printCredentialPush 渲染推送状态投影：汇总一行 + 逐 Agent 明细（ok 单条不列）。
func printCredentialPush(pushAny any) {
	push, _ := pushAny.(map[string]any)
	if push == nil {
		return
	}
	result := strOf(push["result"])
	label, ok := map[string]string{
		"ok":      "已送达",
		"failed":  "未送达（对账将自动重试）",
		"pending": "待推送（对账将重放）",
		"n/a":     "不适用（目标 Agent 非 nvmet 后端）",
		"none":    "无同步目标（无 NVMe-oF 盘、凭据未设置，或盘所在 Agent 未登记）",
	}[result]
	if !ok {
		label = result
	}
	if ts := strOf(push["updated_at"]); ts != "" {
		label += "（" + shortTS(ts) + "）"
	}
	fmt.Printf("推送状态: %s\n", label)
	agentsAny, _ := push["agents"].([]any)
	if len(agentsAny) <= 1 && result == "ok" {
		return // 单目标成功：汇总已表达，不重复列明细
	}
	for _, a := range agentsAny {
		m, _ := a.(map[string]any)
		line := "  " + strOf(m["agent_id"]) + ": " + strOf(m["result"])
		if e := strOf(m["error"]); e != "" {
			line += " —— " + e
		}
		fmt.Println(line)
	}
}

// generateDHHC1 生成 DHHC-1:01:<base64(32B 随机 + CRC32 小端终值)>（固件 2.1 契约；
// 明文不打印也不落盘——控制面持久化后由 /boot-vars 注入固件）。
func generateDHHC1() (string, error) {
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return "", err
	}
	sum := crc32.ChecksumIEEE(key)
	payload := append(key, byte(sum), byte(sum>>8), byte(sum>>16), byte(sum>>24))
	return "DHHC-1:01:" + base64.StdEncoding.EncodeToString(payload), nil
}
