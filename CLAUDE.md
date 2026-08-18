## Documentation First

Before making assumptions, asking the user a question, researching externally, or making a significant implementation/design decision, check the `docs/` directory for relevant existing knowledge.

Use the documentation selectively based on the task:
- `docs/Architecture.md` — current system design, components, boundaries, and implementation structure.
- `docs/Decisions.md` — architectural/design decisions and their rationale.
- `docs/Research.md` — prior research, tool evaluations, experiments, and technical findings.
- `docs/Bugs.md` — known, resolved, and open bugs, including previous diagnoses and lessons learned.
- `docs/Status.md` — project history, current progress, and notable recent changes.

Read only the files or sections relevant to the current question. Prefer documented project knowledge over guessing or asking the user for information that is already recorded. If the documentation does not answer the question or appears outdated, then inspect the codebase, use other available tools, or ask the user as appropriate.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:

- Treat `graphify-out/graph.json` as the persistent project knowledge base.
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current. After changing documentation, run the Graphify semantic update workflow so document nodes are refreshed too.


## Cross-Project Knowledge Discovery

### Purpose

The current repository may not contain all relevant implementation details, research, debugging notes, design decisions, or documentation. Related information may exist in sibling project directories under the parent directory of the current repository.

### When to Perform Cross-Project Discovery

Before implementing new functionality, modifying existing behavior, debugging, investigating user-reported issues, performing architectural analysis, or answering complex technical questions, determine whether relevant work may already exist in another project.
Also perform this workflow whenever the user explicitly mentions that related documentation, code, experiments, or research exist in another project directory.
Only perform this workflow when it is likely to improve the quality or correctness of the response. Do not perform unnecessary scans for trivial or self-contained tasks.

### Step 0: Gain Access First

Sibling directories are usually outside this session's granted file access by default. Before attempting to read anything outside the current repository:

- Check whether the parent directory (or the specific sibling) is already accessible.
- If not, ask the user to grant it, using the mechanism for the current tool:
  - Claude Code: `/add-dir <path>` (or add it to `permissions.additionalDirectories` in `.claude/settings.json` for a persistent grant)
  - Codex: relaunch with `--add-dir <path>`, or add it to `writable_roots` in `~/.codex/config.toml`
  - GitHub Copilot: ask the user to open a multi-root workspace including that folder, or attach the relevant file(s) directly via `#file`
- Do not guess or assume sibling contents if access has not been granted. Wait for confirmation.

### Discovery Workflow

1. Determine the parent directory of the current project.
2. List immediate sibling project directories only (do not recurse further).
3. Use directory names as a first-pass filter — prioritize siblings whose names plausibly relate to the current task before opening any files.
4. For each candidate sibling, read its `README.md` first. If no `README.md` exists, check for a `docs/index.md` or `docs/README.md` as a fallback; if neither exists, skip that sibling rather than opening arbitrary files.
5. If the sibling also has a `graphify`-generated knowledge graph file (e.g. `docs/knowledge_graph.*`), treat it as a secondary relevance signal alongside the README.
6. Use the README (and knowledge graph, if present) to determine whether that project is relevant to the current task.
7. If relevant, open that project's `docs/` directory and read only the documentation files relevant to the current task.
8. Treat the sibling project strictly as reference material. Reuse concepts, architecture, lessons learned, implementation ideas, and documented decisions where appropriate, but adapt them to the current repository instead of copying blindly.
9. Once relevant documentation has been read, retain its important information for the remainder of the current session so the same documentation does not need to be reread unnecessarily.
10. When using cross-project information in a response, briefly note which sibling project and files it came from, so the user can verify the source.

### Scope Restrictions

- Read only sibling projects that appear relevant.
- Read `README.md` (or its fallback) before opening any other files in a sibling project.
- Read only documentation under `docs/` unless the user explicitly requests inspection of source code.
- Avoid scanning every sibling project when a small number of likely candidates is sufficient.

### Safety Rules

Sibling projects are **read-only references**.
Under no circumstances may you:

- modify any sibling project;
- create, rename, move, or delete files in a sibling project;
- alter its directory structure;
- run formatting, linting, dependency updates, or Graphify updates inside a sibling project;
- generate commits or make write operations outside the current working repository.
  All write operations must remain confined to the current project unless the user explicitly changes the working repository or explicitly instructs you to modify another project.
  If access to a sibling project cannot be granted or the user declines, proceed with the task using only current-repository context, and note that the cross-project lookup was skipped.
