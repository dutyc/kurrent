// CLI 帮助文本与 help 主题索引。
// 每个叶子命令都有自己的 usage（由 mustParse 在 --help/用法错误时输出）；
// 命令组输出简短索引；kurrent help <group> [verb] 打印同一份文本。
// 帮助文本统一用英文；运行时错误与业务输出仍按项目语言约定。
package main

import (
	"fmt"
	"strings"
)

const usageText = `kurrent - Kurrent control-plane CLI

Usage:
  kurrent [--credentials PATH] [--server URL] [--token T] <group> <verb> [args]
  kurrent help [group [verb]]

Global options (must precede the command group; defaults come from
KURRENT_CREDENTIALS / KURRENT_CP_URL / KURRENT_CP_TOKEN):
  --credentials  Management credential file (admin client certificate over 443 mTLS;
                 auto-discovered: control_plane/state/admin.conf, then next to the
                 executable, then ~/.kurrent/admin.conf)
  --server       Control-plane API root (overrides the server in the credential file;
                 required with --token when no management credential is found)
  --token        Management token (from kurrent token create --usage admin)

Commands:
  agents list [--no-live] [-o table|json]     List agents (with a live reachability probe)
  agents get <id>                             Show one agent
  agents add <id> --base-url URL [--role disk,cd] [--storager-ip IP] [--tags a,b] [--remark TEXT] [--disabled]
                                              <id> is the machine identity (12 lowercase hex
                                              characters); use --remark for a descriptive name
  agents edit <id> --base-url URL [...]       Replace an agent record (same flags as add;
                                              omitted --remark/--tags clear those fields)
  agents remove <id>                          Delete the agent record (including master tags)
  agents probe --base-url URL [--agent-id ID] Probe agent capabilities without persisting them
  token create [--usage bootstrap|admin] [--agent-id ID] [--cp-url URL]
               [--uses N] [--scope admin,readonly,enroll] [--ttl 24h]
                                              Issue a credential.
                                              bootstrap (default): --agent-id (12 lowercase hex
                                              characters; generated when omitted), --cp-url,
                                              --uses (1..1000, bounded by the control-plane
                                              bootstrapTokenMaxUses setting); descriptive names
                                              belong in metadata.remark.
                                              admin: --scope (admin/readonly/enroll), --ttl
                                              (default 24h; bounded by tokenMaxTtlHours,
                                              default 720h)
  token list [-o table|json]                  List unexpired tokens (plaintext is not returned)
  token revoke <token_id>                     Revoke a token: deleting the record invalidates it
  sessions list [-o table|json]               Management session ledger (admin; session_id = sha256(jti))
  sessions revoke <session_id>                Revoke one session; the next request fails with 401
  sessions revoke-all                         Sign out all sessions (clear the ledger, bump epoch)
  cert keygen <cn> [--out DIR] [--force]      Generate an RSA key and CSR locally (the key never
                                              leaves the host)
  cert issue <cn> --csr FILE [--days N] [--out DIR] [--force]
                                              Submit a CSR to the users CA and write <DIR>/<cn>.crt
  cert list [-o table|json]                   List certificates issued by the users CA (revoked and
                                              expired included)
  cert revoke <cn> [--serial HEX] [--reason REASON]
  cert components [-o table|json]             List nodes-domain certificates for agents and nvmet-host
  cert revoke-component <agent_id> [--serial HEX] [--reason REASON]
                                              Revoke a component certificate (--serial selects one):
                                              re-sign the CRL, reload nginx and drop sessions; the
                                              next TLS handshake is rejected
  credentials install | show | remove         User-level management credential snapshot: install
                                              copies admin.conf and its certificates to ~/.kurrent
                                              (re-run after rotation); show reports the active
                                              credential and snapshot status; remove deletes the
                                              snapshot (repository credentials are untouched)
  config print init-defaults|node-defaults    Print a declaration template (redirect to
                                              control_plane/kurrent.yaml or storager/kurrent.yaml)
  config validate [--node] [--config PATH] [--dir REPO]
  config show     [--node] [--config PATH] [--dir REPO]
                                              Validate against the runtime schema inside the
                                              component image / print the effective declaration
                                              with defaults injected; --node or
                                              kind=NodeConfiguration selects the node declaration
  config status                               Runtime declaration, configHash, dnsmasq.conf and
                                              server-certificate drift (via the management API)
  init [--config PATH] [--dir PATH]           Initialize the control plane: validate the
                                              declaration and converge services; roll back the
                                              previous declaration and exit non-zero when the
                                              health check fails
  join <cp-url> [--agent-id ID] [--config PATH] [--token T] [--dir PATH]
                                              Join a node: generate the declaration when missing
                                              or merge into the existing one and sync the address;
                                              validate inside the agent image before writing, then
                                              converge and start the agent (idempotent)
  workers list [-o table|json]                Worker ledger (read-only)
  workers get <id>                            Worker details (JSON)
  workers credential show <id> [-o table|json] Show the NVMe-oF authentication key and push status
  workers credential set <id> (--secret-file PATH | --secret-stdin | --generate)
                                              Rotate the authentication key (no plaintext in argv;
                                              --generate creates a 32-byte key in place without
                                              printing it)
  workers credential revoke <id>              Revoke the authentication key (the next boot is
                                              refused; failed pushes are retried by reconciliation)
  ops list [--limit N] [-o table|json]        Operation audit log (read-only)
  version                                     Print the version (injected via -ldflags, e.g. v0.3.0)

All commands support -h/--help. Unknown flags, missing values, repeated flags and
unexpected positional arguments are usage errors.
Exit codes: 0 success/help; 1 runtime failure; 2 usage error.
When a value starts with a dash, use --key=value or end flag parsing with --.
`

