---
title: "Search System Guide"
---

The Maestro search system is built on the BM25F algorithm and provides unified knowledge search across spec, knowhow, issue, domain, and other data sources.

For retrieval's role in the full knowledge loop, canonical identity, and the anti-concentration strategy, see
[Maestro Knowledge System Architecture](../docs/knowledge-system-architecture.md).

---

## Overview

`maestro search` is the unified search entry point for the knowledge system. It combines:
- **WikiIndexer** — BM25F-weighted full-text retrieval
- **MaestroGraph** — AST-level code symbol search (optional)
- **Type filtering** — filter by type: spec/knowhow/issue/domain, etc.

---

## Basic Usage

```bash
# Keyword search (1-3 core terms works best)
maestro search "authentication"

# With a type filter
maestro search "jwt token" --type spec

# Filter by category
maestro search --category coding

# Combined query
maestro search "oauth pkce" --type spec --category arch --limit 10

# Code search (requires MaestroGraph to be enabled)
maestro search "UserService" --code

# Unified KG search (MaestroGraph full-source; replaces the deprecated maestro kg search)
maestro search "UserService" --kg

# Search all sources (wiki + code) with unified normalized ranking
maestro search "UserService" --all

# Skip embeddings, use BM25 only (avoids the ONNX cold start)
maestro search "jwt token" --no-emb

# JSON output (for script consumption)
maestro search "jwt token" --json
```

### Query Best Practices

**1-3 core terms** is the optimal query length. Beyond 4 terms, the BM25 score gets diluted by irrelevant words:

```bash
# ❌ Piling up multiple unrelated keywords
maestro search "topology display frontend DetailedTopologySVG elk"

# ✅ Split into targeted queries
maestro search "topology layout"
maestro search "DetailedTopologySVG" --code
maestro search "elk layout" --type knowhow
```

**CamelCase identifiers** are split automatically: searching for `DetailedTopologySVG` also matches `detailed`, `topology`, `svg`, and the full identifier.

**IDF-adaptive weighting**: beyond 3 terms, the system automatically boosts highly specific terms (such as symbol names) and down-weights generic ones.

---

## The BM25F Algorithm

### Field Weights

The search system uses BM25F (Best Match 25 with Field weighting), assigning different weights to different fields. The system maintains separate configurations for three document classes:

**Default (standard documents such as spec/knowhow/issue)**

| Field | boost | b | Description |
|------|-------|---|------|
| `title` | 3 | 0.3 | Title matches carry the highest weight |
| `tags` | 2 | 0 | Tag matches, no length normalization |
| `summary` | 1.5 | 0.75 | Summary matches |
| `body` | 1 | 0.75 | Body matches (baseline) |

**KG (knowledge-graph virtual nodes)**

| Field | boost | b | Description |
|------|-------|---|------|
| `title` | 2 | 0.3 | Only the title contributes to the score |
| `tags` | 1 | 0 | Tag matches |
| `summary` | 0 | 0 | Does not contribute to the score |
| `body` | 0 | 0 | Does not contribute to the score |

**Scratch (scratch documents)**

| Field | boost | b | Description |
|------|-------|---|------|
| `title` | 1 | 0.3 | Title matches (lower weight) |
| `summary` | 0.5 | 0.75 | Summary matches |
| `tags` | 0.5 | 0 | Tag matches, no length normalization |
| `body` | 0.3 | 0.75 | Body matches |

### Scoring Formula

```
score = Σ_idf(tf~ × (k1 + 1)) / (tf~ + k1)
```

where `tf~` is the cross-field weighted term frequency:

```
tf~ = Σ(boost_f × tf_f / (1 - b + b × dl_f / avgdl_f))
```

- `tf_f` — term frequency within field f
- `dl_f` — document length of field f
- `avgdl_f` — average document length of field f
- `k1 = 1.5` — saturation parameter
- `boost` / `b` — set independently per configuration, as listed above

### Division-by-Zero Protection

When a field's `avgFieldLength = 0`, that field is skipped automatically to avoid a division-by-zero error.

### Time Decay

After BM25F + proximity reranking, search results have a time-decay weight applied that is based on the Ebbinghaus forgetting curve:

```
factor = floor + (1 - floor) × e^(-λ × age_days)
λ = ln2 / half_life
```

Half-life by type:

| Type | Half-life (days) |
|------|-------------|
| `domain` | 180 |
| `spec` | 60 |
| `knowhow` | 30 |
| `issue` | 14 |
| `project` / `roadmap` / `note` | 90 |

