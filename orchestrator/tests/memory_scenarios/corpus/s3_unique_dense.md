## 2026-09-04 12:00:00Z — T-201

Set `CODEX_SANDBOX` to danger-full-access or the shell fallback is auto-rejected before the command ever runs.

## 2026-09-04 11:00:00Z — T-202

The langgraph checkpointer needs an explicit thread_id in its configurable dict; omitting it silently starts a fresh graph state.

## 2026-09-04 10:00:00Z — T-203

Ollama's bridge exposes no apply_patch tool, so any task that must write a file structurally cannot succeed there.

## 2026-09-04 09:00:00Z — T-204

A BOM survives Windows pipelines and attaches to the first markdown header, so read episodic logs with utf-8-sig.

## 2026-09-04 08:00:00Z — T-205

Groq returns its remaining budget in x-ratelimit-remaining-tokens; the httpx event hook only attaches to a Groq client.

## 2026-09-04 07:00:00Z — T-206

Reviewer rework resets integration to the wave base SHA and replays kept merges in their original order.

## 2026-09-04 06:00:00Z — T-207

Gemini's CLI writes diagnostics to stdout, so the JSON parser must skip every line before the first opening brace.

## 2026-09-04 05:00:00Z — T-208

Daemon threads cannot be joined past interpreter shutdown, so bound a worker turn at the process-tree boundary instead.

## 2026-09-04 04:00:00Z — T-209

MongoDB rejects a dotted field name inside an update document; escape it before persisting arbitrary agent output.

## 2026-09-04 03:00:00Z — T-210

The supervisor router was exported and unit-tested but never wired into the graph, so its verdict never reached a caller.

## 2026-09-04 02:00:00Z — T-211

Copilot's adapter appends its own instruction file, which overrides a brief that names a different output directory.

## 2026-09-04 01:00:00Z — T-212

SQLite checkpoint writes need write-ahead logging enabled, otherwise a concurrent reader blocks the orchestrator thread.

## 2026-09-04 00:00:00Z — T-213

An em dash separates header fields; a peer reader's regex accepts nothing else and silently drops a hyphenated header.

## 2026-09-03 23:00:00Z — T-214

Anthropic's streaming endpoint terminates a turn with message_stop, not a bare connection close, so treat truncation as failure.

## 2026-09-03 22:00:00Z — T-215

Poetry's lock resolution ignores an editable path dependency during export, which drops it from the produced requirements file.
