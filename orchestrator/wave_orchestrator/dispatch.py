"""Running one wave: worktrees out, coding subagents in, reports back.

Three properties here are the ones worth reading.

**Concurrency costs a thread each and nothing more.** Every coding subagent is a
`subprocess.run` that spends its whole life blocked on a pipe, which releases the
GIL — the parallelism is in the child processes and Python is only waiting. Both
agents are submitted before either result is collected, because a submit followed
by its own `.result()` would serialise them for no reason.

Unlike the sandbox's worker loop, this does **not** need a daemon thread: every
task is waited for, so nothing is ever abandoned to `concurrent.futures`' atexit
hook (docs/Bugs.md #18). The deadline lives one level down, in the adapter's
`subprocess.run(timeout=...)`, which can actually kill the thing it bounds.

**A killed agent still leaves a diff.** The pipeline commits each worktree itself
after the turn, on top of anything the agent committed. ADR-005's premise is that
resumption must not depend on an outgoing agent behaving correctly under duress,
and an agent killed mid-turn has committed nothing.

**A report is a claim, not evidence.** Every task result carries both what the
agent said and what git observed. Bugs.md #21 is an agent reporting a file it had
no tool to write; the filesystem is what caught it, and the same check is here.
"""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config
import parsing
import worktrees as wt
from adapters import run_agent
from logging_config import get_logger
from wave_orchestrator.prompts import CODING_FRAME, coding_prompt

logger = get_logger(__name__)

# The tools a coding subagent needs. Bash is included because a task's acceptance
# criteria routinely name a command to run; the sandbox is the worktree.
CODING_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep", "Bash")


@dataclass
class TaskOutcome:
    """What one coding subagent did, as it claims and as git observed."""

    task_id: str
    ok: bool
    # The agent's own words.
    status: str = ""  # "done" | "blocked" | "" when unreadable
    report: str = ""
    claimed_files: list[str] = field(default_factory=list)
    learnings: str = ""
    # What actually happened.
    branch: str = ""
    worktree: str = ""
    commit: str = ""
    changed_files: list[str] = field(default_factory=list)
    # For the reviewer's rework loop, which must reach this same agent.
    session_id: str = ""
    cost_usd: float = 0.0
    error_kind: str = ""
    error_message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "ok": self.ok,
            "status": self.status,
            "report": self.report,
            "claimed_files": self.claimed_files,
            "changed_files": self.changed_files,
            "branch": self.branch,
            "worktree": self.worktree,
            "commit": self.commit,
            "session_id": self.session_id,
            "cost_usd": round(self.cost_usd, 4),
            "error_kind": self.error_kind,
            "error_message": self.error_message,
        }

    def evidence(self) -> str:
        """One line separating what the agent said from what it did.

        The reviewer routes on this. A worker's prose claim and the files git saw
        change are different kinds of thing, and conflating them is how a run
        reports success having produced nothing.
        """
        if not self.ok:
            return f"{self.task_id}: FAILED ({self.error_kind}) — {self.error_message[:200]}"
        changed = ", ".join(self.changed_files) or "NOTHING"
        return f"{self.task_id}: reported {self.status or 'nothing'}; git saw changes to {changed}"


def _changed_files(worktree: Path, base: str) -> list[str]:
    """Paths that differ from the wave's base, committed or not."""
    result = wt.git(worktree, "diff", "--name-only", base)
    committed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    status = wt.git(worktree, "status", "--porcelain")
    uncommitted = [
        line[3:].strip() for line in status.stdout.splitlines() if len(line) > 3
    ]
    return sorted(set(committed) | set(uncommitted))


def _parse_report(text: str, structured: dict | None) -> tuple[str, str, list[str], str]:
    """(status, summary, files, learnings) from a subagent's reply.

    Falls back to the raw text as the summary when the reply is not JSON. A
    coding agent that did real work and then described it in prose has still done
    the work — the filesystem check is what establishes that, not the format.
    """
    parsed = parsing.extract_json(text, structured)
    if not parsed.ok or not parsed.value:
        return "", (text or "").strip()[:2000], [], ""

    payload = parsed.value
    status = parsing.one_of(payload, "status", ("done", "blocked")) or ""
    summary = parsing.require_str(payload, "summary") or ""
    if status == "blocked":
        reason = parsing.require_str(payload, "blocked_reason") or ""
        summary = f"BLOCKED: {reason}\n\n{summary}".strip()
    return (
        status,
        summary or (text or "").strip()[:2000],
        parsing.string_list(payload, "files_changed"),
        parsing.require_str(payload, "learnings", allow_empty=True) or "",
    )


