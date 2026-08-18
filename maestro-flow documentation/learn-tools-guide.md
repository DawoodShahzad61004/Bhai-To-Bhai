---
title: "Learning Toolkit Guide"
---

The complete manual for the Maestro learning toolkit, covering the principles, usage, and collaboration patterns of the 4 `/maestro-learn` subcommands (`follow` / `investigate` / `decompose` / `consult`). Periodic retrospectives have moved to the `retrospective` pipeline step (see 2.1).

---

## 1. Overview

The learning toolkit is Maestro's **interactive deep-learning** module, focused on extracting structured knowledge from code, documentation, and decision history. Every command follows the scientific method — hypothesis, evidence, verification, consolidation — turning tacit engineering experience into reusable explicit knowledge.

### Difference from `/maestro-knowhow`

| Dimension | The `/maestro-learn` toolkit | `/maestro-knowhow` |
|------|---------------|--------------|
| Interaction model | Interactive deep learning, multi-round guidance | Atomic operation, single capture |
| Goal | Systematically build deep understanding | Quickly record a single insight |
| Artifacts | Structured reports, pattern catalog, evidence trail | A single `.workflow/knowhow/` entry |
| Duration | Several minutes, multiple agents in parallel | Seconds, completed instantly |

Simple rule: **use `/maestro-learn` when you need to think, use `/maestro-knowhow` when you need to record**.

---

## 2. Command Details

### 2.1 Retrospectives — `/learn-retro` has been retired

> **Command removed**: `/learn-retro` (along with `/learn-retro-git` and `/learn-retro-decision`) was deleted during the knowledge-management streamlining because its functionality overlapped with the quality retrospective. Periodic retrospectives are now handled by the `retrospective` pipeline step — it is dispatched by the orchestrator (there is no `/xxx` form), entered via `/maestro-next` or the quality pipeline, and reviews phase artifacts through the `technical` / `process` / `quality` / `decision` lenses, consolidating the resulting insights. See the Quality Pipeline Guide for usage.

---

### 2.2 `/maestro-learn follow` -- Guided Reading

Extracts deep understanding from code or documentation through section-by-section guided reading.

**Parameters**:

| Parameter | Description | Default |
|------|------|--------|
| `<target>` | File path / Wiki ID / topic keyword | Required |
| `--depth shallow\|deep` | Shallow (key structures and patterns) or deep (every function and branch) | `shallow` |
| `--save-wiki` | Save the reading notes as a wiki entry | Off |

<details>
<summary>Command examples</summary>

```bash
/maestro-learn follow src/auth/jwt.ts                     # Read through a specific file
/maestro-learn follow src/utils/ --depth deep              # Deep read of an entire directory
/maestro-learn follow arch-auth-design --save-wiki          # Read a wiki doc and save notes
```
</details>

