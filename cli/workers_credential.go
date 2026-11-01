// workers credential 子命令：NVMe-oF（DHHC-1）凭据的轮换 / 查看 / 吊销。
// 密钥在 nvmet 节点建系统盘时由控制面自动签发（单盘与批量同一语义），本子命令用于显式轮换：
// set 从文件/stdin 读取密钥，或就地生成（--generate：32 字节随机 + CRC32 终值，
// 对齐固件契约；不打印明文——固件经 /boot-vars 注入，人不需要看到）。
// 不接受命令行明文（--secret 会泄漏到进程列表与 shell 历史）。
// 三个动词的响应都带逐 Agent 推送状态（ok=已送达 / degraded=密钥已送达但节点 TLS 数据面
// 未就绪、PSK 未生效 / failed=未送达，对账自动重试 / pending=待对账重放 /
// n/a=目标非 nvmet 后端 / none=无同步目标（无盘 / 未设凭据 / Agent 未登记）。
package main

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"hash/crc32"
	"io"
	"net/url"
	"os"
	"strings"
)

var (
	workersCredentialShowSpec = cmdSpec{
		usage:  workersCredentialShowUsage,
		flags:  []flagSpec{{name: "output", aliases: []string{"o"}, kind: flagString}},
		minPos: 1, maxPos: 1, posNames: []string{"worker_id"},
	}
	workersCredentialSetSpec = cmdSpec{
		usage: workersCredentialSetUsage,
		flags: []flagSpec{
			{name: "secret-file", kind: flagString},
			{name: "secret-stdin", kind: flagBool},
			{name: "generate", kind: flagBool},
		},
		minPos: 1, maxPos: 1, posNames: []string{"worker_id"},
	}
	workersCredentialRevokeSpec = cmdSpec{
		usage:  workersCredentialRevokeUsage,
		minPos: 1, maxPos: 1, posNames: []string{"worker_id"},
	}
)

func workersCredential(args []string) {
	if groupHelp("workers credential", args, workersCredentialGroupUsage) {
		return
	}
	switch args[0] {
	case "show":
		workersCredentialShow(args[1:])
	case "set":
		workersCredentialSet(args[1:])
	case "revoke":
		workersCredentialRevoke(args[1:])
	default:
		usageError(workersCredentialGroupUsage, fmt.Errorf("unknown workers credential subcommand: %s", args[0]))
	}
}

func workersCredentialShow(args []string) {
	p, help := mustParse(args, workersCredentialShowSpec)
	if help {
		return
	}
	format, err := p.outputFormat()
	if err != nil {
		usageError(workersCredentialShowUsage, err)
	}
	workerID := strings.TrimSpace(p.pos[0])
	if workerID == "" {
		usageError(workersCredentialShowUsage, fmt.Errorf("worker id must not be empty"))
	}
	var out map[string]any
	if err := json.Unmarshal(api("GET", "/workers/"+url.PathEscape(workerID)+"/credential", nil), &out); err != nil {
		fatal("parse credential response: %v", err)
	}
	if format == "json" {
		printJSON(out)
		return
	}
	if exists, _ := out["exists"].(bool); !exists {
		fmt.Printf("%s: no authentication key configured\n", workerID)
	} else {
		fmt.Printf("%s: configured (%s, updated %s)\n",
			workerID, strOf(out["secret_hash"]), shortTS(strOf(out["updated_at"])))
	}
	printCredentialPush(out["push"])
}

func workersCredentialSet(args []string) {
	p, help := mustParse(args, workersCredentialSetSpec)
	if help {
		return
	}
	workerID := strings.TrimSpace(p.pos[0])
	if workerID == "" {
		usageError(workersCredentialSetUsage, fmt.Errorf("worker id must not be empty"))
	}
	sources := 0
	if p.has("secret-file") {
		sources++
	}
	if p.bool("secret-stdin") {
		sources++
	}
	if p.bool("generate") {
		sources++
	}
	if sources != 1 {
		usageError(workersCredentialSetUsage, fmt.Errorf("exactly one of --secret-file, --secret-stdin or --generate is required"))
	}

	var secret string
	var err error
	switch {
	case p.has("secret-file"):
		if strings.TrimSpace(p.opt("secret-file", "")) == "" {
			usageError(workersCredentialSetUsage, fmt.Errorf("--secret-file must not be empty"))
		}
		secret, err = readSecretFile(p.opt("secret-file", ""))
	case p.bool("secret-stdin"):
		secret, err = readSecretStdin()
	default:
		secret, err = generateDHHC1()
	}
	if err != nil {
		fatal("%v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(api("PUT", "/workers/"+url.PathEscape(workerID)+"/credential",
		map[string]string{"secret": secret}), &out); err != nil {
		fatal("parse credential response: %v", err)
	}
	fmt.Printf("%s: credential set (%s)\n", workerID, strOf(out["secret_hash"]))
	printCredentialPush(out["push"])
}

