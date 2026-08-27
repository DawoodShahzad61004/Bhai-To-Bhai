"""The files the pipeline passes along its edges.

Recorded permanently in docs/Architecture.md, the artifacts carried forward are:

    requirements -> context.md, user_choices.md -> planner
    planner      -> plan.json, TASK-*.json      -> wave orchestrator
    orchestrator -> git worktrees per subagent  -> merger
    merger       -> merged branch               -> reviewer
    reviewer     -> review notes                -> supervisor

plus `learnings.md`, which four of the six agents append to and which is
invisible in the drawing because it is a write-side channel touching most boxes.

The store lives outside the target repository's working tree, at
`worktrees.artifact_root(target_repo)` — a sibling directory, same idiom as the
task worktrees themselves (ADR-037; this superseded ADR-029's in-repo `runs/`
location, which collided with a target project's own files of that name and
did not survive an ordinary `git clean`). Inside that root, `shared/` holds
`context.md`, `learnings.md`, and `user_choices.md` — the only directory ever
granted to an agent via `extra_dirs` — and `records/` holds everything grouped
by kind and then run id: plans, tasks, events, and reviews, auditable per run
but never itself handed to an agent.

Two properties are deliberate. Everything is written the moment it is produced,
never buffered to the end of a run — a killed process still leaves a recoverable
state, which is ADR-005's whole argument and what made docs/Bugs.md #15 findable.
And every file operation names `encoding="utf-8"` explicitly, because bare
`open()` on Windows defaults to cp1252 and dies on the em dash an agent will
eventually write (Bugs.md #11).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import worktrees as wt
from logging_config import get_logger

logger = get_logger(__name__)

CONTEXT_FILE = "context.md"
USER_CHOICES_FILE = "user_choices.md"
LEARNINGS_FILE = "learnings.md"
TASK_GLOB = "TASK-*.json"

# A task id has to survive being used as a filename, a git branch component and a
# log tag, so it is constrained rather than trusted. The planner is a language
# model and its output is input here.
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


def safe_id(value: str, *, fallback: str = "task") -> str:
    """A task id reduced to something usable as a path and a branch name."""
    cleaned = _SAFE_ID.sub("-", (value or "").strip()).strip("-.")
    return cleaned or fallback


@dataclass(frozen=True)
class RunArtifacts:
    """Every path this run reads and writes, resolved once.

    Absolute throughout. Agents receive these paths verbatim, and a concrete
    absolute path is what beats the abstraction an agent's own system prompt
    supplies — Bugs.md #22 is a chart written into the harness's scratchpad
    because the brief said "the workspace directory" and the harness said
    C:\\Users\\...\\scratchpad.

    `root` is one target's whole artifact store (`worktrees.artifact_root()`),
    shared by every run against that target. `shared_dir` — the small,
    frequently-granted files — and `records_dir` — the growing per-run audit
    trail, never granted to an agent — are the two halves of it.
    """

    run_id: str
    root: Path

    @property
    def shared_dir(self) -> Path:
        return self.root / "shared"

    @property
    def records_dir(self) -> Path:
        return self.root / "records"

    @property
    def context(self) -> Path:
        return self.shared_dir / CONTEXT_FILE

    @property
    def user_choices(self) -> Path:
        return self.shared_dir / USER_CHOICES_FILE

    @property
    def plan(self) -> Path:
        return self.records_dir / "plans" / f"{self.run_id}.json"

    @property
    def learnings(self) -> Path:
        return self.shared_dir / LEARNINGS_FILE

    @property
    def learnings_lock(self) -> Path:
        return self.learnings.with_name(self.learnings.name + ".lock")

    @property
    def events(self) -> Path:
        return self.records_dir / "events" / f"{self.run_id}.jsonl"

    @property
    def tasks_dir(self) -> Path:
        return self.records_dir / "tasks" / self.run_id

    @property
    def reviews_dir(self) -> Path:
        return self.records_dir / "reviews" / self.run_id

    def task_file(self, task_id: str) -> Path:
        return self.tasks_dir / f"TASK-{safe_id(task_id)}.json"

    def task_files(self) -> list[Path]:
        return sorted(self.tasks_dir.glob(TASK_GLOB))

    def review_file(self, wave: int, attempt: int) -> Path:
        return self.reviews_dir / f"wave-{wave:02d}-attempt-{attempt:02d}.md"

    def supervisor_file(self, attempt: int) -> Path:
        return self.reviews_dir / f"supervisor-{attempt:02d}.md"


def _copy_legacy_file(source: Path, destination: Path) -> None:
    if source.is_file() and not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        logger.info("migrated legacy artifact %s -> %s", source, destination)


def _migrate_legacy_layout(target_repo: Path, artifacts: RunArtifacts) -> None:
    """Copy the shared project memory out of the pre-ADR-037 in-repo `runs/`.

    Guarded on the shared files' own names existing there, not on `runs/`
    merely existing — a target project can already own a `runs/` directory of
    unrelated content (a real collision observed in practice: a sibling
    project's own dry-run logs), and this must never touch it. Only the three
    named shared files migrate; the source is never deleted, so a wrong guard
    loses nothing, and old per-run plans/tasks/reviews/events are left where
    they are rather than reshuffled into the new run's record tree.
    """
    legacy = target_repo / "runs"
    if not ((legacy / LEARNINGS_FILE).is_file() or (legacy / USER_CHOICES_FILE).is_file()):
        return
    _copy_legacy_file(legacy / CONTEXT_FILE, artifacts.context)
    _copy_legacy_file(legacy / USER_CHOICES_FILE, artifacts.user_choices)
    _copy_legacy_file(legacy / LEARNINGS_FILE, artifacts.learnings)


def _initialise_empty_artifacts(artifacts: RunArtifacts) -> None:
    """Create first-run artifacts whose empty form is valid and readable.

    Agents receive absolute paths and may read them unconditionally. Text
    memory, its lock, and JSONL event streams all have a meaningful empty
    state. Plans, tasks, and reviews do not: creating placeholders for those
    would falsely claim that their owning pipeline stage had produced output.
    """
    for path in (
        artifacts.context,
        artifacts.learnings,
        artifacts.learnings_lock,
        artifacts.user_choices,
        artifacts.events,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.touch(exist_ok=False)
        except FileExistsError:
            pass


def prepare(run_id: str, target_repo: Path | str) -> RunArtifacts:
    """Create this target's artifact store and one run's record directories.

    The store lives at `worktrees.artifact_root(target_repo)` — outside the
    target's working tree — so `shared/` (context, learnings, user choices) is
    reused across every run against this target, while `records_dir`'s
    plans/tasks/reviews/events are grouped by this specific `run_id`.
    """
    run_id = safe_id(run_id, fallback="run")
    target = Path(target_repo).resolve()
    root = wt.artifact_root(target)
    artifacts = RunArtifacts(run_id=run_id, root=root)

    artifacts.shared_dir.mkdir(parents=True, exist_ok=True)
    artifacts.tasks_dir.mkdir(parents=True, exist_ok=True)
    artifacts.reviews_dir.mkdir(parents=True, exist_ok=True)
    (artifacts.records_dir / "plans").mkdir(parents=True, exist_ok=True)
    (artifacts.records_dir / "events").mkdir(parents=True, exist_ok=True)

    _migrate_legacy_layout(target, artifacts)
    _initialise_empty_artifacts(artifacts)
    return artifacts


def write_text(path: Path, content: str) -> Path:
    """Write a text artifact, creating parents. Returns the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    logger.debug("wrote %s (%d chars)", path.name, len(content))
    return path


def read_text(path: Path, default: str = "") -> str:
    """Read a text artifact, or `default` when it does not exist.

    A missing artifact is a normal state — the reviewer runs before the
    supervisor's notes exist — so this reports rather than raises.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.debug("wrote %s", path.name)
    return path


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        logger.error("%s is not valid JSON: %s", path, exc)
        return default


@contextlib.contextmanager
def _exclusive_lock(path: Path):
    """An OS-level mutex so concurrent writers to `path` serialise.

    Writers only. A reader — a coding subagent's own Read tool, or `read_text`
    below — never takes this lock and is never made to wait on it; the
    requirement is "a writer excludes other writers", not "a writer excludes
    readers". A sidecar `<name>.lock` file rather than the artifact itself, so
    the lock's own open/close never competes with a plain read of it.

    Every appender to learnings.md goes through here, whether it is this
    process (the other five agents, via append_learning) or a coding
    subagent's own OS process invoking this module's CLI directly — the lock
    has to be OS-level, a Python-only mutex would not reach across processes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    handle = open(lock_path, "a+b")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + 30
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"could not lock {lock_path} within 30s") from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def append_learning(artifacts: RunArtifacts, agent: str, finding: str) -> None:
    """Add one finding to the shared learnings file.

    Append-only and timestamped. Every agent writes here, some from this
    process and some — the parallel coding subagents — from their own OS
    process (see the CLI at the bottom of this module), so an overwrite or an
    interleaved write would lose whichever writer lost the race. `_exclusive_lock`
    is what prevents that; the write itself is one `write()` call so a
    concurrent reader never observes a header with no entry after it.
    """
    finding = finding.strip()
    if not finding:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    entry = f"\n## {stamp} — {agent}\n\n{finding}\n"
    path = artifacts.learnings
    path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(path):
        header = (
            ""
            if path.exists() and path.stat().st_size
            else "# Learnings\n\nFindings reported by agents across project runs.\n"
        )
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(header + entry)
    logger.debug("learnings += %s (%d chars)", agent, len(finding))


