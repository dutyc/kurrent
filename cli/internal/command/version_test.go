package command

import (
	"strings"
	"testing"
)

func TestVersionOutput(t *testing.T) {
	orig := Version
	defer func() { Version = orig }()
	Version = "v0.3.0"
	out := captureStdout(t, func() { cmdVersion(nil) })
	if strings.TrimSpace(out) != "kurrent v0.3.0" {
		t.Fatalf("unexpected version output: %q", out)
	}
}