const helpUsage = `Usage:
  kurrent help                    Show the command overview
  kurrent help <group>            Show group help
  kurrent help <group> <verb>     Show help for one command

Example: kurrent help token create
`

const agentsGroupUsage = `Usage:
  kurrent agents list [--no-live] [-o table|json]
  kurrent agents get <id>
  kurrent agents add <id> --base-url URL [--role disk,cd] [--storager-ip IP] [--tags a,b] [--remark TEXT] [--disabled]
  kurrent agents edit <id> --base-url URL [--role disk,cd] [--storager-ip IP] [--tags a,b] [--remark TEXT] [--disabled]
  kurrent agents remove <id>
  kurrent agents probe --base-url URL [--agent-id ID]

  <id> is the machine identity: 12 lowercase hex characters; use --remark for a
  descriptive name. edit fully replaces the record: omitted --remark / --tags
  clear those fields.
`

const agentsListUsage = `Usage: kurrent agents list [--no-live] [-o table|json]

  --no-live        Skip the live reachability probe
  -o, --output     Output format: table (default) | json
`

const agentsGetUsage = `Usage: kurrent agents get <id>

  <id> is the machine identity: 12 lowercase hex characters.
`

const agentsAddUsage = `Usage: kurrent agents add <id> --base-url URL [--role disk,cd] [--storager-ip IP] [--tags a,b] [--remark TEXT] [--disabled]

  <id> is the machine identity: 12 lowercase hex characters; use --remark for a
  descriptive name.
  --base-url       Agent address reachable from the control plane (http(s)://, required)
  --role           Role set: disk,cd (default disk)
  --storager-ip    Data-plane address (IP; defaults to the base_url host)
  --tags           Comma-separated tags
  --remark         Human-readable remark (the only place for a descriptive name)
  --disabled       Disable the agent (drain semantics; enabled by default)
`

const agentsEditUsage = `Usage: kurrent agents edit <id> --base-url URL [--role disk,cd] [--storager-ip IP] [--tags a,b] [--remark TEXT] [--disabled]

  Full replacement: omitted --remark / --tags clear those fields.
  <id> is the machine identity: 12 lowercase hex characters.
`

const agentsRemoveUsage = `Usage: kurrent agents remove <id>

  Deletes the registration and master tags; certificates and disk files on the
  node are untouched.
`

const agentsProbeUsage = `Usage: kurrent agents probe --base-url URL [--agent-id ID]

  --base-url   Agent address to probe (http(s)://, required)
  --agent-id   Optional: registered node identity (12 lowercase hex characters)
               whose token should be reused
`

