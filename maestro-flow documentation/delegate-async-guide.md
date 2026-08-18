---
title: "Delegate Async Execution Guide"
---

Asynchronous task delegation through independent worker processes, with broker-managed lifecycles, message injection, and MCP notifications.

---

## Quick Start

### Launching via Claude Code MCP

```bash
claude --dangerously-load-development-channels server:maestro --dangerously-skip-permissions
```

The MCP server registers 9 built-in tools (`edit_file`, `write_file`, `read_file`, `read_many_files`, `team_msg`, `team_mailbox`, `store_knowhow`, `team_tasks_mcp`, `team_agents`) — **the delegate family is not included**. Delegate subcommands (`message`, `status`, `output`, `tail`, `cancel`) are available only through the CLI and are not registered as MCP tools.

When an async delegate finishes, the notification is pushed to Claude Code over the MCP channel (`<channel source="maestro" ...>`) — no manual polling required. For detailed results, run `maestro delegate status <exec_id>` or `maestro delegate output <exec_id>` in a shell.

### Launching via CLI

```bash
# Async (background) — returns an execId immediately
maestro delegate "Analyze security vulnerabilities in the auth module" --to gemini --async

# Sync (foreground) — blocks until completion
maestro delegate "say hello" --to claude
```

---

## Command Reference

### Main Command

```bash
maestro delegate "<PROMPT>" [options]
```

| Option | Description | Default |
|------|------|--------|
| `--to <tool>` | Agent: gemini, agy, codex, claude, opencode | First enabled tool in the config |
| `--role <role>` | Capability role (analyze, explore, review, implement, plan, brainstorm, research) | — |
| `--mode <mode>` | `analysis` (read-only) or `write` (create/modify/delete) | `analysis` |
| `--effort <level>` | Reasoning effort (low, medium, high, max) | The tool's `reasoningEffort` setting |
| `--model <model>` | Model override | The tool's `primaryModel` |
| `--cd <dir>` | Working directory | Current directory |
| `--rule <template>` | Load protocol + prompt template | — |
| `--id <id>` | Execution ID | Auto: `{prefix}-{HHmmss}-{rand4}` |
| `--resume [id]` | Resume a previous session | — |
| `--includeDirs <dirs>` | Additional directories (comma-separated) | — |
| `--session <id>` | MCP session ID, used for notifications | Auto-detected |
| `--backend <type>` | `direct` or `terminal` | `direct` |
| `--timeout <ms>` | Idle timeout (ms) — force-terminates when the CLI produces no output for longer than this | 600000 (10 minutes) |
| `--async` | Run in the background, return immediately | Foreground |

### Subcommands

```bash
maestro delegate show                              # 20 most recent executions
maestro delegate show --all                        # Up to 100
maestro delegate status <id>                       # Broker + history status
maestro delegate status <id> --events 10           # With more broker events
maestro delegate output <id>                       # Assistant output (final reply only)
maestro delegate output <id> --full                # Full output (all entries, including tool calls)
maestro delegate output <id> --verbose             # With metadata and timestamps
maestro delegate output <id> --all                 # Include thinking/reasoning entries
maestro delegate output <id> --offset <n>          # Character offset
maestro delegate output <id> --limit <n>           # Maximum character count
maestro delegate tail <id>                         # Recent events + history
maestro delegate tail <id> --events 20 --history 20
maestro delegate cancel <id>                       # Request cancellation
maestro delegate message <id> "text"               # Inject a follow-up message
maestro delegate message <id> "text" --delivery after_complete
maestro delegate messages <id>                     # List queued messages
```

### Built-in MCP Tools

The MCP server registers the following 9 built-in tools (via `registerBuiltinTools()`) — **the delegate family is not included**:

| MCP Tool | Description |
|---------|------|
| `edit_file` | Edit a file (text replacement / line operations) |
| `write_file` | Write a file |
| `read_file` | Read a single file |
| `read_many_files` | Batch reads / directory traversal / content search |
| `team_msg` | Agent team message bus (JSONL) |
| `team_mailbox` | Team mailbox reads |
| `store_knowhow` | Knowledge entry storage and search |
| `team_tasks_mcp` | Team task management |
| `team_agents` | Team agent management |

> **Note**: Delegate subcommands (`message`, `status`, `output`, `tail`, `cancel`, `messages`) are invoked only through the CLI shell and are not registered as MCP tools. Async completion notifications are pushed over the MCP channel.

---

## Task Lifecycle

```
queued → running → completed
                 → failed
                 → cancelled
              ↗
         input_required
```

**Execution ID**: `{prefix}-{HHmmss}-{rand4}` (e.g. `gem-143022-a7f2`)
Prefixes: gemini→`gem`, agy→`agy`, codex→`cdx`, claude→`cld`, opencode→`opc`

<details>
<summary>Delegate vs CLI feature comparison</summary>