The decay floor is `floor = 0.3`, so even the oldest entries retain 30% of their original score.

Run `maestro spec health` to see overall freshness statistics for the knowledge base.

### Filtering Deprecated Entries

Entries with status `deprecated` (marked via `maestro spec supersede`) are excluded from search results by default. Use the `--include-deprecated` flag to include them:

```bash
maestro search "error handling" --include-deprecated
```

---

## Chinese Language Support

### CJK Tokenization

Chinese characters are automatically tokenized as bigrams + trigrams (`cjkNgrams`, n=2..3); single characters are not emitted on their own. (The examples below keep the original Chinese input, since they demonstrate how CJK text is tokenized.)

- Input `"认证"` ("authentication") → tokens: `["认证"]`
- Input `"用户认证"` ("user authentication") → tokens: `["用户", "户认", "认证", "用户认", "户认证"]`
- Input `"JWT认证"` ("JWT authentication") → tokens: `["jwt", "认证"]`

### Bilingual Index

doc-site search supports bilingual metadata:
- `name` / `name_zh` — English/Chinese command name
- `description` / `description_zh` — English/Chinese description
- `workflow_zh` — Chinese workflow description

---

## Deduplication

### Source-Level Deduplication

Multiple entries under the same `source.path` (for example a `spec-entry` and a `knowhow-entry`) are **not** deduplicated or merged — they are displayed independently.

### Query-Term Deduplication

Repeated query terms are merged automatically to prevent score inflation:
```bash
# "token token jwt" is equivalent to "token jwt"
maestro search "token token jwt"
```

---

## Index Sources

WikiIndexer automatically indexes the following data sources:

| Source | Path | Description |
|------|------|------|
| Project / Roadmap | `.workflow/project.md`, `roadmap.md` | Single-document project files |
| Spec | `.workflow/specs/` | Specification documents |
| Knowhow | `.workflow/knowhow/` | Knowledge entries |
| Issue | `.workflow/issues/` | Issue JSONL lines (virtual entries) |
| Domain | `.workflow/domain/` | Domain glossary |
| Run-mode Session | `.workflow/sessions/` | Session/Run lifecycle artifacts (see the next section) |
| Codebase / KG | `.workflow/codebase/`, `kg/` | doc-index components/features/ADRs, knowledge-graph nodes |
| Claude Code sessions | `~/.claude/projects/<slug>/` | Claude Code session transcripts (scanned automatically) |
| Codex sessions | `~/.codex/sessions/` | Codex session transcripts (scanned automatically) |

While building the index, WikiIndexer automatically selects the matching BM25F configuration (default/kg/scratch) based on the entry type.

### CLI Session Transcripts

Claude Code and Codex JSONL session transcripts are parsed into lightweight note entries (category `session`) and can be filtered with `--type session`. In hybrid search, each source is capped at 3 entries so that low-value sources cannot flood the results. On startup, the daemon monitors the CLI session directories and discovers new sessions automatically.

### Run-mode Session/Run Entries

Session/Run lifecycle artifacts under `.workflow/sessions/` enter the index according to these rules:

- **Only sealed/archived items are indexed**: sessions and runs in `running`/draft state are not indexed, consistent with the rule that aref only resolves sealed items;
- **Writer/reader version matrix**: the runtime writer currently always emits `session/1.3` + `command-run/1.3`. The wiki read side is compatible with `session/1.0`–`session/1.3` and `command-run/1.0`–`command-run/1.3` (i.e. `1.0-1.3`), normalizing everything to the current read model. Unknown Session/Run schema versions fail closed and produce no virtual entry (set `MAESTRO_DEBUG=1` to see the warning);
- **Live search/load**: sealed 1.3 Sessions/Runs produced by the real runtime writer can be found directly through `maestro search`; use the returned ID with `maestro load`. `artifacts/1.0` and `artifacts/1.1` remain compatible at the read boundary;
- **Entry shape**: one entry per sealed session plus one per sealed run (both of type `knowhow`). A run entry's body starts with a structured handoff section (`## 决策` Decisions / `## 约束` Constraints / `## 关注点` Concerns / `## 豁免` Exemptions — these headings are emitted literally in Chinese by the runtime), followed by the concatenated sealed artifact content (each file truncated at 50KB);
- **tags**: run entries carry `session`, `run`, the command name, `verdict:<verdict>`, `constraint` (when locked constraints are present), and the artifact kind (e.g. `diagnosis`, `review-findings`), supporting the `--kind` filter;
- **Topology**: a session entry's `related` points to all of its run entries and any promoted spec/knowhow (bidirectionally linked); a run entry's `parent` points back to the session, and aref references form cross-run edges. Search results expose the `sessionId`/`runId`/`runCount`/`related` fields for these entries.

