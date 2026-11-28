package command

import (
	"errors"
	"kurrent/cli/internal/cli"
	"strings"
	"testing"
)

func testSpec() cmdSpec {
	return cmdSpec{
		usage: "Usage: test\n",
		flags: []flagSpec{
			{name: "name", kind: flagString},
			{name: "output", aliases: []string{"o"}, kind: flagString},
			{name: "force", kind: flagBool},
			{name: "required", kind: flagString, required: true},
		},
		minPos: 1, maxPos: 2, posNames: []string{"first", "second"},
	}
}

func TestParseCommandMixedOrder(t *testing.T) {
	p, err := parseCommand([]string{"first", "--name", "x", "-o=json", "--force", "--required", "r", "second"}, testSpec())
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(p.pos) != 2 || p.pos[1] != "second" {
		t.Fatalf("positional args = %v", p.pos)
	}
	if p.opt("name", "") != "x" || p.opt("output", "") != "json" || !p.bool("force") {
		t.Fatalf("flag parsing error: %+v", p)
	}
	if !p.has("force") || !p.has("output") || p.has("missing") {
		t.Fatalf("has() semantics mismatch: %+v", p.set)
	}
}

func TestParseCommandHelpWins(t *testing.T) {
	for _, args := range [][]string{
		{"--help"},
		{"-h"},
		{"first", "--help", "--bogus"},
		{"--name", "--help"},
	} {
		if _, err := parseCommand(args, testSpec()); !errors.Is(err, errHelp) {
			t.Errorf("%v: err = %v, want errHelp", args, err)
		}
	}
}

func TestParseCommandErrors(t *testing.T) {
	cases := []struct {
		name string
		args []string
		want string
	}{
		{"unknown flag", []string{"--bogus", "first", "--required", "r"}, "unknown flag"},
		{"duplicate", []string{"--name", "a", "--name", "b", "first", "--required", "r"}, "specified more than once"},
		{"missing value", []string{"--name"}, "requires a value"},
		{"required missing", []string{"first"}, "missing required flag"},
		{"required empty", []string{"first", "--required="}, "must not be empty"},
		{"too few", []string{"--required", "r"}, "missing argument <first>"},
		{"too many", []string{"a", "b", "c", "--required", "r"}, "unexpected positional argument"},
		{"bool with separate value", []string{"first", "--force", "false", "--required", "r"}, "does not take a separate value"},
		{"bool bad value", []string{"first", "--force=1", "--required", "r"}, "accepts only true or false"},
		{"global after group", []string{"first", "--server", "x", "--required", "r"}, "must precede the command group"},
		{"secret argv", []string{"first", "--secret", "x", "--required", "r"}, "--secret-file"},
	}
	for _, c := range cases {
		_, err := parseCommand(c.args, testSpec())
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: err = %v, want contains %q", c.name, err, c.want)
		}
	}
}

func TestParseCommandBoolFalse(t *testing.T) {
	p, err := parseCommand([]string{"first", "--force=false", "--required", "r"}, testSpec())
	if err != nil {
		t.Fatal(err)
	}
	if !p.has("force") || p.bool("force") {
		t.Fatalf("--force=false should explicitly disable: set=%v value=%v", p.has("force"), p.bool("force"))
	}
}

func TestParseCommandTerminator(t *testing.T) {
	p, err := parseCommand([]string{"--required", "r", "--", "--not-a-flag", "-x"}, testSpec())
	if err != nil {
		t.Fatal(err)
	}
	if len(p.pos) != 2 || p.pos[0] != "--not-a-flag" || p.pos[1] != "-x" {
		t.Fatalf("args after -- must all be positional: %v", p.pos)
	}
}

func TestOutputFormat(t *testing.T) {
	p := parsedArgs{opts: map[string]string{}}
	if got, err := p.outputFormat(); err != nil || got != "table" {
		t.Fatalf("default output = %q, %v", got, err)
	}
	p.opts["output"] = "json"
	if got, err := p.outputFormat(); err != nil || got != "json" {
		t.Fatalf("json output = %q, %v", got, err)
	}
	p.opts["output"] = "yaml"
	if _, err := p.outputFormat(); err == nil {
		t.Fatal("unknown output formats must be rejected")
	}
}

func TestParseGlobalFlags(t *testing.T) {
	oldServer, oldToken, oldCred := cli.Server, cli.Token, cli.Credentials
	t.Cleanup(func() { cli.Server, cli.Token, cli.Credentials = oldServer, oldToken, oldCred })
	cli.Server, cli.Token, cli.Credentials = "", "", ""

	args, err := parseGlobalFlags([]string{"--server", "https://cp.example.com/", "--token", " tok ", "agents", "list"})
	if err != nil {
		t.Fatal(err)
	}
	if len(args) != 2 || args[0] != "agents" || args[1] != "list" {
		t.Fatalf("positional args = %v", args)
	}
	if cli.Server != "https://cp.example.com" || cli.Token != "tok" {
		t.Fatalf("global flags = server=%q token=%q", cli.Server, cli.Token)
	}
}

func TestParseGlobalFlagsErrors(t *testing.T) {
	oldServer, oldToken, oldCred := cli.Server, cli.Token, cli.Credentials
	t.Cleanup(func() { cli.Server, cli.Token, cli.Credentials = oldServer, oldToken, oldCred })
	cli.Server, cli.Token, cli.Credentials = "", "", ""

	cases := []struct {
		name string
		args []string
		want string
	}{
		{"unknown", []string{"--bogus", "agents"}, "unknown global flag"},
		{"duplicate", []string{"--token", "a", "--token", "b", "agents"}, "specified more than once"},
		{"missing value", []string{"--server"}, "requires a value"},
		{"empty", []string{"--token=", "agents"}, "must not be empty"},
		{"bad url", []string{"--server", "not-a-url", "agents"}, "http(s)"},
	}
	for _, c := range cases {
		if _, err := parseGlobalFlags(c.args); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: err = %v, want contains %q", c.name, err, c.want)
		}
	}

	if _, err := parseGlobalFlags([]string{"--help"}); !errors.Is(err, errHelp) {
		t.Errorf("--help err = %v", err)
	}
}
