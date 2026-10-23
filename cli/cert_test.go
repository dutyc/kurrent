package main

import "testing"

func TestCertStatus(t *testing.T) {
	cases := []struct {
		name string
		in   certInfo
		want string
	}{
		{"active", certInfo{}, "active"},
		{"revoked wins over expired", certInfo{RevokedAt: "2026-10-23T00:00:00Z", Expired: true}, "revoked"},
		{"expired", certInfo{Expired: true}, "expired"},
	}
	for _, c := range cases {
		if got := certStatus(c.in); got != c.want {
			t.Errorf("%s: certStatus() = %q, want %q", c.name, got, c.want)
		}
	}
}

func TestShortFingerprint(t *testing.T) {
	long := "0123456789abcdef0123"
	if got := shortFingerprint(long); got != "0123456789abcdef" {
		t.Errorf("shortFingerprint(long) = %q", got)
	}
	if got := shortFingerprint("abc"); got != "abc" {
		t.Errorf("shortFingerprint(short) = %q", got)
	}
}