def append_user_choices(artifacts: RunArtifacts, run_id: str, content: str) -> None:
    """Append one run's explicit user decisions to the project ledger.

    The run marker makes checkpoint replay idempotent. The approved
    `learnings.md.lock` coordinates this second append-only project file too,
    avoiding another lock artifact in the flat shared layout.
    """
    content = content.strip()
    if not content:
        return
    marker = f"<!-- run:{safe_id(run_id, fallback='run')} -->"
    path = artifacts.user_choices
    path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(artifacts.learnings):
        existing = read_text(path)
        if marker in existing:
            return
        header = (
            ""
            if existing.strip()
            else (
                "# User choices\n\n"
                "_Append-only explicit decisions made by the user across project runs. "
                "Nothing inferred, assumed, or derived from code belongs here._\n"
            )
        )
        entry = f"\n{marker}\n{content}\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(header + entry)
    logger.debug("user_choices += %s (%d chars)", run_id, len(content))


def append_event(artifacts: RunArtifacts, entry: dict[str, Any]) -> None:
    """Mirror one audit event to disk as it happens.

    The graph state carries the same events, but state is only durable at a
    checkpoint boundary. This is written on arrival, which is the difference
    between a killed run leaving evidence and leaving nothing.
    """
    path = artifacts.events
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.flush()


