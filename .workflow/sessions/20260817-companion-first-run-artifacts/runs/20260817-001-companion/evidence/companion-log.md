# Companion Log: Fix first-run artifact initialization for every applicable artifact
> run_id: 20260817-001-companion | session: 20260817-companion-first-run-artifacts

## Evidence

- Loaded the governing target-repository artifact layout from the prior sealed companion run.
- The reported first-run failure is a missing `target_repo/runs/context.md` read by the requirements agent.
- Audited every production artifact path and prompt consumer. Only context, learnings, user choices, the learnings lock, and JSONL events have valid empty representations.
- Plans, tasks, and reviews are stage-owned structured outputs; creating placeholders would incorrectly signal completed work.
- Added initialization after legacy migration so old artifacts win and existing cross-run data is never overwritten.
- Added a requirements regression that asserts every readable-empty path exists before the agent dispatch begins.

## Work Log

### 20:20 - Implemented first-run initialization

Added atomic create-if-absent initialization for the five valid empty artifacts and exposed the learnings lock as a resolved artifact path.

### 20:22 - Added regression coverage

Covered pre-dispatch readability, preservation of existing content, valid empty events, and lazy structured outputs.

### 20:25 - Verified the complete pipeline

Focused tests passed, all 221 orchestrator tests passed, compilation and diff checks passed.

### 20:26 - Refreshed repository graph

The normal Windows updater hit the known permission error; sequential extraction succeeded and the graph JSON validated at 3,268 nodes and 4,389 edges.

## Outcome

**Status:** done
**Summary:** A brand-new target repository now has every safely initializable artifact before the requirements agent receives its paths. Existing project memory remains untouched, while plans, task files, and reviews remain truthful stage outputs.
**Files:** `orchestrator/artifacts.py`, `orchestrator/tests/test_foundation.py`, `orchestrator/tests/test_requirements.py`
