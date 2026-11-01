package main

import (
	"encoding/base64"
	"encoding/binary"
	"hash/crc32"
	"strings"
	"testing"
)

// TestValidateSecretInput：只有完整的 DHHC-1:01（32B 密钥 + CRC32 小端）才被接受。
func TestValidateSecretInput(t *testing.T) {
	valid, err := generateDHHC1()
	if err != nil {
		t.Fatal(err)
	}
	if got, err := validateSecretInput(valid + "\n"); err != nil || got != valid {
		t.Fatalf("合法密钥应通过: got=%q err=%v", got, err)
	}

	parts := strings.SplitN(valid, ":", 3)
	payload, err := base64.StdEncoding.DecodeString(parts[2])
	if err != nil {
		t.Fatal(err)
	}
	corrupt := append([]byte(nil), payload...)
	corrupt[0] ^= 0xff
	badCRC := "DHHC-1:01:" + base64.StdEncoding.EncodeToString(corrupt)
	short := "DHHC-1:01:" + base64.StdEncoding.EncodeToString(payload[:20])

	for _, c := range []struct {
		name string
		in   string
		want string
	}{
		{"empty", "  ", "empty"},
		{"prefix", "AABB-1:01:" + parts[2], "invalid secret format"},
		{"type", "DHHC-1:02:" + parts[2], "invalid secret format"},
		{"truncated", short, "invalid secret payload"},
		{"bad crc", badCRC, "CRC32"},
		{"bad base64", "DHHC-1:01:!!!", "invalid secret payload"},
	} {
		if _, err := validateSecretInput(c.in); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: err = %v, want contains %q", c.name, err, c.want)
		}
	}
}

// TestGenerateDHHC1：生成密钥满足固件契约（前缀、两位类型、36 字节载荷、CRC32 小端终值）。
func TestGenerateDHHC1(t *testing.T) {
	secret, err := generateDHHC1()
	if err != nil {
		t.Fatalf("生成密钥: %v", err)
	}
	if !strings.HasPrefix(secret, "DHHC-1:01:") {
		t.Fatalf("前缀/类型不符: %q", secret)
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(secret, "DHHC-1:01:"))
	if err != nil {
		t.Fatalf("base64 解码: %v", err)
	}
	if len(raw) != 36 {
		t.Fatalf("载荷 %d 字节，want 36（32 密钥 + 4 CRC）", len(raw))
	}
	key, crc := raw[:32], raw[32:]
	if got := crc32.ChecksumIEEE(key); got != binary.LittleEndian.Uint32(crc) {
		t.Fatalf("CRC32 非小端终值: got %x want %x", binary.LittleEndian.Uint32(crc), got)
	}
	other, err := generateDHHC1()
	if err != nil {
		t.Fatalf("再次生成: %v", err)
	}
	if secret == other {
		t.Fatal("两次生成密钥相同（随机源异常）")
	}
}
