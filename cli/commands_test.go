package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

type exitPanic struct{ code int }

// captureCLI 捕获命令的 stdout/stderr 与退出码（fatal/usageError 经注入的 exit 转 panic）。
func captureCLI(t *testing.T, fn func()) (code int, stdout, stderr string) {
	t.Helper()
	oldExit, oldStdout, oldStderr := exit, os.Stdout, os.Stderr
	outR, outW, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	errR, errW, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout, os.Stderr = outW, errW
	exit = func(code int) { panic(exitPanic{code}) }
	defer func() {
		os.Stdout, os.Stderr = oldStdout, oldStderr
		exit = oldExit
	}()

	func() {
		defer func() {
			if r := recover(); r != nil {
				if e, ok := r.(exitPanic); ok {
					code = e.code
					return
				}
				panic(r)
			}
		}()
		fn()
	}()

	outW.Close()
	errW.Close()
	outData, _ := io.ReadAll(outR)
	errData, _ := io.ReadAll(errR)
	outR.Close()
	errR.Close()
	return code, string(outData), string(errData)
}

type apiCall struct {
	Method string
	Path   string
	Body   []byte
}

type apiStub struct {
	mu       sync.Mutex
	calls    []apiCall
	status   int
	response string
	srv      *httptest.Server
}

func newAPIStub(t *testing.T, status int, response string) *apiStub {
	t.Helper()
	s := &apiStub{status: status, response: response}
	s.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		s.mu.Lock()
		s.calls = append(s.calls, apiCall{Method: r.Method, Path: r.URL.RequestURI(), Body: body})
		s.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(s.status)
		io.WriteString(w, s.response)
	}))
	t.Cleanup(s.srv.Close)
	old := flagServer
	flagServer = s.srv.URL
	t.Cleanup(func() { flagServer = old })
	return s
}

func (s *apiStub) Calls() []apiCall {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]apiCall(nil), s.calls...)
}

func (s *apiStub) HitCount() int {
	return len(s.Calls())
}

// isolateCLI 隔离凭据发现链与工作目录，避免测试命中仓库/用户级真实凭据。
func isolateCLI(t *testing.T) {
	t.Helper()
	clearCredentialFlags(t)
	stubUserHome(t)
	stubExeDir(t, t.TempDir())
	t.Chdir(t.TempDir())
	t.Setenv("KURRENT_CP_URL", "")
	t.Setenv("KURRENT_CP_TOKEN", "")
}

func decodeBody(t *testing.T, call apiCall) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(call.Body, &m); err != nil {
		t.Fatalf("请求体不是 JSON: %v（%s）", err, call.Body)
	}
	return m
}

func TestHelpNeverCallsAPI(t *testing.T) {
	cases := []struct {
		name string
		run  func()
		want string
	}{
		{"token create", func() { tokenCreate([]string{"--help"}) }, "Usage: kurrent token create"},
		{"token create after flag", func() { tokenCreate([]string{"--usage", "admin", "--help"}) }, "Usage: kurrent token create"},
		{"token list", func() { tokenList([]string{"-h"}) }, "Usage: kurrent token list"},
		{"agents add", func() { agentsAdd([]string{"--help"}) }, "Usage: kurrent agents add"},
		{"cert issue", func() { certIssue([]string{"--help"}) }, "Usage: kurrent cert issue"},
		{"workers credential set", func() { workersCredentialSet([]string{"--help"}) }, "Usage: kurrent workers credential set"},
		{"config status", func() { configStatus([]string{"--help"}) }, "Usage: kurrent config status"},
		{"config validate", func() { configInspectCmd("validate", []string{"--help"}) }, "Usage: kurrent config validate"},
		{"version", func() { cmdVersion([]string{"--help"}) }, "Usage: kurrent version"},
		{"token group", func() { cmdToken([]string{"--help"}) }, "Usage:"},
		{"workers group", func() { cmdWorkers([]string{"-help"}) }, "Usage:"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			isolateCLI(t)
			t.Setenv("PATH", t.TempDir()) // 帮助不应调用 docker（config/join）
			stub := newAPIStub(t, 200, "{}")
			code, stdout, stderr := captureCLI(t, c.run)
			if code != 0 {
				t.Fatalf("code = %d, stderr = %s", code, stderr)
			}
			if stub.HitCount() != 0 {
				t.Fatalf("帮助触发了 API 调用: %+v", stub.Calls())
			}
			if !strings.Contains(stdout, c.want) {
				t.Fatalf("stdout 缺少 %q:\n%s", c.want, stdout)
			}
		})
	}
}

