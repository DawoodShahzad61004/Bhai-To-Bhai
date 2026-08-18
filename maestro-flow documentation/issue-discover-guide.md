---
title: "Issue Discovery Guide"
---

The complete manual for the Maestro Issue system, covering the full discovery, management, and closed-loop workflow.

---

## 1. Overview

The Maestro Issue system is an issue-tracking mechanism that is independent of the Phase pipeline. The Phase pipeline (analyze -> plan -> execute -> verify) drives predefined development tasks, while the Issue system captures and manages problems discovered in the codebase.

The two can run independently or work together:

- **Independent operation**: discover and manage issues directly, without affecting Phase progress
- **Linked mode**: issues are injected into the Phase pipeline via the `--gaps` parameter, driving root-cause analysis and fixes

`/maestro-issue discover` is the entry point to the Issue system and offers two discovery modes:

- **Multi-perspective full scan**: 8 specialized perspectives analyzed in parallel, covering code quality comprehensively
- **Prompt-driven exploration**: deep, targeted exploration around the user's area of concern

Discovery results are automatically deduplicated, turned into issue records, and fed into the closed-loop workflow.

---

## 2. maestro-issue discover in Detail

### Basic Usage

```bash
/maestro-issue discover                              # Interactive mode selection
/maestro-issue discover multi-perspective            # 8-perspective full scan
/maestro-issue discover by-prompt "Check API error handling"  # Prompt-driven
/maestro-issue discover multi-perspective -y         # Skip confirmation
/maestro-issue discover multi-perspective --scope=src/auth/**  # Restrict the scope
/maestro-issue discover by-prompt "Database query performance" --depth=deep  # Deep exploration
```

### Parameter Reference

| Parameter | Description | Default |
|------|------|--------|
| _(no arguments)_ | Interactive mode selection | -- |
| `multi-perspective` | 8-perspective parallel scan | -- |
| `by-prompt "..."` | Prompt-driven exploration | -- |
| `-y` / `--yes` | Skip confirmation prompts | Confirmation required |
| `--scope=<pattern>` | File scan scope | `**/*` |
| `--depth=standard\|deep` | Exploration depth (by-prompt only) | `standard` |

---

### 8-Perspective Full Scan Mode

Launches parallel analysis across 8 specialized perspectives (4 agents per batch):

```
Batch 1: security, performance, reliability, maintainability
Batch 2: scalability, ux, accessibility, compliance
```

Each perspective agent scans the source files, records `file:line` evidence, assesses severity (critical/high/medium/low), and suggests a fix direction.

<details>
<summary>Perspective definitions (8 dimensions)</summary>

| Perspective | Focus Areas | Core Question |
|------|---------|---------|
| **SECURITY** | Authentication, authorization, input validation, secret management, injection attacks | What security vulnerabilities or unsafe patterns exist? |
| **PERFORMANCE** | N+1 queries, unbounded loops, missing caches, memory leaks, large payloads | What performance bottlenecks or inefficient patterns exist? |
| **RELIABILITY** | Error handling, retry logic, race conditions, data integrity, graceful degradation | Which failure modes are unhandled or could cause data loss? |
| **MAINTAINABILITY** | Code duplication, tight coupling, missing abstractions, unclear naming, dead code | What makes the codebase harder to understand or change? |
| **SCALABILITY** | Hard-coded limits, single-threaded bottlenecks, stateful assumptions, schema rigidity | What will break as load/data/users grow? |
| **UX** | Confusing flows, missing feedback, inconsistent behavior, accessibility gaps | What creates friction or confusion for end users? |
| **ACCESSIBILITY** | Screen readers, keyboard navigation, color contrast, ARIA labels, focus management | What barriers exist for users with disabilities? |
| **COMPLIANCE** | Missing logging, audit trails, data retention, privacy controls, regulatory requirements | Which regulatory or policy requirements are unmet? |

</details>

#### Result Deduplication