| Feature | `maestro cli` | `maestro delegate` |
|------|:---:|:---:|
| Synchronous execution | ✓ | ✓ |
| Asynchronous execution | — | ✓ `--async` |
| Prompt input | `-p "..."` | Positional argument `"..."` |
| Tool selection | `--tool` | `--to` |
| Mode (analysis/write) | ✓ | ✓ |
| Model override | ✓ | ✓ |
| Working directory | `--cd` | `--cd` |
| Rule template | `--rule` | `--rule` |
| Custom execution ID | `--id` | `--id` |
| Session resume | `--resume` | `--resume` |
| Backend selection | — | `--backend` |
| MCP session binding | — | `--session` |
| show (list executions) | ✓ | ✓ |
| output (fetch results) | ✓ | ✓ |
| output --verbose | ✓ | ✓ |
| watch (live stream) | ✓ | — |
| status (broker + history) | — | ✓ |
| tail (recent events) | — | ✓ |
| cancel | — | ✓ |
| message injection | — | ✓ |
| message after_complete | — | ✓ |
| MCP channel notifications | — | ✓ |
| Snapshot (latest output preview) | — | ✓ |

**Delegate is a complete replacement for CLI.** The CLI-only features (`watch`, `output --tail`) are convenience shortcuts.

</details>

---

## Message Delivery

| Mode | Behavior | Use For |
|------|------|------|
| `inject` | Routed to the running worker via stdin | Supplementary context, course correction |
| `after_complete` | Queues the message; relaunches after completion | Chained tasks, post-processing |

```bash
# Inject context into a running delegate
maestro delegate message gem-143022-a7f2 "Also check src/utils/sanitize.ts"

# Chaining: analyze → auto-fix
maestro delegate "Analyze auth security vulnerabilities" --to gemini --async
maestro delegate message gem-143022-a7f2 "Fix all critical vulnerabilities" --delivery after_complete
```

---

## Prompt Construction

Assembly order: **Mode protocol** → **User prompt** → **Rule template** (if specified)

### Prompt Template (6 fields)

```
PURPOSE: [goal] + [reason] + [success criteria]
TASK: [step 1] | [step 2] | [step 3]
MODE: analysis|write
CONTEXT: @[file patterns] | Memory: [context from prior work]
EXPECTED: [output format] + [quality standard]
CONSTRAINTS: [scope limits] | [special requirements]
```

### Rule Templates

**Analysis**: `analysis-trace-code-execution`, `analysis-diagnose-bug-root-cause`, `analysis-analyze-code-patterns`, `analysis-review-architecture`, `analysis-review-code-quality`, `analysis-analyze-performance`, `analysis-assess-security-risks`

**Planning**: `planning-plan-architecture-design`, `planning-breakdown-task-steps`, `planning-design-component-spec`, `planning-plan-migration-strategy`

**Development**: `development-implement-feature`, `development-refactor-codebase`, `development-generate-tests`, `development-implement-component-ui`, `development-debug-runtime-issues`

---

## Notification System

Dual channel: **MCP channel** (primary, push-based) + **Hook fallback** (JSONL file)

Throttling: `status_update` every 10s, `snapshot` every 15s.

---

## Stale-Stream Timeout

When a CLI process produces no output at all (stdout/stderr) within the specified time, the delegate broker force-terminates the process and marks it `failed`.

- **Default timeout**: 600000ms (10 minutes)
- **CLI override**: `delegate --timeout <ms>`
- **Config override**: the per-tool `streamTimeoutMs` field in `cli-tools.json`

Precedence: the `--timeout` CLI option > the `streamTimeoutMs` config > the 600000ms default.

---

## Proxy Configuration

`cli-tools.json` supports a global proxy configuration. Delegate injects the proxy environment variables into the child process environment before launching the CLI subprocess.

```json
{
  "proxy": {
    "enabled": true,
    "httpProxy": "http://127.0.0.1:7890",
    "noProxy": "127.0.0.1,localhost"
  },
  "tools": {
    "gemini": {
      "enabled": true,
      "primaryModel": "gemini-3.1-pro-preview"
    },
    "agy": {
      "enabled": true,
      "primaryModel": ""
    },
    "claude": {
      "enabled": true,
      "primaryModel": "claude-sonnet-4-6"
    }
  }
}
```

| Field | Description |
|------|------|
| `proxy.enabled` | Enable the global proxy |
| `proxy.httpProxy` | HTTP proxy URL |
| `proxy.noProxy` | Comma-separated bypass list |
| `tools.<name>.enabled` | Enable/disable a tool |
| `tools.<name>.primaryModel` | Default model |

At startup, Delegate TCP-probes the proxy for reachability; if it is unreachable, the proxy is skipped with a warning.

---

## Workflows

### Launch → Monitor → Fetch

```bash
maestro delegate "Analyze the auth module" --to gemini --async
# → execId: gem-143022-a7f2

maestro delegate status gem-143022-a7f2
# → status: running

maestro delegate output gem-143022-a7f2
# → Full analysis result
```

### Chaining: analyze → auto-fix

```bash
maestro delegate "Find all SQL injection vulnerabilities" --to gemini --async
maestro delegate message gem-143022-a7f2 "Fix all critical vulnerabilities" --delivery after_complete
```

### Cancel → Redirect

```bash
maestro delegate cancel gem-143022-a7f2
maestro delegate "Analyze only the payment module" --to gemini --async
```

---

## Related Documentation

- [Role Routing Guide](role-routing-guide.md) — the `--role` mapping mechanism and custom configuration
