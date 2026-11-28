package main

import (
	"strings"
	"testing"
)

func TestNormalizeCpURL(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"plain", "https://cp.example.com", "https://cp.example.com"},
		{"trailing slash", "https://cp.example.com/", "https://cp.example.com"},
		{"api root suffix", "https://cp.example.com/api/cp", "https://cp.example.com"},
		{"api root with slash", "https://cp.example.com/api/cp/", "https://cp.example.com"},
		{"proxy prefix kept", "https://cp.example.com/proxy/api/cp", "https://cp.example.com/proxy"},
		{"port kept", "https://cp.example.com:8443/api/cp", "https://cp.example.com:8443"},
		{"api cp as path segment", "https://cp.example.com/api/cpx", "https://cp.example.com/api/cpx"},
	}
	for _, c := range cases {
		got, err := normalizeCpURL("--cp-url", c.in)
		if err != nil || got != c.want {
			t.Errorf("%s: got %q err=%v, want %q", c.name, got, err, c.want)
		}
	}
}

func TestValidateHTTPURLRejectsSensitiveParts(t *testing.T) {
	for _, raw := range []string{
		"https://user:pass@cp.example.com",
		"https://cp.example.com?x=1",
		"https://cp.example.com#frag",
		"ftp://cp.example.com",
		"https:///no-host",
	} {
		if _, err := validateHTTPURL("--server", raw); err == nil {
			t.Errorf("%q 应被拒绝", raw)
		}
	}
}

func TestValidateTokenID(t *testing.T) {
	if got, err := validateTokenID(" abcdef "); err != nil || got != "abcdef" {
		t.Fatalf("token id = %q, %v", got, err)
	}
	for _, raw := range []string{"abcdef.0123456789abcdef", "ABCDEF", "abcde", "abcdef0", ""} {
		if _, err := validateTokenID(raw); err == nil {
			t.Errorf("%q 应被拒绝", raw)
		}
	}
}

func TestValidateSerialAndReason(t *testing.T) {
	if got, err := validateSerial("ABCDEF0123"); err != nil || got != "abcdef0123" {
		t.Fatalf("serial = %q, %v", got, err)
	}
	if got, err := validateSerial("0xABCDEF"); err != nil || got != "abcdef" {
		t.Fatalf("0x serial = %q, %v", got, err)
	}
	for _, raw := range []string{"not-hex", "0x"} {
		if _, err := validateSerial(raw); err == nil {
			t.Errorf("%q 应被拒绝", raw)
		}
	}
	if _, err := validateRevocationReason("lost"); err == nil || !strings.Contains(err.Error(), "unspecified") {
		t.Fatalf("未知原因 err = %v", err)
	}
}