def run_task(
    task: dict[str, Any],
    *,
    target_repo: str,
    run_id: str,
    base: str,
    context: str,
    learnings: str,
    rework_comments: str,
    resume_session: str,
) -> TaskOutcome:
    """One coding subagent, in its own worktree. Returns an outcome; never raises."""
    task_id = task["task_id"]
    outcome = TaskOutcome(task_id=task_id, ok=False)

    if config.USE_GIT_WORKTREES:
        worktree, git_result = wt.create(
            target_repo, run_id=run_id, task_id=task_id, base=base
        )
        if worktree is None:
            outcome.error_kind = "worktree_failed"
            outcome.error_message = git_result.stderr or "could not create a worktree"
            return outcome
        workdir = worktree.path
        outcome.branch = worktree.branch
        outcome.worktree = str(workdir)
    else:
        # Shared-workspace mode: the wave runs against the target checkout itself
        # and the merger has nothing to merge.
        workdir = Path(target_repo)
        outcome.worktree = str(workdir)
        outcome.branch = base

    result = run_agent(
        coding_prompt(
            task=task,
            worktree=str(workdir),
            context=context,
            learnings=learnings,
            rework_comments=rework_comments,
        ),
        spec=config.CODING_AGENT,
        system_prompt=CODING_FRAME,
        cwd=str(workdir),
        tag=f"task-{task_id}",
        tools=CODING_TOOLS,
        resume_session=resume_session,
    )

    outcome.session_id = result.session_id
    outcome.cost_usd = result.cost_usd

    # Commit whatever is in the worktree either way. A failed turn may still have
    # written half a file, and that half is the thing a rework starts from.
    commit = wt.commit_all(workdir, f"{task_id}: {task.get('title') or 'agent work'}")
    if not commit.ok:
        logger.warning("[task-%s] could not commit worktree: %s", task_id, commit.stderr)
    outcome.commit = wt.head_sha(workdir)
    outcome.changed_files = _changed_files(workdir, base)

    if not result.ok:
        outcome.error_kind = result.error_kind
        outcome.error_message = result.error_message
        outcome.report = (
            f"Agent failed with {result.error_kind}. {result.error_message[:300]} "
            "Anything it saved before failing is committed on its branch."
        )
        return outcome

    status, summary, claimed, learning = _parse_report(result.text, result.structured)
    outcome.status = status
    outcome.report = summary
    outcome.claimed_files = claimed
    outcome.learnings = learning
    outcome.ok = status != "blocked"

    if outcome.ok and not outcome.changed_files:
        # The Bugs.md #21 signature: a fluent, specific, plausible report for work
        # that left no trace. Demoted to a failure here rather than passed on as a
        # success, because the reviewer would otherwise be reviewing a claim.
        outcome.ok = False
        outcome.error_kind = "no_changes"
        outcome.error_message = (
            "The agent reported completing the task but changed no files. "
            "Its report describes work that did not happen."
        )
        logger.warning("[task-%s] reported done and changed nothing", task_id)

    return outcome


def run_wave(
    tasks: list[dict[str, Any]],
    *,
    target_repo: str,
    run_id: str,
    base: str,
    context: str,
    learnings: str,
    rework_comments: str = "",
    sessions: dict[str, str] | None = None,
) -> list[TaskOutcome]:
    """Dispatch every task in a wave, up to MAX_PARALLEL_TASKS at a time.

    `sessions` maps task_id to the vendor session that attempted it last time, so
    a rework reaches the same agent rather than briefing a fresh one — which is
    what makes "here is what you got wrong" refer to anything.
    """
    sessions = sessions or {}
    if not tasks:
        return []

    workers = max(1, min(config.MAX_PARALLEL_TASKS, len(tasks)))
    logger.info(
        "wave dispatch | %d task(s) | %d at a time | base=%s",
        len(tasks),
        workers,
        base,
    )

    def _call(task: dict[str, Any]) -> TaskOutcome:
        return run_task(
            task,
            target_repo=target_repo,
            run_id=run_id,
            base=base,
            context=context,
            learnings=learnings,
            rework_comments=rework_comments,
            resume_session=sessions.get(task["task_id"], ""),
        )

    outcomes: list[TaskOutcome] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Every task is submitted before any result is collected. That is what
        # makes them overlap; submitting and immediately waiting would serialize
        # them for no reason.
        #
        # copy_context() carries the run's correlation id into each thread. A
        # ContextVar does not cross a thread boundary on its own, and without it
        # the logs from concurrent agents lose the id exactly when interleaving
        # makes it necessary (ADR-013).
        futures = [
            pool.submit(contextvars.copy_context().run, _call, task) for task in tasks
        ]
        for future in futures:
            outcomes.append(future.result())

    return outcomes
