package main

import (
	"encoding/base64"
	"encoding/binary"
	"hash/crc32"
	"strings"
	"testing"
)

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
