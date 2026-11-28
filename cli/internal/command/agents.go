// agents 子命令：list/get/add/edit/remove/probe（对齐控制面 Agent 管理端点）。
package main

import (
	"encoding/json"
	"fmt"
	"net/url"
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
	Remark     string    `json:"remark"`
}

var (
	agentsListSpec = cmdSpec{
		usage: agentsListUsage,
		flags: []flagSpec{
			{name: "no-live", kind: flagBool},
			{name: "output", aliases: []string{"o"}, kind: flagString},
		},
		maxPos: 0,
	}
	agentsGetSpec = cmdSpec{usage: agentsGetUsage, minPos: 1, maxPos: 1, posNames: []string{"id"}}
	agentsAddSpec = cmdSpec{
		usage: agentsAddUsage,
		flags: []flagSpec{
			{name: "base-url", kind: flagString, required: true},
			{name: "role", kind: flagString},
			{name: "storager-ip", kind: flagString},
			{name: "tags", kind: flagString},
			{name: "remark", kind: flagString},
			{name: "disabled", kind: flagBool},
		},
		minPos: 1, maxPos: 1, posNames: []string{"id"},
	}
	agentsEditSpec = cmdSpec{
		usage: agentsEditUsage,
		flags: []flagSpec{
			{name: "base-url", kind: flagString, required: true},
			{name: "role", kind: flagString},
			{name: "storager-ip", kind: flagString},
			{name: "tags", kind: flagString},
			{name: "remark", kind: flagString},
			{name: "disabled", kind: flagBool},
		},
		minPos: 1, maxPos: 1, posNames: []string{"id"},
	}
	agentsRemoveSpec = cmdSpec{usage: agentsRemoveUsage, minPos: 1, maxPos: 1, posNames: []string{"id"}}
	agentsProbeSpec  = cmdSpec{
		usage: agentsProbeUsage,
		flags: []flagSpec{
			{name: "base-url", kind: flagString, required: true},
			{name: "agent-id", kind: flagString},
		},
		maxPos: 0,
	}
)

func cmdAgents(args []string) {
	if groupHelp("agents", args, agentsGroupUsage) {
		return
	}
	switch args[0] {
	case "list":
		agentsList(args[1:])
	case "get":
		agentsGet(args[1:])
	case "add":
		agentsAdd(args[1:])
	case "edit":
		agentsEdit(args[1:])
	case "remove":
		agentsRemove(args[1:])
	case "probe":
		agentsProbe(args[1:])
	default:
		usageError(agentsGroupUsage, fmt.Errorf("unknown agents subcommand: %s", args[0]))
	}
}