Raw findings from all perspectives are merged and deduplicated: grouped by `file:line`, entries with >80% description similarity are merged, and the higher severity is retained.

#### Sample Output

```
Discovery Session: DBP-20260513-143022
Mode: multi-perspective
Raw findings: 47 → Unique issues: 31

Severity: critical(3) high(8) medium(12) low(8)
Next: /maestro-issue list --severity critical
```

---

### by-prompt Mode

Prompt-driven mode performs deep, targeted exploration around the user's area of concern.

**Execution flow**:

1. Decompose the user prompt into 3-5 exploration dimensions (search patterns + file patterns + discovery criteria)
2. Run semantic and pattern searches for each dimension, collecting code snippets
3. Explore iteratively (up to 3 rounds): identify issues -> refine the search -> final sweep
4. Deduplicate and create issue records

**Suitable for**: troubleshooting a specific module, targeted security audits, dependency analysis before a refactor, systematic triage of user-reported issues.

**When no prompt is given**, the system offers preset directions to choose from: Error handling gaps / API contract violations / Test coverage gaps / Custom.

---

### Artifact Paths

Each discovery session creates artifacts under `.workflow/issues/discoveries/{SESSION_ID}/` (session ID format: `DBP-YYYYMMDD-HHmmss`):

| File | Description |
|------|------|
| `discovery-state.json` | Session metadata and progress tracking |
| `discovery-issues.jsonl` | Issues created in this session |
| `{PERSPECTIVE}-findings.json` | Raw findings per perspective (full scan) |
| `exploration-plan.json` | Exploration dimension definitions (by-prompt) |
| `{dimension}-context.md` | Code context for each dimension |
| `exploration-log.md` | Round-by-round exploration log |

---

### How Findings Become Issues

1. Severity maps to priority: `critical->1`, `high->2`, `medium->3`, `low->4`
2. Generate an issue ID (`ISS-YYYYMMDD-NNN`), scanning to avoid collisions
3. Build the full issue record (including `context.location`, `fix_direction`, `tags`)
4. Write to both `issues.jsonl` (global) and `discovery-issues.jsonl` (session record)
5. Initial status `registered`, source `discovery`

---

## 3. maestro-issue in Detail

`/maestro-issue` handles issue lifecycle management and supports 6 subcommands.

### Basic Usage

```bash
/maestro-issue create --title "Memory leak" --severity high
/maestro-issue list --severity critical --status open
/maestro-issue status ISS-20260513-001
/maestro-issue update ISS-20260513-001 --status in_progress --priority 1
/maestro-issue close ISS-20260513-001 --resolution "Memory leak fixed"
/maestro-issue link ISS-20260513-001 --task TASK-003
```

---

### Subcommand Details

<details>
<summary>create -- Create an issue</summary>

```bash
/maestro-issue create --title "Title" [options]
```

| Option | Description | Default |
|------|------|--------|
| `--title TEXT` | Title (**required**) | Prompted interactively |
| `--severity VALUE` | critical / high / medium / low | `medium` |
| `--source VALUE` | planned / supplement / bug / review / verification / discovery / manual | `manual` |
| `--phase VALUE` | Phase reference | -- |
| `--milestone VALUE` | Milestone reference (inferred automatically from `state.json`) | -- |
| `--description TEXT` | Detailed description | Prompted interactively |
| `--priority NUMBER` | 1-5; lower means higher priority | `3` |
| `--tags TAG1,TAG2` | Tag list | -- |

After creation, an ID is generated automatically (`ISS-YYYYMMDD-NNN`), you are prompted to add context, and `supplement`-type issues are checked for cross-milestone conflicts.

</details>

<details>
<summary>list -- List issues</summary>

| Option | Description |
|------|------|
| `--status VALUE` | open / in_progress / completed / failed / deferred |
| `--phase VALUE` | Filter by phase |
| `--milestone VALUE` | Filter by milestone |
| `--severity VALUE` | Filter by severity |
| `--source VALUE` | Filter by source |
| `--all` | Include closed issues (read from `issue-history.jsonl`) |

