---
title: "Quality Pipeline Guide"
---

A complete reference for the Maestro quality pipeline: seven stages organized around the **"review → test → debug → refactor → retrospective"** closed loop. Of these, `review` / `test` / `auto-test` / `debug` / `retrospective` are **first-tier steps** dispatched by the orchestrator through the session chain (they are not standalone slash commands — you cannot type `/quality-*` directly). `refactor` has been folded into `/maestro-odyssey --mode improve`, and `sync` is handled by `maestro kg index`. The user-facing entry points are `/maestro "<intent>"` (the coordinator classifies the intent and builds a chain) or `/maestro-next`. The parameter blocks for each stage below describe its step interface, for reference when passing arguments through during chain construction.

---

## Command Overview

| Command | Role | Core Question | Artifact ID |
|------|------|---------|---------|
| `review` | Layered code review | Does the code meet quality standards? | `REV-{NNN}` |
| `test` | Conversational UAT | Does it work from the user's perspective? | `TST-{NNN}` |
| `auto-test` | Unified automated testing | Do coverage and regression pass? | `TST-{NNN}` |
| `debug` | Hypothesis-driven debugging | What is the root cause? | `DBG-{NNN}` |
| `/maestro-odyssey --mode improve` | Reflection-driven refactoring | Is technical debt converging? | `WBR-{NNN}` |
| `maestro kg index` | Documentation sync | Are the docs consistent with the code? | — |
| `retrospective` | Phase retrospective | What are the reusable insights? | `INS-{8hex}` |

---

## review — Layered Code Review

```bash
review <phase> [--level quick|standard|deep] [--dimensions security,architecture,...] [--skip-specs]
```

| Parameter | Description |
|------|------|
| `<phase>` | Required. Phase number or slug |
| `--level` | Review level: `quick` / `standard` / `deep`. Auto-detected by default |
| `--dimensions` | Comma-separated review dimensions; overrides the level's defaults |

**Three review levels**: Quick (inline review for small changes) → Standard (parallel agents reviewing by dimension, with automatic deep-dive) → Deep (multi-round aggregation)

Artifact path: `scratch/{YYYYMMDD}-review-P{N}-{slug}/review.json`

| Verdict | Meaning | Next Step |
|---------|------|--------|
| `PASS` | All dimensions passed | `test {phase}` |
| `WARN` | Non-critical issues; safe to continue | `test {phase}` |
| `BLOCK` | Critical issues; must be fixed | `plan {phase} --gaps` |

---

## test — Conversational UAT

```bash
test [phase] [--smoke] [--auto-fix]
```

| Parameter | Description |
|------|------|
| `--smoke` | Inject a smoke test before UAT |
| `--auto-fix` | Automatic gap-fix loop (verify → plan --gaps → execute → re-verify, up to 2 rounds) |

**Flow**: Extract scenarios from `verification.json` → walk through each scenario interactively → automatically infer severity (blocker/major/minor/cosmetic) → debug issues in parallel by gap cluster

Artifact path: `scratch/{YYYYMMDD}-test-P{N}-{slug}/` (uat.md, test-plan.json, test-results.json)

| Condition | Next Step |
|------|--------|
| Everything passed | `/maestro-session-seal` |
| `--auto-fix` succeeded | `review {phase}` |
| Issues remain | `debug --from-uat {phase}` |
| Insufficient coverage | `auto-test {phase}` |

---

## auto-test — Unified Automated Testing

```bash
auto-test <phase> [--max-iter N] [--layer L0-L3] [--strategy name] [--dry-run] [--re-run] [-y]
```

| Parameter | Description |
|------|------|
| `--max-iter N` | Maximum iterations (default 5) |
| `--layer L` | Target a specific layer (L0/L1/L2/L3) |
| `--dry-run` | Generate the plan only; do not execute |
| `--re-run` | Re-run only the failed scenarios |

**Smart routing**:

| Priority | Condition | Route |
|--------|------|------|
| 1 | An active session exists | Resume the session |
| 2 | `--re-run` + prior failures | Re-run failures |
| 3 | REQ-*.md files exist | spec route |
| 4 | Coverage gaps exist | gap route |
| 5 | Default | code route |

**Layer waves**: L0→L1→L2→L3 executed in order, with parallel CSV writes + parallel CSV diagnosis

Artifact path: `scratch/{YYYYMMDD}-auto-test-P{N}-{slug}/` (test-plan.json, scenarios.csv, report.json)

| Condition | Next Step |
|------|--------|
| Converged (≥95%) | `test {phase}` |
| Bugs found | `debug --from-uat {phase}` |
| Max iterations, >80% | `test {phase}` |
| Max iterations, <80% | `debug {phase}` |

---

## debug — Hypothesis-Driven Debugging

```bash
debug [issue description] [--from-uat <phase>] [--parallel]
```

| Mode | Trigger | Symptom Source |
|------|---------|---------|
| Standalone | Provide the issue description directly | Collected interactively |
| UAT handoff | `--from-uat` | Loaded from `uat.md` |
| Parallel | `--parallel` | A dedicated agent per gap cluster |