func TestInvalidArgsNeverCallAPI(t *testing.T) {
	cases := []struct {
		name string
		run  func()
		want string
	}{
		{"token flag typo", func() { tokenCreate([]string{"--agnet-id", "0123456789ab"}) }, "unknown flag"},
		{"admin rejects bootstrap flag", func() { tokenCreate([]string{"--usage", "admin", "--agent-id", "0123456789ab"}) }, "applies only to bootstrap"},
		{"bootstrap rejects admin flag", func() { tokenCreate([]string{"--scope", "admin"}) }, "applies only to admin"},
		{"uses above schema cap", func() { tokenCreate([]string{"--agent-id", "0123456789ab", "--uses", "1001"}) }, "1..1000"},
		{"ttl above schema cap", func() { tokenCreate([]string{"--usage", "admin", "--ttl", "9000h"}) }, "8760h"},
		{"ttl duration overflow", func() { tokenCreate([]string{"--usage", "admin", "--ttl", "2562047h"}) }, "8760h"},
		{"bad usage", func() { tokenCreate([]string{"--usage", "root"}) }, "accepts only bootstrap or admin"},
		{"duplicate scope", func() { tokenCreate([]string{"--usage", "admin", "--scope", "admin,admin"}) }, "duplicate scope"},
		{"agent bad id", func() { agentsAdd([]string{"not-an-id", "--base-url", "https://agent.local"}) }, "12 lowercase hex characters"},
		{"agent bad url", func() { agentsAdd([]string{"0123456789ab", "--base-url", "ftp://agent.local"}) }, "http(s)"},
		{"agent bad role", func() { agentsAdd([]string{"0123456789ab", "--base-url", "https://agent.local", "--role", "gpu"}) }, "accepts only disk or cd"},
		{"agent missing base url", func() { agentsAdd([]string{"0123456789ab"}) }, "missing required flag"},
		{"ops negative limit", func() { opsList([]string{"--limit=-1"}) }, "positive integer"},
		{"ops zero limit", func() { opsList([]string{"--limit", "0"}) }, "positive integer"},
		{"bad output", func() { tokenList([]string{"--output", "yaml"}) }, "table or json"},
		{"session missing id", func() { sessionsRevoke(nil) }, "missing argument"},
		{"full token to revoke", func() { tokenRevoke([]string{"abcdef.0123456789abcdef"}) }, "prefix"},
		{"bad revoke reason", func() { certRevoke([]string{"alice", "--reason", "lost"}) }, "unspecified"},
		{"empty serial", func() { certRevoke([]string{"alice", "--serial="}) }, "must not be empty"},
		{"empty config path", func() { configInspectCmd("validate", []string{"--config="}) }, "must not be empty"},
		{"join bad url", func() { cmdJoin([]string{"not-a-url"}) }, "http(s)"},
		{"join empty token", func() { cmdJoin([]string{"https://cp.example.com", "--token="}) }, "must not be empty"},
		{"init empty dir", func() { cmdInit([]string{"--dir="}) }, "must not be empty"},
		{"credentials extra arg", func() { cmdCredentials([]string{"install", "extra"}) }, "unexpected positional argument"},
		{"version extra arg", func() { cmdVersion([]string{"extra"}) }, "unexpected positional argument"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			isolateCLI(t)
			t.Setenv("PATH", t.TempDir()) // 证明用法错误不会调用 docker
			stub := newAPIStub(t, 200, "{}")
			code, _, stderr := captureCLI(t, c.run)
			if code != 2 {
				t.Fatalf("code = %d, want 2（stderr=%s）", code, stderr)
			}
			if stub.HitCount() != 0 {
				t.Fatalf("用法错误触发了 API 调用: %+v", stub.Calls())
			}
			if !strings.Contains(stderr, c.want) {
				t.Fatalf("stderr 缺少 %q:\n%s", c.want, stderr)
			}
		})
	}
}