const tokenGroupUsage = `Usage:
  kurrent token create [--usage bootstrap|admin] [flags]
  kurrent token list [-o table|json]
  kurrent token revoke <token_id>

  bootstrap accepts only --agent-id / --cp-url / --uses;
  admin accepts only --scope / --ttl. The two sets cannot be mixed.
`

const tokenCreateUsage = `Usage: kurrent token create [--usage bootstrap|admin] [flags]

  bootstrap (default):
    --agent-id ID      Node identity to bind (12 lowercase hex characters;
                       generated when omitted)
    --cp-url URL       Control-plane entry written into the join command
                       (http(s)://; a mistakenly pasted /api/cp suffix is removed)
    --uses N           Maximum uses, 1..1000 and not above the control-plane
                       bootstrapTokenMaxUses setting (default 100; single use by
                       default)
  admin:
    --scope LIST       Comma-separated scopes: admin,readonly,enroll (default admin)
    --ttl DURATION     Lifetime (default 24h; capped by the control-plane
                       tokenMaxTtlHours setting, default 720h)
`

const tokenListUsage = `Usage: kurrent token list [-o table|json]

  Lists the ledger only; plaintext is never returned.
`

const tokenRevokeUsage = `Usage: kurrent token revoke <token_id>

  token_id is the 6-character prefix shown at issuance (see kurrent token list).
`

const sessionsGroupUsage = `Usage:
  kurrent sessions list [-o table|json]
  kurrent sessions revoke <session_id>
  kurrent sessions revoke-all
`

const sessionsListUsage = `Usage: kurrent sessions list [-o table|json]

  Lists unexpired sessions; session_id = sha256(jti).
`

const sessionsRevokeUsage = `Usage: kurrent sessions revoke <session_id>

  Deletes the registration; the next request fails with 401.
`

const sessionsRevokeAllUsage = `Usage: kurrent sessions revoke-all

  Clears every registration and bumps the epoch; replayed old cookies fail too.
`

const certGroupUsage = `Usage:
  kurrent cert keygen <cn> [--out DIR] [--force]
  kurrent cert issue <cn> --csr FILE [--days N] [--out DIR] [--force]
  kurrent cert list [-o table|json]
  kurrent cert revoke <cn> [--serial HEX] [--reason REASON]
  kurrent cert components [-o table|json]
  kurrent cert revoke-component <agent_id> [--serial HEX] [--reason REASON]

  keygen creates the private key and CSR locally (the key is never uploaded);
  issue submits the CSR to the users CA.
`

const certKeygenUsage = `Usage: kurrent cert keygen <cn> [--out DIR] [--force]

  --out     Output directory (default .); writes <DIR>/<cn>.key (0600) and <cn>.csr
  --force   Overwrite an existing key/CSR (the old private key cannot be recovered)
`

const certIssueUsage = `Usage: kurrent cert issue <cn> --csr FILE [--days N] [--out DIR] [--force]

  --csr     Local CSR file (required; run cert keygen first)
  --days    Certificate lifetime in days (>0; defaults to the control-plane policy)
  --out     Certificate output directory (default .); writes <DIR>/<cn>.crt
  --force   Overwrite an existing certificate file
`

const certListUsage = `Usage: kurrent cert list [-o table|json]

  Lists certificates issued by the users CA (revoked and expired included).
`

const certRevokeUsage = `Usage: kurrent cert revoke <cn> [--serial HEX] [--reason REASON]

  --serial   Revoke only the given serial (hex, optional 0x prefix); when omitted,
             revoke every unrevoked certificate for the CN
  --reason   unspecified (default) | key_compromise | superseded | cessation_of_operation
`

const certComponentsUsage = `Usage: kurrent cert components [-o table|json]

  Lists nodes-domain certificates for agents and nvmet-host (revoked and expired
  included).
`

const certRevokeComponentUsage = `Usage: kurrent cert revoke-component <agent_id> [--serial HEX] [--reason REASON]

  <agent_id> is 12 lowercase hex characters; --serial targets a single certificate
  (hex, optional 0x prefix).
  --reason   unspecified (default) | key_compromise | superseded | cessation_of_operation
`