Output is sorted by ascending priority and descending severity.

</details>

<details>
<summary>status / update / close / link</summary>

**status** shows the full issue details (title, status, severity, description, fix direction, context, tags, history, feedback):

```bash
/maestro-issue status ISS-20260513-001
```

**update** updates fields; status changes are recorded automatically in `issue_history`:

```bash
/maestro-issue update ISS-20260513-001 --status in_progress --priority 1 --add-tag urgent
# Optional: --severity, --tags, --phase, --milestone, --fix-direction, --description, --note
```

**close** closes the issue and moves it into the history list:

```bash
/maestro-issue close ISS-20260513-001 --resolution "Fix description" [--status completed|failed|deferred]
```

**link** creates a bidirectional association (Issue `affected_components` <-> Task `issue_refs`):

```bash
/maestro-issue link ISS-20260513-001 --task TASK-003
```

</details>

---

### issues.jsonl Format

All issues are stored as JSONL. Key fields:

```json
{
  "id": "ISS-20260513-001",
  "title": "Refresh token is not rotated correctly",
  "status": "registered",
  "priority": 1,
  "severity": "critical",
  "source": "discovery",
  "phase_ref": "01-auth",
  "milestone_ref": "MVP",
  "description": "...",
  "fix_direction": "Use a database lock to guarantee atomicity",
  "context": { "location": "src/auth/token.ts:45", "suggested_fix": "..." },
  "tags": ["SECURITY", "auth"],
  "affected_components": ["src/auth/token.ts"],
  "issue_history": [{ "from_status": null, "to_status": "registered", "note": "Issue created" }]
}
```

| Storage Location | Description |
|---------|------|
| `.workflow/issues/issues.jsonl` | Active issues |
| `.workflow/issues/issue-history.jsonl` | Closed issues (archive) |

---

### Status Transitions

```
registered -> open -> in_progress -> completed
                                -> failed
                                -> deferred
```

| Status | Description | Trigger |
|------|------|------|
| `registered` | Initial (created by discover) | Automatic discovery |
| `open` | Confirmed and pending | Manual creation/confirmation |
| `in_progress` | Being worked on | Fix started |
| `completed` | Resolved | Fix verified |
| `failed` | Handling failed | Fix failed |
| `deferred` | Postponed | Low priority or dependencies not ready |

---

## 4. The Issue Closed Loop

### Standard Flow

```
discover -> list -> analyze -> plan -> execute -> verify -> close
```

```bash
# 1. Discover
/maestro-issue discover multi-perspective

# 2. Review the results
/maestro-issue list --severity critical
/maestro-issue status ISS-20260513-001

# 3-5. Root cause analysis → solution planning → apply the fix
#      (--gaps injects the issue into the Phase pipeline; the orchestrator
#       dispatches analyze → plan → execute in sequence)
/maestro "Fix ISS-20260513-001"

# 6. Close
/maestro-issue close ISS-20260513-001 --resolution "Fix description"
```

### Fast Path

For urgent or simple issues, `/maestro-next` can skip the intermediate steps:

```bash
/maestro-next "Fix the token rotation race condition"
/maestro-issue close ISS-20260513-001 --resolution "Fixed via /maestro-next"
```

### Integration with Roadmap/Milestone

- **Milestone association**: `--milestone` sets the owning milestone (inferred automatically from `state.json` when omitted); `supplement`-type issues are automatically checked for cross-milestone conflicts
- **Phase association**: `--phase` links to a phase; `--gaps` converts the issue into a gap injected into the analysis flow; `link` creates a bidirectional association between an issue and a task
- **Roadmap feedback**: issue statistics (count, severity distribution, fix rate) inform planning; phases with a high issue density may need to be split; `supplement` items can serve as requirement input for the next milestone

The Commander agent automatically identifies unanalyzed issues and drives them forward; combined with hooks, this enables a fully automatic closed loop.