func workersCredentialRevoke(args []string) {
	p, help := mustParse(args, workersCredentialRevokeSpec)
	if help {
		return
	}
	workerID := strings.TrimSpace(p.pos[0])
	if workerID == "" {
		usageError(workersCredentialRevokeUsage, fmt.Errorf("worker id must not be empty"))
	}
	var out map[string]any
	if err := json.Unmarshal(api("DELETE", "/workers/"+url.PathEscape(workerID)+"/credential", nil), &out); err != nil {
		fatal("parse credential response: %v", err)
	}
	fmt.Printf("%s: credential revoked (the next boot is refused)\n", workerID)
	printCredentialPush(out["push"])
}

func readSecretFile(path string) (string, error) {
	path = strings.TrimSpace(path)
	if path == "" {
		return "", fmt.Errorf("--secret-file must not be empty")
	}
	info, err := os.Stat(path)
	if err != nil {
		return "", fmt.Errorf("read secret file %s: %w", path, err)
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("secret file %s is not a regular file (devices, FIFOs and directories are rejected)", path)
	}
	if info.Mode().Perm()&0o077 != 0 {
		return "", fmt.Errorf("secret file %s has overly broad permissions (%04o): run chmod 600 (the key must not be readable by other users)", path, info.Mode().Perm())
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("read secret file %s: %w", path, err)
	}
	return validateSecretInput(string(data))
}

func readSecretStdin() (string, error) {
	// 密钥不足 1KB；限长避免把管道里的其他内容整段读进内存
	data, err := io.ReadAll(io.LimitReader(os.Stdin, 8192))
	if err != nil {
		return "", fmt.Errorf("read stdin: %w", err)
	}
	if len(data) >= 8192 {
		return "", fmt.Errorf("stdin input is too long (a DHHC-1 key should be under 1KB)")
	}
	return validateSecretInput(string(data))
}

// validateSecretInput 按固件契约校验 DHHC-1:01:<base64(32B 密钥 + CRC32 小端)>：
// 在发请求前拦住截断/损坏的密钥，避免把坏密钥写进控制面台账再等固件发现。
func validateSecretInput(raw string) (string, error) {
	secret := strings.TrimSpace(raw)
	if secret == "" {
		return "", fmt.Errorf("secret is empty")
	}
	parts := strings.SplitN(secret, ":", 3)
	if len(parts) != 3 || parts[0] != "DHHC-1" || parts[1] != "01" {
		return "", fmt.Errorf("invalid secret format: expected DHHC-1:01:<base64> (32-byte key + CRC32)")
	}
	payload, err := base64.StdEncoding.DecodeString(parts[2])
	if err != nil || len(payload) != 36 {
		return "", fmt.Errorf("invalid secret payload: expected base64-encoded 36 bytes (32-byte key + 4-byte CRC32)")
	}
	want := crc32.ChecksumIEEE(payload[:32])
	if got := binary.LittleEndian.Uint32(payload[32:]); got != want {
		return "", fmt.Errorf("secret CRC32 check failed (corrupted file or not a DHHC-1 key)")
	}
	return secret, nil
}

// printCredentialPush 渲染推送状态投影：汇总一行 + 逐 Agent 明细（ok 单条不列）。
func printCredentialPush(pushAny any) {
	push, _ := pushAny.(map[string]any)
	if push == nil {
		return
	}
	result := strOf(push["result"])
	label, ok := map[string]string{
		"ok":       "delivered",
		"degraded": "partially applied (node TLS data plane not ready; reconciliation will retry)",
		"failed":   "not delivered (reconciliation will retry)",
		"pending":  "pending (reconciliation will replay)",
		"n/a":      "not applicable (target agent is not an nvmet backend)",
		"none":     "no sync target (no NVMe-oF disk, credential not set, or the disk's agent is not registered)",
	}[result]
	if !ok {
		label = result
	}
	if ts := strOf(push["updated_at"]); ts != "" {
		label += " (" + shortTS(ts) + ")"
	}
	fmt.Printf("push status: %s\n", label)
	agentsAny, _ := push["agents"].([]any)
	if len(agentsAny) <= 1 && result == "ok" {
		return // 单目标成功：汇总已表达，不重复列明细
	}
	for _, a := range agentsAny {
		m, _ := a.(map[string]any)
		line := "  " + strOf(m["agent_id"]) + ": " + strOf(m["result"])
		if e := strOf(m["error"]); e != "" {
			line += " -- " + e
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