const credentialsGroupUsage = `Usage:
  kurrent credentials install    Install management credentials into ~/.kurrent (self-contained snapshot)
  kurrent credentials show       Show the active credential and the user-level snapshot
  kurrent credentials remove     Delete the user-level snapshot (repository credentials are untouched)
`

const credentialsInstallUsage = `Usage: kurrent credentials install

  Copies the discovered management credential (admin.conf and its certificates)
  into ~/.kurrent; re-run after credential rotation.
`

const credentialsShowUsage = `Usage: kurrent credentials show

  Prints the active credential path, server and certificate file availability.
`

const credentialsRemoveUsage = `Usage: kurrent credentials remove

  Deletes only the user-level snapshot; repository credentials and issued tokens
  are unaffected.
`

const configGroupUsage = `Usage:
  kurrent config print init-defaults|node-defaults
  kurrent config validate [--node] [--config PATH] [--dir REPO]
  kurrent config show     [--node] [--config PATH] [--dir REPO]
  kurrent config status
`

const configPrintUsage = `Usage: kurrent config print init-defaults|node-defaults

  Prints a declaration template (redirect to control_plane/kurrent.yaml or
  storager/kurrent.yaml, then edit it).
`

const configInspectUsage = `Usage: kurrent config validate [--node] [--config PATH] [--dir REPO]
       kurrent config show     [--node] [--config PATH] [--dir REPO]

  --node          Validate as a node declaration (auto-detected when
                  kind=NodeConfiguration)
  --config PATH   Candidate declaration file (default depends on --node)
  --dir REPO      Repository root (default: discovered upward from the cwd)
`

const configStatusUsage = `Usage: kurrent config status

  Reads the runtime state: loaded declaration, configHash, dnsmasq.conf and
  server-certificate drift.
`

const initUsage = `Usage: kurrent init [--config PATH] [--dir PATH]

  --config   Control-plane declaration file (default <dir>/kurrent.yaml)
  --dir      control_plane directory (default: discovered from the cwd)

  Applies the declaration and converges services after full validation; rolls
  back the previous declaration and exits non-zero when the health check fails.
`

const joinUsage = `Usage: kurrent join <cp-url> [--agent-id ID] [--config PATH] [--token T] [--dir PATH]

  <cp-url>       Control-plane HTTPS entry (already carried by the command printed
                 by token create; a mistakenly pasted /api/cp suffix is removed)
  --agent-id     Node identity (12 lowercase hex characters; the identity bound
                 to the token)
  --config       Node declaration file (default <dir>/kurrent.yaml)
  --token        Bootstrap token (when omitted, bootstrap/agent.token must exist)
  --dir          storager directory (required when running outside the repository)
`

const workersGroupUsage = `Usage:
  kurrent workers list [-o table|json]
  kurrent workers get <id>
  kurrent workers credential show <id> [-o table|json]
  kurrent workers credential set <id> (--secret-file PATH | --secret-stdin | --generate)
  kurrent workers credential revoke <id>
`

const workersListUsage = `Usage: kurrent workers list [-o table|json]

  Lists the worker ledger.
`

const workersGetUsage = `Usage: kurrent workers get <id>

  Prints worker details (JSON).
`

const workersCredentialGroupUsage = `Usage:
  kurrent workers credential show <id> [-o table|json]
  kurrent workers credential set <id> (--secret-file PATH | --secret-stdin | --generate)
  kurrent workers credential revoke <id>

  set never accepts a plaintext key on the command line (it would leak into the
  process list and shell history); use --secret-file (0600 recommended) or
  --secret-stdin instead.
`

const workersCredentialShowUsage = `Usage: kurrent workers credential show <id> [-o table|json]

  Shows whether the authentication key exists and its per-agent push status.
`

const workersCredentialSetUsage = `Usage: kurrent workers credential set <id> (--secret-file PATH | --secret-stdin | --generate)

  --secret-file PATH   Read the DHHC-1 key from a file (group/other read
                       permission is rejected)
  --secret-stdin       Read the key from stdin (avoids argv leakage)
  --generate           Create a 32-byte random key in place without printing it

  Exactly one source is required.
`

