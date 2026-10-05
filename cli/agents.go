// agents 子命令：list/get/add/edit/remove/probe（对齐控制面 Agent 管理端点）。
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
	"text/tabwriter"
)

type agentRole struct {
	Disk bool `json:"disk"`
	CD   bool `json:"cd"`
}

// agentBody 对应 CreateAgentRequest / UpdateAgentRequest（id 仅 add 携带）。
type agentBody struct {
	ID         string    `json:"id,omitempty"`
	BaseURL    string    `json:"base_url"`
	StoragerIP string    `json:"storager_ip,omitempty"`
	Role       agentRole `json:"role"`
	Tags       []string  `json:"tags"`
	Enabled    bool      `json:"enabled"`
	Remark     string    `json:"remark"` // 备注（人类识别用；edit 为全量覆盖，不带 --remark 即清空）
}

func cmdAgents(args []string) {
	if len(args) == 0 {
		fatal("agents 子命令: list | get | add | edit | remove | probe（help 查看用法）")
	}
	switch args[0] {
	case "list":
		agentsList(args[1:])
	case "get":
		if len(args) != 2 {
			fatal("用法: kurrent agents get <id>")
		}
		agentsGet(args[1])
	case "add":
		agentsAdd(args[1:])
	case "edit":
		agentsEdit(args[1:])
	case "remove":
		if len(args) != 2 {
			fatal("用法: kurrent agents remove <id>")
		}
		agentsRemove(args[1])
	case "probe":
		agentsProbe(args[1:])
	default:
		fatal("未知 agents 子命令: %s（help 查看用法）", args[0])
	}
}

// ── list / get ──

func agentsList(args []string) {
	p := parseArgs(args)
	live := !p.has("no-live")

	var items []map[string]any
	if err := json.Unmarshal(api("GET", agentsPath(live), nil), &items); err != nil {
		fatal("解析 agents 响应: %v", err)
	}
	if p.opt("o", "table") == "json" {
		printJSON(items)
		return
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 4, 2, ' ', 0)
	fmt.Fprintln(w, "ID\tREMARK\tHEALTH\tBASE_URL\tROLE\tENABLED\tTAGS")
	for _, a := range items {
		role := "disk"
		if v, _ := a["role"].(map[string]any); v != nil && boolOf(v["cd"]) {
			role = "disk,cd"
		}
		health := "—"
		if h, ok := a["health"].(string); ok && h != "" {
			health = h
		}
		id, _ := a["id"].(string)
		remark, _ := a["remark"].(string)
		baseURL, _ := a["base_url"].(string)
		tags := joinStrings(a["tags"])
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%v\t%s\n", id, remark, health, baseURL, role, boolOf(a["enabled"]), tags)
	}
	w.Flush()
}

func agentsGet(agentID string) {
	var items []map[string]any
	if err := json.Unmarshal(api("GET", agentsPath(true), nil), &items); err != nil {
		fatal("解析 agents 响应: %v", err)
	}
	for _, a := range items {
		if a["id"] == agentID {
			printJSON(a)
			return
		}
	}
	fatal("agent not found: %s", agentID)
}

// ── add / edit（全量覆盖）──

func agentsAdd(args []string) {
	p := parseArgs(args)
	agentID := firstPos(p)
	baseURL := p.opt("base-url", "")
	if agentID == "" || baseURL == "" {
		fatal("用法: kurrent agents add <id> --base-url URL [--role disk,cd] [--tags a,b] [--remark TEXT]")
	}
	body := agentBody{ID: agentID, BaseURL: baseURL, StoragerIP: p.opt("storager-ip", ""),
		Role: parseRole(p.opt("role", "disk")), Tags: splitTags(p.opt("tags", "")), Enabled: !p.has("disabled"),
		Remark: p.opt("remark", "")}
	api("POST", "/agents", body)
	fmt.Printf("agent %s registered\n", agentID)
}

func agentsEdit(args []string) {
	p := parseArgs(args)
	agentID := firstPos(p)
	baseURL := p.opt("base-url", "")
	if agentID == "" || baseURL == "" {
		fatal("用法: kurrent agents edit <id> --base-url URL [--role disk,cd] [--tags a,b] [--remark TEXT]\n" +
			"      全量覆盖语义：不带 --remark / --tags 即清空对应字段")
	}
	body := agentBody{BaseURL: baseURL, StoragerIP: p.opt("storager-ip", ""),
		Role: parseRole(p.opt("role", "disk")), Tags: splitTags(p.opt("tags", "")), Enabled: !p.has("disabled"),
		Remark: p.opt("remark", "")}
	api("PUT", "/agents/"+agentID, body)
	fmt.Printf("agent %s updated\n", agentID)
}

// ── remove / probe ──

func agentsRemove(agentID string) {
	var out map[string]any
	if err := json.Unmarshal(api("DELETE", "/agents/"+agentID, nil), &out); err != nil {
		fatal("解析删除响应: %v", err)
	}
	fmt.Printf("agent %s removed（master_tags_removed=%v）\n", out["deleted"], out["master_tags_removed"])
}

func agentsProbe(args []string) {
	p := parseArgs(args)
	baseURL := p.opt("base-url", "")
	if baseURL == "" {
		fatal("用法: kurrent agents probe --base-url URL")
	}
	body := map[string]any{"base_url": baseURL, "agent_id": p.opt("agent-id", "")}
	var out map[string]any
	if err := json.Unmarshal(api("POST", "/agents/probe", body), &out); err != nil {
		fatal("解析探测响应: %v", err)
	}
	fmt.Printf("agent reachable: backend=%v fs_type=%v base_nqn=%v\n",
		out["backend"], out["fs_type"], out["base_nqn"])
	fmt.Printf("role=%v tags=%v storager_ip=%v\n",
		out["role"], out["tags"], out["storager_ip"])
}

// ── 工具 ──

// agentsPath 拼 /agents 查询路径（live 控制是否附带存活探测）。
func agentsPath(live bool) string {
	if live {
		return "/agents?live=true"
	}
	return "/agents?live=false"
}

func parseRole(s string) agentRole {
	var r agentRole
	for _, part := range strings.Split(s, ",") {
		switch strings.TrimSpace(part) {
		case "disk":
			r.Disk = true
		case "cd":
			r.CD = true
		}
	}
	if !r.Disk && !r.CD {
		fatal("无效角色: %s（disk,cd 组合）", s)
	}
	return r
}

func splitTags(s string) []string {
	var tags []string
	for _, t := range strings.Split(s, ",") {
		if t = strings.TrimSpace(t); t != "" {
			tags = append(tags, t)
		}
	}
	return tags
}

// firstPos 返回第一个位置参数（无则空串）。
func firstPos(p parsedArgs) string {
	if len(p.pos) > 0 {
		return p.pos[0]
	}
	return ""
}

func boolOf(v any) bool {
	b, _ := v.(bool)
	return b
}

func joinStrings(v any) string {
	arr, ok := v.([]any)
	if !ok {
		return ""
	}
	parts := make([]string, 0, len(arr))
	for _, x := range arr {
		if s, ok := x.(string); ok && s != "" {
			parts = append(parts, s)
		}
	}
	sort.Strings(parts)
	return strings.Join(parts, ",")
}

func printJSON(v any) {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		fatal("encode output: %v", err)
	}
	fmt.Println(string(data))
}
