## 2026-09-01 12:00:00Z — T-101

The `npm ci` step failed with exit 1 because package-lock.json had drifted out of sync with package.json. Regenerating the lockfile via `npm install --package-lock-only` resolved it. Never hand-edit a lockfile to silence that mismatch.

## 2026-09-01 10:00:00Z — T-102

Pytest collection aborted under `--strict-markers` when a suite used an undeclared marker. Declaring it in the pytest.ini `markers` section fixed the run. A collection error is not a test failure and surfaces before any test executes.

## 2026-09-01 08:00:00Z — T-103

The Codex adapter reported an empty thread_id on every turn launched with `--ephemeral`. Resume then died with a no rollout found error. Drop that flag whenever a resumable session is required.

## 2026-09-01 06:00:00Z — T-104

Worktree cleanup left stale git index locks behind after a hard kill on Windows. Pruning the `.git/worktrees` entries unblocked the following run. Prefer `git worktree prune` over deleting those directories by hand.

## 2026-09-04 12:00:00Z — T-101

The `npm ci` step failed with exit 1 because package-lock.json had drifted out of sync with package.json. Regenerating the lockfile via `npm install --package-lock-only` resolved it. Never hand-edit a lockfile to silence that mismatch.

## 2026-09-04 10:00:00Z — T-102

Pytest collection aborted under `--strict-markers` when a suite used an undeclared marker. Declaring it in the pytest.ini `markers` section fixed the run. A collection error is not a test failure and surfaces before any test executes.

## 2026-09-04 08:00:00Z — T-103

The Codex adapter reported an empty thread_id on every turn launched with `--ephemeral`. Resume then died with a no rollout found error. Drop that flag whenever a resumable session is required.

## 2026-09-04 06:00:00Z — T-104

Worktree cleanup left stale git index locks behind after a hard kill on Windows. Pruning the `.git/worktrees` entries unblocked the following run. Prefer `git worktree prune` over deleting those directories by hand.

## 2026-09-07 12:00:00Z — T-101

The `npm ci` step failed with exit 1 because package-lock.json had drifted out of sync with package.json. Regenerating the lockfile via `npm install --package-lock-only` resolved it. Never hand-edit a lockfile to silence that mismatch.

## 2026-09-07 10:00:00Z — T-102

Pytest collection aborted under `--strict-markers` when a suite used an undeclared marker. Declaring it in the pytest.ini `markers` section fixed the run. A collection error is not a test failure and surfaces before any test executes.

## 2026-09-07 08:00:00Z — T-103

The Codex adapter reported an empty thread_id on every turn launched with `--ephemeral`. Resume then died with a no rollout found error. Drop that flag whenever a resumable session is required.

## 2026-09-07 06:00:00Z — T-104

Worktree cleanup left stale git index locks behind after a hard kill on Windows. Pruning the `.git/worktrees` entries unblocked the following run. Prefer `git worktree prune` over deleting those directories by hand.
