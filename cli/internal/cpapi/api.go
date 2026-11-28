// 控制面 HTTP 客户端：管理凭据（admin 证书 mTLS）/ Bearer token 双通道 + JSON，
// 非 2xx 打印 detail 后退出。
package cpapi

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"kurrent/cli/internal/cli"
	"kurrent/cli/internal/creds"
	"kurrent/cli/internal/validate"
	"net/http"
	"strings"
	"time"
)

// Do 向控制面发请求并返回响应体；非 2xx 时打印错误并退出进程。
func Do(method, path string, body any) []byte {
	client, server := endpoint()
	var rd io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			cli.Fatal("encode request body: %v", err)
		}
		rd = bytes.NewReader(data)
	}
	req, err := http.NewRequest(method, server+path, rd)
	if err != nil {
		cli.Fatal("build request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if cli.Token != "" {
		req.Header.Set("Authorization", "Bearer "+cli.Token)
	}
	resp, err := client.Do(req)
	if err != nil {
		cli.Fatal("connect %s: %v%s", server, err, creds.CredentialsHint())
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		hint := ""
		if resp.StatusCode == http.StatusUnauthorized {
			hint = creds.AuthHint() // 两条凭据通道都空时点明"未发现凭据"，而非只报 unauthorized
		}
		cli.Fatal("%s %s -> HTTP %d: %s%s", method, path, resp.StatusCode, detailOf(data), hint)
	}
	return data
}

// ServerURL 返回当前实际使用的 API 根（凭据文件优先，--server 覆盖；无凭据时取 --server）。
// 供输出 join 命令等需要“当前入口”的场景复用，避免与 apiEndpoint 的发现链分叉。
// 环境变量 KURRENT_CP_URL 未经命令行校验，这里统一校验，坏地址不会被当作有效入口。
func ServerURL() (string, error) {
	if cc := creds.LoadCredentials(); cc != nil {
		return cc.ServerURL()
	}
	if strings.TrimSpace(cli.Server) == "" {
		return "", fmt.Errorf("no control-plane address configured: management credentials were not found and neither --server nor KURRENT_CP_URL was given")
	}
	return validate.ValidateHTTPURL("--server/KURRENT_CP_URL", cli.Server)
}

// endpoint 选择请求端点：管理凭据（admin 证书 mTLS，443 唯一入口）优先；
// 无管理凭据时必须显式给出 --server（配合 --token），不再有本机回环回退——
// 控制面管理面只监听 unix socket，不提供 TCP 端口。
func endpoint() (*http.Client, string) {
	if cc := creds.LoadCredentials(); cc != nil {
		server, err := cc.ServerURL()
		if err != nil {
			cli.Fatal("%v", err)
		}
		return cc.Client(), server
	}
	if strings.TrimSpace(cli.Server) == "" {
		cli.Fatal("no management credentials found: run kurrent credentials install, or pass --credentials;\n" +
			"alternatively connect explicitly with --server https://<host>/api/cp + --token (the management API no longer listens on a local loopback port)")
	}
	server, err := validate.ValidateHTTPURL("--server/KURRENT_CP_URL", cli.Server)
	if err != nil {
		cli.Fatal("%v", err)
	}
	return &http.Client{Timeout: 15 * time.Second}, server
}

// detailOf 从 FastAPI 错误响应中提取人类可读的 detail。
func detailOf(data []byte) string {
	var m map[string]any
	if json.Unmarshal(data, &m) == nil {
		switch d := m["detail"].(type) {
		case string:
			return d
		case map[string]any:
			if s, ok := d["error"].(string); ok {
				return s
			}
			if b, err := json.Marshal(d); err == nil {
				return string(b)
			}
		case []any:
			// FastAPI 422：detail 是 [{loc, msg, type}, ...]，拼成一行便于阅读
			msgs := make([]string, 0, len(d))
			for _, item := range d {
				if m, ok := item.(map[string]any); ok {
					msg, _ := m["msg"].(string)
					if msg == "" {
						continue
					}
					if loc, ok := m["loc"].([]any); ok && len(loc) > 0 {
						parts := make([]string, 0, len(loc))
						for _, p := range loc {
							parts = append(parts, fmt.Sprint(p))
						}
						msg = strings.Join(parts, ".") + ": " + msg
					}
					msgs = append(msgs, msg)
				}
			}
			if len(msgs) > 0 {
				return strings.Join(msgs, "; ")
			}
		}
	}
	return strings.TrimSpace(string(data))
}