func agentsList(args []string) {
	p, help := mustParse(args, agentsListSpec)
	if help {
		return
	}
	format, err := p.outputFormat()
	if err != nil {
		usageError(agentsListUsage, err)
	}
	live := !p.bool("no-live")

	var items []map[string]any
	if err := json.Unmarshal(api("GET", agentsPath(live), nil), &items); err != nil {
		fatal("parse agents response: %v", err)
	}
	if format == "json" {
		printJSON(items)
		return
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 4, 2, ' ', 0)
	fmt.Fprintln(w, "ID\tREMARK\tHEALTH\tBASE_URL\tROLE\tENABLED\tTAGS")
	for _, a := range items {
		role := "—"
		if v, _ := a["role"].(map[string]any); v != nil {
			parts := []string{}
			if boolOf(v["disk"]) {
				parts = append(parts, "disk")
			}
			if boolOf(v["cd"]) {
				parts = append(parts, "cd")
			}
			if len(parts) > 0 {
				role = strings.Join(parts, ",")
			}
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

func agentsGet(args []string) {
	p, help := mustParse(args, agentsGetSpec)
	if help {
		return
	}
	agentID, err := validateAgentID(p.pos[0])
	if err != nil {
		usageError(agentsGetUsage, err)
	}
	agentsGetByID(agentID)
}

func agentsGetByID(agentID string) {
	var items []map[string]any
	if err := json.Unmarshal(api("GET", agentsPath(true), nil), &items); err != nil {
		fatal("parse agents response: %v", err)
	}
	for _, a := range items {
		if a["id"] == agentID {
			printJSON(a)
			return
		}
	}
	fatal("agent not found: %s", agentID)
}

func agentsAdd(args []string) {
	p, help := mustParse(args, agentsAddSpec)
	if help {
		return
	}
	agentID, err := validateAgentID(p.pos[0])
	if err != nil {
		usageError(agentsAddUsage, err)
	}
	body, err := buildAgentBody(p)
	if err != nil {
		usageError(agentsAddUsage, err)
	}
	body.ID = agentID
	api("POST", "/agents", body)
	fmt.Printf("agent %s registered\n", agentID)
}

func agentsEdit(args []string) {
	p, help := mustParse(args, agentsEditSpec)
	if help {
		return
	}
	agentID, err := validateAgentID(p.pos[0])
	if err != nil {
		usageError(agentsEditUsage, err)
	}
	body, err := buildAgentBody(p)
	if err != nil {
		usageError(agentsEditUsage, err)
	}
	api("PUT", "/agents/"+url.PathEscape(agentID), body)
	fmt.Printf("agent %s updated\n", agentID)
}

func agentsRemove(args []string) {
	p, help := mustParse(args, agentsRemoveSpec)
	if help {
		return
	}
	agentID, err := validateAgentID(p.pos[0])
	if err != nil {
		usageError(agentsRemoveUsage, err)
	}
	var out map[string]any
	if err := json.Unmarshal(api("DELETE", "/agents/"+url.PathEscape(agentID), nil), &out); err != nil {
		fatal("parse delete response: %v", err)
	}
	fmt.Printf("agent %s deleted (master_tags_removed=%v)\n", agentID, out["master_tags_removed"])
}

func agentsProbe(args []string) {
	p, help := mustParse(args, agentsProbeSpec)
	if help {
		return
	}
	baseURL, err := validateHTTPURL("--base-url", p.opt("base-url", ""))
	if err != nil {
		usageError(agentsProbeUsage, err)
	}
	agentID := ""
	if p.has("agent-id") {
		if agentID, err = validateAgentID(p.opt("agent-id", "")); err != nil {
			usageError(agentsProbeUsage, err)
		}
	}
	body := map[string]any{"base_url": baseURL, "agent_id": agentID}
	var out map[string]any
	if err := json.Unmarshal(api("POST", "/agents/probe", body), &out); err != nil {
		fatal("parse probe response: %v", err)
	}
	fmt.Printf("agent reachable: backend=%v fs_type=%v base_nqn=%v\n",
		out["backend"], out["fs_type"], out["base_nqn"])
	fmt.Printf("role=%v tags=%v storager_ip=%v\n",
		out["role"], out["tags"], out["storager_ip"])
}

func buildAgentBody(p parsedArgs) (agentBody, error) {
	baseURL, err := validateHTTPURL("--base-url", p.opt("base-url", ""))
	if err != nil {
		return agentBody{}, err
	}
	role, err := parseRole(p.opt("role", "disk"))
	if err != nil {
		return agentBody{}, err
	}
	tags, err := splitTags(p.opt("tags", ""))
	if err != nil {
		return agentBody{}, err
	}
	storagerIP, err := validateIP("--storager-ip", p.opt("storager-ip", ""))
	if err != nil {
		return agentBody{}, err
	}
	return agentBody{
		BaseURL:    baseURL,
		StoragerIP: storagerIP,
		Role:       role,
		Tags:       tags,
		Enabled:    !p.bool("disabled"),
		Remark:     p.opt("remark", ""),
	}, nil
}

// ── 工具 ──

// agentsPath 拼 /agents 查询路径（live 控制是否附带存活探测）。
func agentsPath(live bool) string {
	if live {
		return "/agents?live=true"
	}
	return "/agents?live=false"
}

func parseRole(s string) (agentRole, error) {
	var r agentRole
	seen := map[string]bool{}
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			return agentRole{}, fmt.Errorf("--role %q contains an empty item", s)
		}
		if seen[part] {
			return agentRole{}, fmt.Errorf("--role %q contains a duplicate item %q", s, part)
		}
		switch part {
		case "disk":
			r.Disk = true
		case "cd":
			r.CD = true
		default:
			return agentRole{}, fmt.Errorf("--role %q accepts only disk or cd", part)
		}
		seen[part] = true
	}
	if !r.Disk && !r.CD {
		return agentRole{}, fmt.Errorf("--role %q accepts only disk or cd", s)
	}
	return r, nil
}

func splitTags(s string) ([]string, error) {
	tags := []string{}
	if strings.TrimSpace(s) == "" {
		return tags, nil
	}
	seen := map[string]bool{}
	for _, t := range strings.Split(s, ",") {
		t = strings.TrimSpace(t)
		if t == "" {
			return nil, fmt.Errorf("--tags %q contains an empty tag", s)
		}
		if seen[t] {
			return nil, fmt.Errorf("--tags %q contains a duplicate tag %q", s, t)
		}
		seen[t] = true
		tags = append(tags, t)
	}
	return tags, nil
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
