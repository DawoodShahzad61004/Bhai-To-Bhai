# Companion Log: Implement target-repo artifact categories, local Git exclusion, and persistent user choices
> run_id: 20260817-001-companion | session: 20260817-companion-artifact-layout

## Evidence

- Project knowledge search returned no governing artifact entry; the user-approved layout is authoritative.
- `orchestrator/artifacts.py` already separates flat shared files through `shared_dir`; remaining paths still use the controller `run_dir`.
- Prior verified tests cover concurrent OS-level learning appends and cross-run shared-path identity.
- Implemented run-keyed category paths for plans, tasks, reviews, and events under the target repository's `runs/` directory.
- Added an idempotent managed block to the target repository's local `.git/info/exclude`; only generated artifact paths are excluded, leaving other `runs/` files visible to Git.
- Changed user choices into an append-only, replay-idempotent ledger and injected prior context, choices, and learnings paths into the requirements survey prompt.
- Added current-run migration from the legacy flat layout without overwriting an existing destination.
- Focused artifact, requirements, and CLI tests passed.
- The complete `orchestrator/tests` suite passed after updating stale path assertions.
- `python -m compileall -q orchestrator` and `git diff --check` passed.
- Graphify's normal Windows update hit `[WinError 5]`; the established sequential update succeeded with 3,263 nodes and 4,381 edges, and the resulting JSON was validated.

## Work Log

- 2026-08-17T19:27:00+05:00 - Inspected artifact writers, consumers, Git behavior, and tests.
- 2026-08-17T19:41:00+05:00 - Implemented the approved category layout, local exclusion, persistent choices, and legacy migration.
- 2026-08-17T19:56:00+05:00 - Completed focused and full-suite verification.
- 2026-08-17T20:09:52+05:00 - Refreshed and validated the repository knowledge graph.

## Outcome

The approved artifact organization is implemented and verified. Controller run directories remain usable as identity and legacy-migration sources, while durable artifacts now live inside the target repository.