**Debug loop**: symptom collection → hypothesis generation → isolation and verification → root cause confirmation → readiness gate → stress testing

Artifact path: `scratch/{YYYYMMDD}-debug-P{N}-{slug}/` (understanding.md, evidence.ndjson)

| Condition | Next Step |
|------|--------|
| Root cause found | `plan {phase} --gaps` |
| UAT handoff + auto-fix | `test {phase} --auto-fix` |
| Conclusion unclear | Resume the debug session |

---

## /maestro-odyssey --mode improve — Reflection-Driven Refactoring

```bash
/maestro-odyssey --mode improve [<scope>]    # scope: module path | feature area | all
```

Each round: **analyze** (identify impact) → **plan** (execute after confirmation) → **reflect** (test verification + strategy adjustment)

Artifact path: `scratch/{YYYYMMDD}-refactor-{scope}/reflection-log.md`

---

## maestro kg index — Documentation Sync

```bash
maestro kg index [--full] [--since <commit|HEAD~N>] [--dry-run]
```

Detect changes via `git diff` → track the impact chain through `doc-index.json` → update the `.workflow/codebase/` documentation.

---

## retrospective — Phase Retrospective

```bash
retrospective [phase|N..M] [--lens technical|process|quality|decision] [--all] [--no-route] [--compare N] [-y]
```

Four parallel lenses (Technical / Process / Quality / Decision), with automatic insight routing:

| Route Target | Condition |
|---------|------|
| Spec stub | Reusable patterns/constraints |
| Issue | Recurring gaps |
| Knowhow tip | Process notes/reminders |
| Learnings | All insights (always) |

---

## Quality Loop Flow

```
                    ┌──────────────────────┐
                    │ Phase execution done │
                    └───────────┬──────────┘
                                │
                    ┌───────────▼──────────┐
             ┌──────┤ review               │
             │      └───────────┬──────────┘
             │ BLOCK            │ PASS/WARN
             │                  ▼
    ┌────────▼────────┐  ┌──────▼───────────┐
    │ plan --gaps     │  │ test / auto-test │
    │ (fix)           │  │ (testing)        │
    └────────┬────────┘  └────────┬─────────┘
             │ apply fixes        │ issues found
             ▼                    ▼
    ┌─────────────────┐     ┌─────▼────────────┐
    │ execute         │◄────┤ debug            │
    └────────┬────────┘ fix └─────┬────────────┘
             │ root cause found   │
             │                    │
    ┌────────▼─────────┐          │
    │ re-run test loop │◄─────────┘
    └────────┬─────────┘
             │ all passing
             ▼
    ┌───────────────────────────────────────────────────────┐
    │ /maestro-odyssey --mode improve (optional, tech debt) │
    │ maestro kg index (sync the codebase index)            │
    │ retrospective step (retro, knowledge feedback)        │
    └───────────────────────────────────────────────────────┘
```

<details>
<summary>Decision tree: which command to use when</summary>

```
Code was just executed
  ├─ Need a code quality assessment? ──> review <phase>
  │    ├─ PASS/WARN ──> continue to testing
  │    └─ BLOCK ──> plan <phase> --gaps
  │
  ├─ Need user acceptance? ──> test <phase>
  │    ├─ All passed ──> /maestro-session-seal
  │    └─ Issues found ──> debug --from-uat <phase>
  │
  ├─ Need automated testing? ──> auto-test <phase>
  │    ├─ Converged ──> test <phase>
  │    └─ Bugs found ──> debug --from-uat <phase>
  │
  ├─ Have a known bug? ──> debug "<issue>"
  │    ├─ Root cause clear ──> plan <phase> --gaps
  │    └─ Still uncertain ──> keep debugging
  │
  ├─ Need to reduce tech debt? ──> /maestro-odyssey --mode improve <scope>
  │    ├─ Tests pass ──> maestro kg index
  │    └─ Tests fail ──> debug <scope>
  │
  ├─ Code changed but docs did not? ──> maestro kg index
  │
  └─ Phase finished and needs a retrospective? ──> retrospective <phase>
       ├─ Insights found ──> auto-routed to spec/issue/knowhow
       └─ After completion ──> maestro session status
```

</details>

---

## Integration with the Phase Pipeline

`execute` (which includes the built-in verification gate E2.7) is the standard entry point for the quality commands:

```bash
execute 1 → review 1 → auto-test 1 → test 1 → retrospective 1
```

`--gaps` is the core bridge between the quality pipeline and the Phase pipeline:

| Trigger Scenario | Command |
|---------|------|
| `review` returns BLOCK | `plan {phase} --gaps` |
| `debug` confirms a root cause | `plan {phase} --gaps` |
| `test --auto-fix` | Automatically invokes `plan --gaps → execute → verify` |

**Pre-milestone-audit checkpoint**: all phases verified → critical phases reviewed → core features tested → issues closed out → retrospective completed
