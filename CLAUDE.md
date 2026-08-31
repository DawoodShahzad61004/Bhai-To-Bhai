# Bhai-To-Bhai — Project Instructions

Your global instruction file (`~/.claude/CLAUDE.md` for Claude Code, `~/.codex/AGENTS.md` for Codex) applies here in full and is **not** repeated below. In one line each:

- **Coding Philosophy** — simplicity, minimal scoped diffs, fix the cause instead of suppressing the symptom, no unsolicited docs or reports.
- **Graphify Knowledge Gate** — this repo has a graph at `graphify-out/graph.json`: query it before analysis, planning, or edits; `graphify update .` after code changes, the semantic `/graphify . --update` workflow after documentation changes.
- **Second Brain** — personal, career, and cross-project knowledge lives in the vault and is reached through the `second-brain` skill; search it before answering anything this repo's `docs/` doesn't cover, capture only the durable generalizable lesson back, and never mirror this repo's `docs/` into it.

## Documentation First

Before assuming, asking the user, researching externally, or making a significant implementation or design decision, check `docs/` for existing knowledge — read only the file or section the task needs:

- `docs/Architecture.md` — current system design, components, boundaries, implementation structure.
- `docs/Decisions.md` — architectural and design decisions with their rationale.
- `docs/Research.md` — prior research, tool evaluations, experiments, technical findings.
- `docs/Bugs.md` — known, resolved, and open bugs, with past diagnoses and lessons.
- `docs/Status.md` — project history, current progress, notable recent changes.

Prefer documented project knowledge over guessing or asking for something already recorded. If the docs don't answer the question or look outdated, inspect the codebase, use other tools, or ask.

## Cross-Project Knowledge Discovery

Sibling projects under this repo's parent directory may already hold relevant implementation, research, debugging notes, or decisions. Check them before implementing new functionality, changing behavior, debugging, doing architectural analysis, or answering a complex technical question that this repo's own `docs/` doesn't cover — and whenever the user says related work exists in another project directory. Skip it for trivial or self-contained tasks.

1. **Get access first.** Siblings are normally outside this session's granted paths. Ask the user to grant it — Claude Code: `/add-dir <path>` or `permissions.additionalDirectories` in `.claude/settings.json`; Codex: relaunch with `--add-dir <path>` or `writable_roots` in `~/.codex/config.toml`; Copilot: a multi-root workspace or an attached `#file`. Never guess sibling contents before access exists.
2. **Filter by directory name.** List immediate siblings only, no recursion, and prioritize the plausibly related ones before opening anything.
3. **Read `README.md` first** — fallback `docs/index.md` or `docs/README.md`; if neither exists, skip that sibling rather than opening arbitrary files. An existing knowledge graph there is a secondary relevance signal.
4. **Then read only the relevant files under that sibling's `docs/`** — source code only if the user explicitly asks. Retain what you learn for the rest of the session instead of rereading.
5. **Cite the sibling project and files** whenever their information shapes a response, so the user can verify it.

Siblings are strictly read-only references: adapt their concepts and lessons to this repo rather than copying blindly, and never create, modify, rename, move, or delete files there, alter structure, run formatters, linters, dependency updates, `graphify`, or commits inside one. All writes stay in this repository unless the user explicitly changes the working repository. If access is declined or unavailable, continue with this repo's context alone and say the cross-project lookup was skipped.