func TestGroupGlobalFlagHint(t *testing.T) {
	isolateCLI(t)
	stub := newAPIStub(t, 200, "{}")
	code, _, stderr := captureCLI(t, func() { cmdToken([]string{"--server", "x", "create"}) })
	if code != 2 || !strings.Contains(stderr, "must precede the command group") {
		t.Fatalf("code=%d stderr=%s", code, stderr)
	}
	if stub.HitCount() != 0 {
		t.Fatal("组级错误触发了 API 调用")
	}
}

func TestTokenCreateBootstrap(t *testing.T) {
	isolateCLI(t)
	stub := newAPIStub(t, 201, `{"token":"abcdef.0123456789abcdef","expires_at":"2026-11-01T12:00:00Z","agent_id":"0123456789ab","uses":0,"max_uses":500}`)
	code, stdout, stderr := captureCLI(t, func() {
		tokenCreate([]string{
			"--agent-id", "0123456789ab", "--uses", "500",
			"--cp-url", "https://cp.example.com/api/cp",
		})
	})
	if code != 0 {
		t.Fatalf("code=%d stderr=%s", code, stderr)
	}
	calls := stub.Calls()
	if len(calls) != 1 || calls[0].Method != "POST" || calls[0].Path != "/pki/tokens" {
		t.Fatalf("API 调用 = %+v", calls)
	}
	body := decodeBody(t, calls[0])
	if body["usage"] != "bootstrap" || body["agent_id"] != "0123456789ab" || body["max_uses"] != float64(500) {
		t.Fatalf("请求体 = %v", body)
	}
	if !strings.Contains(stdout, "kurrent join https://cp.example.com --token") {
		t.Fatalf("join 命令未归一化 /api/cp:\n%s", stdout)
	}
	if strings.Contains(stdout, "/api/cp") {
		t.Fatalf("join 命令不应带 /api/cp:\n%s", stdout)
	}
}

func TestTokenCreateAdminTTLIsPolicyDriven(t *testing.T) {
	isolateCLI(t)
	stub := newAPIStub(t, 201, `{"token":"abcdef.0123456789abcdef","expires_at":"2026-12-01T00:00:00Z","scopes":["readonly"]}`)
	// 900h 高于默认 720h、低于 schema 上限 8760h：CLI 应放行，由控制面策略裁决。
	code, stdout, stderr := captureCLI(t, func() {
		tokenCreate([]string{"--usage", "admin", "--scope", "readonly", "--ttl", "900h"})
	})
	if code != 0 {
		t.Fatalf("code=%d stderr=%s", code, stderr)
	}
	body := decodeBody(t, stub.Calls()[0])
	if body["ttl_hours"] != float64(900) {
		t.Fatalf("请求体 = %v", body)
	}
	if _, ok := body["scopes"].([]any); !ok {
		t.Fatalf("scopes 未发送: %v", body)
	}
	if !strings.Contains(stdout, "admin token") {
		t.Fatalf("stdout:\n%s", stdout)
	}
}

func TestAgentsDisabledFalseMeansEnabled(t *testing.T) {
	for _, c := range []struct {
		name string
		args []string
		want bool
	}{
		{"default enabled", []string{"0123456789ab", "--base-url", "https://agent.local"}, true},
		{"explicit false", []string{"0123456789ab", "--base-url", "https://agent.local", "--disabled=false"}, true},
		{"disabled", []string{"0123456789ab", "--base-url", "https://agent.local", "--disabled"}, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			isolateCLI(t)
			stub := newAPIStub(t, 201, "{}")
			code, _, stderr := captureCLI(t, func() { agentsAdd(c.args) })
			if code != 0 {
				t.Fatalf("code=%d stderr=%s", code, stderr)
			}
			body := decodeBody(t, stub.Calls()[0])
			if body["enabled"] != c.want {
				t.Fatalf("enabled = %v, want %v（body=%v）", body["enabled"], c.want, body)
			}
			if tags, ok := body["tags"].([]any); !ok || len(tags) != 0 {
				t.Fatalf("空标签应为 []，实际 %v", body["tags"])
			}
		})
	}
}

