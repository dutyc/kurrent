package main

import (
	"strings"
	"testing"
)

func TestVersionOutput(t *testing.T) {
	orig := version
	defer func() { version = orig }()
	version = "v0.3.0"
	out := captureStdout(t, func() { cmdVersion(nil) })
	if strings.TrimSpace(out) != "kurrent v0.3.0" {
		t.Fatalf("unexpected version output: %q", out)
	}
}
