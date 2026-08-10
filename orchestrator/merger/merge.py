"""Merging a wave's branches, and calling for help when git cannot.

The sequence is: merge each successful task branch into the integration branch in
turn; when one conflicts, leave the conflict exactly where it is and dispatch the
merge agent into it; verify the resolution against the filesystem; commit.

The verification is the part that matters. An agent reporting "resolved" is a
claim, and the specific way this claim goes wrong is well known — a conflict
marker left in a file. So the resolution is checked two ways before it is
committed: git must report no unmerged paths, and no file may still contain a
marker. Bugs.md #23 is the general form of this, where a supervisor read a file,
understood it, and approved bytes it could not see.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config
import parsing
import worktrees as wt
from adapters import run_agent
from logging_config import get_logger
from merger.prompts import MERGE_FRAME, merge_prompt

logger = get_logger(__name__)

AGENT = "merger"

# The merge agent edits files in the target repository and reads them back.
MERGE_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep", "Bash")

# A conflict marker at the start of a line. `git merge` writes exactly these.
_MARKER = re.compile(r"^(<{7}|={7}|>{7})", re.MULTILINE)


@dataclass
class MergeReport:
    """The outcome of merging one wave."""

    ok: bool
    merged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    conflicts_resolved: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    learnings: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    detail: str = ""

    def summary(self) -> str:
        parts = [f"{len(self.merged)} branch(es) merged"]
        if self.conflicts_resolved:
            parts.append(f"{len(self.conflicts_resolved)} conflict(s) resolved by the merge agent")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped (the task failed)")
        if self.unresolved:
            parts.append(f"UNRESOLVED: {', '.join(self.unresolved)}")
        return "; ".join(parts)


def markers_left_in(repo: Path | str, files: list[str]) -> list[str]:
    """Files that still contain a conflict marker after a claimed resolution."""
    remaining = []
    for name in files:
        path = Path(repo) / name
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, IsADirectoryError):
            continue
        if _MARKER.search(body):
            remaining.append(name)
    return remaining


def _resolve_conflict(
    *,
    target_repo: str,
    branch: str,
    into: str,
    task: dict[str, Any],
    files: list[str],
    ours: str,
    context_path: str,
    run_dir: str,
) -> tuple[bool, str, str, float]:
    """Dispatch the merge agent. Returns (ok, detail, learnings, cost)."""
    logger.warning(
        "[%s] %s conflicts with %s in: %s", AGENT, branch, into, ", ".join(files)
    )

    result = run_agent(
        merge_prompt(
            branch=branch,
            into=into,
            task_id=task.get("task_id", "?"),
            files=files,
            ours="Work merged from earlier tasks in this wave and previous waves.",
            theirs=task.get("report") or task.get("description") or "",
            context_path=context_path,
        ),
        spec=config.AGENTS[AGENT],
        system_prompt=MERGE_FRAME,
        cwd=target_repo,
        tag=f"{AGENT}-{task.get('task_id', 'x')}",
        tools=MERGE_TOOLS,
        extra_dirs=(run_dir,),
    )
    if not result.ok:
        return False, f"the merge agent failed ({result.error_kind}): {result.error_message[:200]}", "", result.cost_usd

    parsed = parsing.extract_json(result.text, result.structured)
    payload = parsed.value or {}
    status = parsing.one_of(payload, "status", ("resolved", "unresolvable"))
    learnings = parsing.require_str(payload, "learnings", allow_empty=True) or ""

    if status == "unresolvable":
        reason = parsing.require_str(payload, "unresolvable_reason") or "no reason given"
        return False, f"the merge agent judged this unresolvable: {reason}", learnings, result.cost_usd

    # A claim of resolution, now checked against the filesystem.
    #
    # The check is for conflict markers on disk, and it happens BEFORE staging.
    # Git's own unmerged list is the obvious thing to test and is the wrong one:
    # `git add` resolves a path in the index whatever its contents, so after
    # staging that list is empty for a correct resolution and a botched one
    # alike. What separates them is whether `<<<<<<<` is still in the file.
    with_markers = markers_left_in(target_repo, files)
    if with_markers:
        return (
            False,
            f"the merge agent reported success but conflict markers remain in: "
            f"{', '.join(with_markers)}",
            learnings,
            result.cost_usd,
        )

    # Staging is the pipeline's job: the brief tells the agent to edit the files
    # and to run no git commands at all, so nothing has marked these resolved.
    staged = wt.git(target_repo, "add", "-A")
    if not staged.ok:
        return (
            False,
            f"could not stage the resolved files: {staged.stderr[:200]}",
            learnings,
            result.cost_usd,
        )

    detail = parsing.require_str(payload, "summary") or "resolved"
    return True, detail, learnings, result.cost_usd


def merge_wave(
    *,
    target_repo: str,
    into: str,
    tasks: list[dict[str, Any]],
    context_path: str,
    run_dir: str,
) -> MergeReport:
    """Merge every successful task branch of one wave into `into`.

    A task that failed has nothing worth merging and is skipped by name rather
    than silently — its branch still exists, carrying whatever partial work it
    committed, so a later rework can start from it.
    """
    report = MergeReport(ok=True)

    if not config.USE_GIT_WORKTREES:
        # Shared-workspace mode: the wave ran against the target checkout, so
        # there is nothing to merge. Recorded rather than passed over quietly.
        report.detail = "USE_GIT_WORKTREES is off; the wave ran in the target checkout."
        return report

    checkout = wt.git(target_repo, "checkout", into)
    if not checkout.ok:
        report.ok = False
        report.detail = f"could not check out {into}: {checkout.stderr}"
        return report

    for task in tasks:
        task_id = task.get("task_id", "?")
        branch = task.get("branch", "")
        if not task.get("ok") or not branch:
            report.skipped.append(task_id)
            continue

        result = wt.merge(
            target_repo,
            branch=branch,
            into=into,
            message=f"Merge {task_id} ({branch})",
        )
        if result.ok:
            report.merged.append(task_id)
            logger.info("[%s] merged %s cleanly", AGENT, branch)
            continue

        files = wt.conflicted_files(target_repo)
        if not files:
            # Failed for a reason other than a conflict — a bad branch name, a
            # dirty tree. Not the merge agent's problem.
            wt.abort_merge(target_repo)
            report.ok = False
            report.unresolved.append(task_id)
            report.detail = f"merging {branch} failed without conflicts: {result.stderr[:300]}"
            return report

        resolved, detail, learning, cost = _resolve_conflict(
            target_repo=target_repo,
            branch=branch,
            into=into,
            task=task,
            files=files,
            ours=", ".join(report.merged),
            context_path=context_path,
            run_dir=run_dir,
        )
        report.cost_usd += cost
        if learning:
            report.learnings.append(learning)

        if not resolved:
            wt.abort_merge(target_repo)
            report.ok = False
            report.unresolved.append(task_id)
            report.detail = detail
            logger.error("[%s] could not merge %s: %s", AGENT, branch, detail)
            return report

        commit = wt.commit_all(target_repo, f"Merge {task_id} ({branch}) with conflicts resolved")
        if not commit.ok:
            wt.abort_merge(target_repo)
            report.ok = False
            report.unresolved.append(task_id)
            report.detail = f"could not commit the resolved merge: {commit.stderr[:300]}"
            return report

        report.merged.append(task_id)
        report.conflicts_resolved.append(task_id)
        logger.info("[%s] merged %s after resolving %d conflict(s)", AGENT, branch, len(files))

    report.detail = report.summary()
    return report