const workersCredentialRevokeUsage = `Usage: kurrent workers credential revoke <id>

  Revokes the authentication key (the next boot is refused; failed pushes are
  retried by reconciliation).
`

const opsGroupUsage = `Usage:
  kurrent ops list [--limit N] [-o table|json]
`

const opsListUsage = `Usage: kurrent ops list [--limit N] [-o table|json]

  --limit   Number of entries (>0, default 50)
  -o        Output format: table (default) | json
`

const versionUsage = `Usage: kurrent version

  Prints the version.
`

var helpTopics = map[string]string{
	"":                          usageText,
	"help":                      helpUsage,
	"agents":                    agentsGroupUsage,
	"agents list":               agentsListUsage,
	"agents get":                agentsGetUsage,
	"agents add":                agentsAddUsage,
	"agents edit":               agentsEditUsage,
	"agents remove":             agentsRemoveUsage,
	"agents probe":              agentsProbeUsage,
	"token":                     tokenGroupUsage,
	"token create":              tokenCreateUsage,
	"token list":                tokenListUsage,
	"token revoke":              tokenRevokeUsage,
	"sessions":                  sessionsGroupUsage,
	"sessions list":             sessionsListUsage,
	"sessions revoke":           sessionsRevokeUsage,
	"sessions revoke-all":       sessionsRevokeAllUsage,
	"cert":                      certGroupUsage,
	"cert keygen":               certKeygenUsage,
	"cert issue":                certIssueUsage,
	"cert list":                 certListUsage,
	"cert revoke":               certRevokeUsage,
	"cert components":           certComponentsUsage,
	"cert revoke-component":     certRevokeComponentUsage,
	"credentials":               credentialsGroupUsage,
	"credentials install":       credentialsInstallUsage,
	"credentials show":          credentialsShowUsage,
	"credentials remove":        credentialsRemoveUsage,
	"config":                    configGroupUsage,
	"config print":              configPrintUsage,
	"config validate":           configInspectUsage,
	"config show":               configInspectUsage,
	"config status":             configStatusUsage,
	"init":                      initUsage,
	"join":                      joinUsage,
	"workers":                   workersGroupUsage,
	"workers list":              workersListUsage,
	"workers get":               workersGetUsage,
	"workers credential":        workersCredentialGroupUsage,
	"workers credential show":   workersCredentialShowUsage,
	"workers credential set":    workersCredentialSetUsage,
	"workers credential revoke": workersCredentialRevokeUsage,
	"ops":                       opsGroupUsage,
	"ops list":                  opsListUsage,
	"version":                   versionUsage,
}

func helpCommand(args []string) {
	if len(args) == 0 {
		fmt.Print(usageText)
		return
	}
	if args[0] == "-h" || args[0] == "-help" || args[0] == "--help" {
		fmt.Print(helpUsage)
		return
	}
	topic := strings.Join(args, " ")
	if text, ok := helpTopics[topic]; ok {
		fmt.Print(text)
		return
	}
	usageError(usageText, fmt.Errorf("unknown help topic: %s", topic))
}

func isHelpArg(a string) bool {
	return a == "help" || a == "-h" || a == "-help" || a == "--help"
}

// groupHelp 处理命令组的空参数/help：无参数或 help 后无主题时打印组帮助；
// “<group> help <verb>”转到 kurrent help <group> <verb>。返回 true 表示已处理。
func groupHelp(group string, args []string, usage string) bool {
	if len(args) == 0 {
		fmt.Print(usage)
		return true
	}
	if strings.HasPrefix(args[0], "-") && !isHelpArg(args[0]) {
		raw := strings.TrimLeft(args[0], "-")
		name, _, _ := strings.Cut(raw, "=")
		if global, ok := globalFlagNames[name]; ok {
			usageError(usage, fmt.Errorf("global flag %s must precede the command group", global))
		}
		usageError(usage, fmt.Errorf("unknown flag %s (%s accepts only subcommands)", args[0], group))
	}
	if !isHelpArg(args[0]) {
		return false
	}
	if len(args) == 1 {
		fmt.Print(usage)
		return true
	}
	helpCommand(append([]string{group}, args[1:]...))
	return true
}