func TestCertIssueChecksOutputBeforeAPI(t *testing.T) {
	isolateCLI(t)
	dir := t.TempDir()
	csr := filepath.Join(dir, "alice.csr")
	if err := os.WriteFile(csr, []byte("dummy"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "alice.crt"), []byte("existing"), 0o600); err != nil {
		t.Fatal(err)
	}
	stub := newAPIStub(t, 201, "{}")
	code, _, stderr := captureCLI(t, func() {
		certIssue([]string{"alice", "--csr", csr, "--out", dir})
	})
	if code != 1 || !strings.Contains(stderr, "already exists") {
		t.Fatalf("code=%d stderr=%s", code, stderr)
	}
	if stub.HitCount() != 0 {
		t.Fatalf("输出文件已存在仍调用了签发 API: %+v", stub.Calls())
	}
}

func TestCertIssueMissingCSRNoAPI(t *testing.T) {
	isolateCLI(t)
	stub := newAPIStub(t, 201, "{}")
	code, _, stderr := captureCLI(t, func() {
		certIssue([]string{"alice", "--csr", filepath.Join(t.TempDir(), "missing.csr")})
	})
	if code != 1 || !strings.Contains(stderr, "read CSR") {
		t.Fatalf("code=%d stderr=%s", code, stderr)
	}
	if stub.HitCount() != 0 {
		t.Fatal("缺少 CSR 仍调用了签发 API")
	}
}

func TestWorkerCredentialRejectsUnsafeInputBeforeAPI(t *testing.T) {
	cases := []struct {
		name string
		run  func(t *testing.T)
		code int
		want string
	}{
		{
			name: "argv secret",
			run:  func(t *testing.T) { workersCredentialSet([]string{"w1", "--secret", "DHHC-1:01:x"}) },
			code: 2,
			want: "--secret-file",
		},
		{
			name: "two sources",
			run: func(t *testing.T) {
				workersCredentialSet([]string{"w1", "--secret-file", "/tmp/x", "--generate"})
			},
			code: 2,
			want: "exactly one of",
		},
		{
			name: "no source",
			run:  func(t *testing.T) { workersCredentialSet([]string{"w1"}) },
			code: 2,
			want: "exactly one of",
		},
		{
			name: "empty file flag",
			run:  func(t *testing.T) { workersCredentialSet([]string{"w1", "--secret-file="}) },
			code: 2,
			want: "must not be empty",
		},
		{
			name: "world readable file",
			run: func(t *testing.T) {
				p := filepath.Join(t.TempDir(), "secret")
				if err := os.WriteFile(p, []byte("DHHC-1:01:AAAA"), 0o644); err != nil {
					t.Fatal(err)
				}
				workersCredentialSet([]string{"w1", "--secret-file", p})
			},
			code: 1,
			want: "overly broad permissions",
		},
		{
			name: "bad format",
			run: func(t *testing.T) {
				p := filepath.Join(t.TempDir(), "secret")
				if err := os.WriteFile(p, []byte("not-a-key"), 0o600); err != nil {
					t.Fatal(err)
				}
				workersCredentialSet([]string{"w1", "--secret-file", p})
			},
			code: 1,
			want: "invalid secret format",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			isolateCLI(t)
			stub := newAPIStub(t, 200, "{}")
			code, _, stderr := captureCLI(t, func() { c.run(t) })
			if code != c.code {
				t.Fatalf("code=%d want %d（stderr=%s）", code, c.code, stderr)
			}
			if !strings.Contains(stderr, c.want) {
				t.Fatalf("stderr 缺少 %q:\n%s", c.want, stderr)
			}
			if stub.HitCount() != 0 {
				t.Fatalf("非法输入仍调用 API: %+v", stub.Calls())
			}
		})
	}
}