def write_tasks(artifacts: RunArtifacts, tasks: list[dict[str, Any]]) -> list[Path]:
    """Write one TASK-*.json per task. Returns the paths in task order."""
    paths = []
    for task in tasks:
        paths.append(write_json(artifacts.task_file(task["task_id"]), task))
    return paths


def load_tasks(artifacts: RunArtifacts) -> list[dict[str, Any]]:
    """Every TASK-*.json on disk, for a resumed run that has no state in hand."""
    tasks = []
    for path in artifacts.task_files():
        task = read_json(path)
        if isinstance(task, dict):
            tasks.append(task)
    return tasks


def _cli(argv: list[str] | None = None) -> int:
    """`python artifacts.py append-learning <artifacts_dir> <agent> [message]`.

    The coding subagents' own entry point for writing directly to the shared
    learnings.md from inside their worktree — invoked over Bash, not imported,
    since they run as a separate OS process from this one. Reuses
    append_learning rather than reimplementing it, so there is exactly one
    place that knows the file's format and its locking.

    `artifacts_dir` is the `shared/` directory itself — the same path a coding
    agent already received via `--add-dir` (`RunArtifacts.shared_dir`) — not a
    directory this command prepares. Nothing under `records/` is reachable
    from here, by construction: this command only ever needs `learnings.md`.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="artifacts.py",
        description="Append one finding to a run's shared learnings.md.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    append = subcommands.add_parser(
        "append-learning", help="Append a finding, excluding other concurrent writers."
    )
    append.add_argument(
        "artifacts_dir",
        help="The shared artifact directory containing learnings.md (absolute path).",
    )
    append.add_argument("agent", help='Who is reporting this, e.g. "task-T-001".')
    append.add_argument(
        "message", nargs="?", help="The finding to record. Omit to read it from stdin."
    )

    args = parser.parse_args(argv)
    message = args.message if args.message is not None else sys.stdin.read()
    if not message.strip():
        print("error: no message given (pass it as an argument or on stdin)", file=sys.stderr)
        return 1

    shared_dir = Path(args.artifacts_dir).resolve()
    run_artifacts = RunArtifacts(run_id="_cli", root=shared_dir.parent)
    append_learning(run_artifacts, args.agent, message)
    print(f"appended to {run_artifacts.learnings}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