---

## Credibility and Search Popularity

A search hit asynchronously increments the node's `search_hits` counter (via `CredibilityStore`), which feeds later credibility scoring. This is best-effort and does not block the search from returning.

---

## Search Cache Invalidator Hook

`search-cache-invalidator` is a PostToolUse hook that automatically rebuilds the WikiIndexer cache after file modifications:

- **Trigger**: after a Write or Edit tool call
- **Scope**: enabled only inside a workspace (`requiresWorkspace: true`)
- **Behavior**: automatically rebuilds the WikiIndexer index so search results reflect the latest file contents
- **Persistence version**: `search-cache.json` is currently at **cache v3** (`version: 3`); legacy cache generations are all rejected for reuse and rebuilt through the existing atomic path

This hook is enabled by default in the standard hook set and requires no manual configuration. When spec/knowhow and other files under `.workflow/` are modified via Write|Edit, the search index updates automatically.

---

## Performance Characteristics

| Optimization | Improvement | Description |
|--------|------|------|
| Cold-start optimization | ~3200ms → ~280ms | Daemon hot path + BM25-only fallback + background daemon startup |
| Backlinks construction | O(n²) → O(1) | Uses a Set instead of Array.includes |
| Inverted index | Prebuilt | Built on first load and reused thereafter |
| Candidate-set trimming | 3x limit | The candidate set is 3× the limit, returned after filtering |
| Workspace filtering | Applied before the limit | Filters before truncating results, so valid entries are not lost |
| Embedding skip | Automatically skipped for non-embedding queries | Falls back to BM25-only when the daemon is unavailable, avoiding the ONNX cold-start penalty |

---

## Search Daemon (resident process)

The search daemon is a resident background process that keeps the WikiIndexer and the ONNX embedding model hot in cache, avoiding the cold-start cost on every search.

### Basic Operations

```bash
# Start the daemon
maestro search-daemon start

# Stop the daemon
maestro search-daemon stop

# Check the daemon status
maestro search-daemon status
```

### How It Works

- **Protocol**: TCP on localhost, newline-delimited JSON
- **Lock file**: `.workflow/search-daemon.json` (records the PID + port)
- **Idle timeout**: shuts down automatically after 30 minutes without a request
- **ONNX hot cache**: the daemon preloads the embedding model at startup, so later searches do not reload it

### Automatic Fallback Strategy

When the daemon is unavailable, the search command falls back automatically:

1. Uses BM25-only mode (skipping embeddings) to avoid the ONNX cold start (~1800ms)
2. Starts the daemon in the background so later searches get embedding acceleration

```bash
# Daemon available: hot path, embeddings included
maestro search "query"          # ~280ms

# Daemon unavailable: falls back to BM25-only
maestro search "query"          # ~280ms (BM25-only)
maestro search "query" --no-emb # Explicitly skip embeddings
```

---

## Embedding Management

Maestro supports embedding-based semantic search, complementing BM25 full-text retrieval with vector similarity. For detailed configuration, see the [Embedding Model Configuration Guide](embedding-guide.md).

> **Note**: `embedding` is a standalone top-level command, not a subcommand of `search`. `maestro search embedding status` would be greedily captured by the variadic argument of `search <query...>` and treated as the search keywords `"embedding status"`.

```bash
# Check the embedding model status
maestro embedding status

# Warm up the embedding model
maestro embedding warmup

# Rebuild the embedding index
maestro embedding rebuild
```

**Quick setup**:

```bash
# Install the dependencies
npm install @huggingface/transformers onnxruntime-node

# Check the status
maestro embedding status

# Warm up the model (the first load is slower)
maestro embedding warmup
```

**Using a local model folder** (offline environments or custom models):

```bash
# Option 1: the config file ~/.maestro/local-embedding.json
echo '{"modelPath": "D:/models/multilingual-e5-small"}' > ~/.maestro/local-embedding.json

# Option 2: an environment variable (takes precedence over the config file)
export MAESTRO_EMBEDDING_MODEL_PATH="D:/models/multilingual-e5-small"
```