func TestWorkerCredentialAcceptsValidDHHC(t *testing.T) {
	isolateCLI(t)
	secret, err := generateDHHC1()
	if err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(t.TempDir(), "secret")
	if err := os.WriteFile(p, []byte(secret+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	stub := newAPIStub(t, 200, `{"secret_hash":"deadbeef","push":{"result":"ok","agents":[{"agent_id":"a","result":"ok"}]}}`)
	code, stdout, stderr := captureCLI(t, func() {
		workersCredentialSet([]string{"w1", "--secret-file", p})
	})
	if code != 0 {
		t.Fatalf("code=%d stderr=%s", code, stderr)
	}
	body := decodeBody(t, stub.Calls()[0])
	if body["secret"] != secret {
		t.Fatal("请求体中的密钥与文件不一致")
	}
	if !strings.Contains(stdout, "credential set") {
		t.Fatalf("stdout:\n%s", stdout)
	}
}

func TestWorkerCredentialGenerate(t *testing.T) {
	isolateCLI(t)
	stub := newAPIStub(t, 200, `{"secret_hash":"deadbeef"}`)
	code, _, stderr := captureCLI(t, func() {
		workersCredentialSet([]string{"w1", "--generate"})
	})
	if code != 0 {
		t.Fatalf("code=%d stderr=%s", code, stderr)
	}
	body := decodeBody(t, stub.Calls()[0])
	secret, _ := body["secret"].(string)
	if _, err := validateSecretInput(secret); err != nil {
		t.Fatalf("生成的密钥不合规: %v", err)
	}
}

func TestWorkerCredentialReadsStdin(t *testing.T) {
	isolateCLI(t)
	secret, err := generateDHHC1()
	if err != nil {
		t.Fatal(err)
	}
	oldStdin := os.Stdin
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdin = r
	t.Cleanup(func() {
		os.Stdin = oldStdin
		r.Close()
	})
	if _, err := w.WriteString(secret + "\n"); err != nil {
		t.Fatal(err)
	}
	w.Close()

	stub := newAPIStub(t, 200, `{"secret_hash":"deadbeef"}`)
	code, _, stderr := captureCLI(t, func() {
		workersCredentialSet([]string{"w1", "--secret-stdin"})
	})
	if code != 0 {
		t.Fatalf("code=%d stderr=%s", code, stderr)
	}
	if got := decodeBody(t, stub.Calls()[0])["secret"]; got != secret {
		t.Fatal("stdin 密钥未发送")
	}
}

func TestHelpCommand(t *testing.T) {
	isolateCLI(t)
	code, stdout, stderr := captureCLI(t, func() { helpCommand([]string{"token", "create"}) })
	if code != 0 || stdout != tokenCreateUsage {
		t.Fatalf("help token create: code=%d stderr=%s", code, stderr)
	}
	code, _, stderr = captureCLI(t, func() { helpCommand([]string{"nope"}) })
	if code != 2 || !strings.Contains(stderr, "unknown help topic") {
		t.Fatalf("unknown help topic: code=%d stderr=%s", code, stderr)
	}
}

func TestJoinHelpDoesNotRunDocker(t *testing.T) {
	isolateCLI(t)
	t.Setenv("PATH", t.TempDir())
	code, stdout, stderr := captureCLI(t, func() { cmdJoin([]string{"--help"}) })
	if code != 0 {
		t.Fatalf("code=%d stderr=%s", code, stderr)
	}
	if !strings.Contains(stdout, "Usage: kurrent join") {
		t.Fatalf("stdout:\n%s", stdout)
	}
}

func TestConfigPrintUnknownTemplate(t *testing.T) {
	isolateCLI(t)
	code, _, stderr := captureCLI(t, func() { configPrint([]string{"bogus"}) })
	if code != 2 || !strings.Contains(stderr, "supported values are init-defaults and node-defaults") {
		t.Fatalf("code=%d stderr=%s", code, stderr)
	}
}