**Target resolution**: file paths (containing `/` or `\`) are read directly; a Wiki ID invokes `wiki get`; topic text searches the wiki first, then the source code.

#### The 4 Mandatory Questions

| # | Question | What It Extracts |
|---|------|---------|
| 1 | What pattern is being used here? | Design patterns, idioms, conventions |
| 2 | Why was this approach chosen over the alternatives? | Trade-offs, options that were ruled out |
| 3 | What implicit assumptions does this code rely on? | Implicit contracts, input shapes, execution ordering |
| 4 | If this changed, what would break? | Fragile points, downstream blast radius |

The command automatically builds a **1-hop context neighborhood** (wiki references, import dependencies, downstream consumers) and cross-checks the extracted results against `coding-conventions.md`: already-documented items are marked "confirmed", undocumented ones are suggested for inclusion in the specs.

**Artifact paths**: `KNW-follow-{slug}-{date}.md` (understanding map), `specs/learnings.md` (consolidation)

---

### 2.3 `/maestro-learn decompose` -- Code Pattern Decomposition

Systematically decomposes complex code into a catalog of reusable design patterns, analyzed in parallel across 4 dimensions.

**Parameters**:

| Parameter | Description | Default |
|------|------|--------|
| `<target>` | File path / directory / module name | Required |
| `--patterns <list>` | Comma-separated pattern names to focus the analysis on | Detect all |
| `--save-spec` | Automatically invoke `/maestro-spec "<constraint>"` for each new pattern | Off |
| `--save-wiki` | Create a wiki note per dimension | Off |

<details>
<summary>Command examples</summary>

```bash
/maestro-learn decompose src/auth/                       # Decompose the auth module
/maestro-learn decompose src/utils/ --patterns "Factory,Observer,Strategy"  # Focus on specific patterns
/maestro-learn decompose src/core/ --save-spec --save-wiki  # Decompose and sync to spec and wiki
```
</details>

#### 4-Dimension Parallel Analysis

| Agent | Dimension | What It Detects |
|-------|------|---------|
| Structural | Structural patterns | Class hierarchies, composition, DI/IoC, Factory/Builder/Singleton, barrel exports |
| Behavioral | Behavioral patterns | Event flows, middleware chains, observer/pub-sub, command/strategy, state machines |
| Data | Data patterns | Repository/DAO, DTO pipelines, caching strategies (memo/LRU/TTL), serialization, schema validation |
| Error | Error patterns | Error boundaries, retry/backoff/circuit breaker, fallback chains, guard clauses, logging strategy |

Every finding carries: pattern name, dimension, confidence, code anchor (file:line), description, and trade-offs. Findings are compared against existing knowledge and marked documented / known / new; duplicates across dimensions are merged automatically.

**Artifact paths**: `KNW-decompose-{slug}-{date}.md` (pattern catalog), `specs/learnings.md` (consolidation)

---

### 2.4 `/maestro-learn consult` -- Multi-Perspective Analysis

Gets alternative perspectives on code, decisions, or plans, avoiding the blind spots of a single point of view.

**Parameters**:

| Parameter | Description | Default |
|------|------|--------|
| `<target>` | File path / Wiki ID / `HEAD` / `staged` / phase number | Required |
| `--mode` | `review` / `challenge` / `consult` | `review` |

<details>
<summary>Command examples</summary>

```bash
/maestro-learn consult src/auth/jwt.ts                    # Default review mode
/maestro-learn consult src/core/ --mode challenge          # Adversarial challenge
/maestro-learn consult HEAD --mode consult                 # Interactive Q&A
/maestro-learn consult 2 --mode review                     # Review the plan for Phase 2
```
</details>

#### The Three Modes

**Review (default)**: 3 agents reviewing in parallel

| Agent Role | Focus | Core Questions |
|-----------|--------|---------|
| Pragmatist | Simplicity, YAGNI, maintenance cost | "What is the simplest viable approach? What is the maintenance burden?" |
| Purist | Correctness, edge cases, type safety | "Which assumptions could be violated?" |
| Strategist | Extensibility, architectural consistency | "Does it support future growth? Does it fit the architecture?" |

Synthesized into: points of consensus, points of disagreement, an overall verdict, and the top 3 recommendations.

**Challenge**: a single adversarial agent that tries to find the weakest assumption, construct breaking scenarios, identify the biggest risk, and propose alternatives.

**Consult**: an interactive Q&A loop — the agent loads the target and answers your questions; say "done" to finish and compile the report.

**Artifact paths**: `KNW-opinion-{slug}-{date}.md` (analysis report), `specs/learnings.md` (consolidation)

---

### 2.5 `/maestro-learn investigate` -- Systematic Investigation

Uses the scientific method to investigate the "why" and "how" questions in a codebase — not to fix bugs, but to understand the system.

**Parameters**:

| Parameter | Description | Default |
|------|------|--------|
| `<question>` | The question to investigate | Required |
| `--scope <path>` | Restrict the search scope | The whole project |
| `--max-hypotheses N` | Maximum number of hypotheses; exceeding it triggers escalation | 3 |

<details>
<summary>Command examples</summary>

```bash
/maestro-learn investigate "What is the full lifecycle of a JWT refresh token"
/maestro-learn investigate "Why does queue consumption sometimes process duplicates" --scope src/queue/
/maestro-learn investigate "What cache invalidation strategies exist" --max-hypotheses 5
```
</details>

#### Hypothesis-Testing Flow

```
define the question → collect evidence → pattern matching → generate hypotheses → test hypotheses → synthesize report
                                                                                          ↑
                                                                        3-strike escalation mechanism
```

**Collect evidence**: 4 parallel channels — code search (Grep), file inspection, dependency tracing (import chains), and Git history.

**Generate hypotheses**: produces a ranked list based on the evidence, e.g. `[HIGH] JWT refresh uses a rotation strategy — Evidence: src/auth/jwt.ts:42`.

**Test hypotheses**: tested one by one in priority order and marked confirmed / disproved / inconclusive. All evidence is recorded in NDJSON format in `evidence.ndjson`.

**3-strike escalation**: when everything is inconclusive, the user is asked whether to widen the scope and re-hypothesize, or mark the investigation INCONCLUSIVE and produce a known-unknowns report.

**Artifact paths**: `KNW-investigate-{slug}/` (containing `evidence.ndjson`, `understanding.md`, `report.md`), `specs/learnings.md` (consolidation)

---

## 3. The Learning Data Flow

### Artifact Structure

Artifacts from all learning commands follow a unified storage convention:

```
.workflow/knowhow/                         # Main directory for learning artifacts
├── KNW-retro-{date}.md / .json            # Retrospective reports
├── KNW-follow-{slug}-{date}.md            # Guided reading notes
├── KNW-decompose-{slug}-{date}.md         # Pattern catalog
├── KNW-opinion-{slug}-{date}.md           # Second opinions
└── KNW-investigate-{slug}/                # Investigation directory
    ├── evidence.ndjson
    ├── understanding.md
    └── report.md
specs/learnings.md                         # Unified learning consolidation
```

### learnings.md Structure

Uses the closed `<spec-entry>` tag format with `category`, `keywords`, `date`, and `source` attributes to keep everything traceable.

### Knowledge Flow

- All commands **automatically** write a knowhow report and an entry in `specs/learnings.md`
- `--save-spec` / `--save-wiki` control whether results are further synced into the spec system and the wiki
- Duplicate findings are deduplicated automatically — existing knowledge is marked documented/known, and only new entries are consolidated

---

## 4. Use-Case Quick Reference

### Choosing a Command by Intent

| What you want to do | Command | Example |
|-----------|---------|------|
| Understand the design of an unfamiliar module | `/maestro-learn follow` | `src/auth/ --depth deep` |
| Learn the implicit conventions in some code | `/maestro-learn follow` | `src/utils/logger.ts` |
| Inventory a module's design patterns | `/maestro-learn decompose` | `src/core/ --save-spec` |
| Extract a reusable pattern library | `/maestro-learn decompose` | `src/ --save-wiki` |
| Review code quality (multi-perspective) | `/maestro-learn consult` | `src/api/` |
| Stress-test a proposal | `/maestro-learn consult` | `HEAD --mode challenge` |
| Ask the AI about a specific implementation | `/maestro-learn consult` | `plan.json --mode consult` |
| Understand "why does it work this way" | `/maestro-learn investigate` | `"What causes cache penetration"` |
| Trace the full path of a call chain | `/maestro-learn investigate` | `"The path from entry point to the database"` |

### Typical Workflow Combinations

| Scenario | Steps |
|------|------|
| **New member onboarding** | `/maestro-learn follow src/` → `/maestro-learn decompose src/core/ --save-wiki` → `retrospective` step (orchestrator-dispatched) |
| **Before an architecture decision** | `/maestro-learn follow src/auth/ --depth deep` → `/maestro-learn consult --mode review` → `/maestro-learn consult --mode challenge` → `/maestro-learn investigate "blast radius"` |
| **Iteration retrospective** | `retrospective` step → `/maestro-learn investigate "reasons for high churn"` → `/maestro-learn decompose --save-spec` |
| **Troubleshooting (to understand, not to fix)** | `/maestro-learn investigate "causes of latency"` → `/maestro-learn follow <key files>` → `/maestro-learn consult --mode consult` |

### Natural Transitions Between Commands

```
/maestro-learn follow → /maestro-learn decompose      # From understanding to pattern extraction
/maestro-learn follow → /maestro-learn consult        # From understanding to multi-perspective validation
/maestro-learn decompose → /maestro-spec "<constraint>"  # From pattern discovery to spec entry
retrospective step → /maestro-learn investigate       # From retrospective findings to deep investigation
/maestro-learn investigate → /maestro-learn follow    # From locating the problem to deep reading
/maestro-learn consult → /maestro-learn decompose     # From challenge to systematic decomposition
```