The model folder must contain `onnx/model.onnx` (or `model.onnx` in the root) plus `tokenizer.json` and `config.json`.

**Automatic fallback**: when embeddings are unavailable, search falls back to BM25-only mode automatically, with no manual intervention.

---

## Search Result Structure

```typescript
interface SearchResult {
  id: string;           // Unique identifier
  type: WikiNodeType;   // spec/knowhow/issue/domain/...
  title: string;        // Title
  category: string;     // coding/arch/review/...
  summary: string;      // Summary
  score: number;        // BM25F score
  snippet: string;      // Context snippet (keywords highlighted)
  source: { path: string };  // Source file path
  // Present only on run-mode session/run entries (topology exposure)
  sessionId?: string;   // Owning session
  runId?: string;       // Run ID of a run entry
  runCount?: number;    // Number of runs on a session entry
  related?: string[];   // session→runs / run→session + aref edges
}
```

---

## Filter Syntax

### Filter by Type

```bash
maestro search "query" --type spec       # Search specs only
maestro search "query" --type knowhow    # Search knowhow only
maestro search "query" --type issue      # Search issues only
maestro search "query" --type domain     # Search domain entries only
```

Valid types: `project`, `roadmap`, `spec`, `issue`, `knowhow`, `note`, `domain`, `session`, `scratch`

`session` and `scratch` are virtual type aliases; they actually filter by category (CLI session transcript entries).

### Filter by Category

```bash
maestro search "query" --category coding   # Coding conventions
maestro search "query" --category arch     # Architecture constraints
maestro search "query" --category review   # Review standards
maestro search "query" --category debug    # Debug notes
maestro search "query" --category test     # Test conventions
maestro search "query" --category learning # Lessons learned
```

### Filter by Artifact Kind

```bash
maestro search "timeout" --kind diagnosis         # Only runs containing diagnosis artifacts
maestro search "regression" --kind review-findings # Only review findings
maestro search "lessons" --kind lessons           # Only retrospective output
```

`--kind` matches entry tags exactly (run-mode run entries carry the artifact kind as a tag). It applies only to wiki results and cannot be combined with `--code`/`--kg`.

### Filter by Workspace

```bash
maestro search "query" --workspace shared  # Search a shared workspace
```

---

## Code Search

With the `--code` flag enabled, search also queries the MaestroGraph AST index:

```bash
maestro search "UserService" --code
```

Code search results are displayed separately and include:
- Symbol name and type (function/class/interface/...)
- File path and line number
- Function signature (when available)

---

## FAQ

### Search returns no results

1. Confirm that `.workflow/wiki-index.json` exists
2. Run `maestro wiki health` to check the index status
3. Try broader keywords

### Chinese search results are inaccurate

CJK tokenization operates at the bigram + trigram level, so short queries (2 characters or fewer) may not match well. Recommendations:
- Use keywords of 3 characters or more to trigger trigram matching
- Combine with `--category` filtering to narrow the scope

### Unexpected scores

If an entry scores unexpectedly high, the cause may be:
- A title-field hit (3× weight under the default configuration)
- A tags-field hit (2× weight, no length normalization)
- Heavy keyword repetition (already optimized, but it can still have an effect)

---

## Related Commands

```bash
# Unified search (recommended)
maestro search <query> [--type <type>] [--category <cat>] [--kind <kind>] [--code] [--kg] [--diversity balanced|off] [--all] [--no-emb] [--json]

# Wiki system search
maestro wiki search <query> [--json]
maestro wiki list [--type <type>] [--category <cat>] [--keyword <kw>]

# Knowledge-graph search (deprecated; use maestro search --kg instead)
maestro kg search <symbol>   # [deprecated] Use "maestro search --kg" instead
maestro kg context <node>

# Search Daemon
maestro search-daemon start   # Start the resident process
maestro search-daemon stop    # Stop the resident process
maestro search-daemon status  # Check the status

# Embedding management
maestro embedding status   # Check the embedding model status
maestro embedding warmup   # Warm up the embedding model
maestro embedding rebuild  # Rebuild the embedding index

# Index health check
maestro wiki health

# Knowledge health check (freshness, evolution-chain integrity)
maestro spec health
```

`--kg` shares the `--type`, `--category`, lifecycle, and diversity constraints with regular search. A KG result's `id` is a canonical ID that can be passed to `maestro load`; `graphId`/`aliases` preserve graph-traversal and historical-compatibility identities.
