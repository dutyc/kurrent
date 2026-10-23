// 控制面 HTTP 客户端：管理凭据（admin 证书 mTLS）/ Bearer token 双通道 + JSON，
// 非 2xx 打印 detail 后退出。
package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"
)

// api 向控制面发请求并返回响应体；非 2xx 时打印错误并退出进程。
func api(method, path string, body any) []byte {
	client, server := apiEndpoint()
	var rd io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			fatal("encode request body: %v", err)
		}
		rd = bytes.NewReader(data)
	}
	req, err := http.NewRequest(method, server+path, rd)
	if err != nil {
		fatal("build request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if flagToken != "" {
		req.Header.Set("Authorization", "Bearer "+flagToken)
	}
	resp, err := client.Do(req)
	if err != nil {
		fatal("connect %s: %v%s", server, err, credentialsHint())
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		hint := ""
		if resp.StatusCode == http.StatusUnauthorized {
			hint = authHint() // 两条凭据通道都空时点明"未发现凭据"，而非只报 unauthorized
		}
		fatal("%s %s -> HTTP %d: %s%s", method, path, resp.StatusCode, detailOf(data), hint)
	}
	return data
}

// apiEndpoint 选择请求端点：管理凭据（admin 证书 mTLS，443 唯一入口）优先；
// 无管理凭据时必须显式给出 --server（配合 --token），不再有本机回环兜底——
// 控制面管理面只监听 unix socket，不提供 TCP 端口。
func apiEndpoint() (*http.Client, string) {
	if cc := loadCredentials(); cc != nil {
		return cc.client(), cc.serverURL()
	}
	server := strings.TrimRight(flagServer, "/")
	if server == "" {
		fatal("未发现管理凭据：用 kurrent credentials install 安装快照，或 --credentials 指定；\n" +
			"也可用 --server https://<host>/api/cp + --token 显式连接（管理面不再监听本机回环端口）")
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
		}
	}
	return strings.TrimSpace(string(data))
}
