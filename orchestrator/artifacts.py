"""The files the pipeline passes along its edges.

Recorded permanently in docs/Architecture.md, the artifacts carried forward are:

    requirements -> context.md, user_choices.md -> planner
    planner      -> plan.json, TASK-*.json      -> wave orchestrator
    orchestrator -> git worktrees per subagent  -> merger
    merger       -> merged branch               -> reviewer
    reviewer     -> review notes                -> supervisor

plus `learnings.md`, which four of the six agents append to and which is
invisible in the drawing because it is a write-side channel touching most boxes.

The context, user choices, and learnings are project-scoped under the target
repository's `runs/` directory so a later run can reuse them. Run-specific
artifacts live there too, grouped by kind and then run id: plans, tasks, events,
and reviews remain auditable without turning `runs/<run-id>/` into the layout.

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
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logging_config import get_logger

logger = get_logger(__name__)

CONTEXT_FILE = "context.md"
USER_CHOICES_FILE = "user_choices.md"
PLAN_FILE = "plan.json"
LEARNINGS_FILE = "learnings.md"
EVENTS_FILE = "events.jsonl"
TASK_GLOB = "TASK-*.json"

_LOCAL_EXCLUDE_BEGIN = "# >>> bhai-to-bhai artifacts >>>"
_LOCAL_EXCLUDE_END = "# <<< bhai-to-bhai artifacts <<<"
_LOCAL_EXCLUDE_PATTERNS = (
    "/runs/context.md",
    "/runs/learnings.md",
    "/runs/learnings.md.lock",
    "/runs/user_choices.md",
    "/runs/plans/",
    "/runs/tasks/",
    "/runs/reviews/",
    "/runs/events/",
)

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
    """

    run_dir: Path
    shared_dir: Path

    @property
    def run_id(self) -> str:
        return safe_id(self.run_dir.name, fallback="run")

    @property
    def context(self) -> Path:
        return self.shared_dir / CONTEXT_FILE

    @property
    def user_choices(self) -> Path:
        return self.shared_dir / USER_CHOICES_FILE

    @property
    def plan(self) -> Path:
        return self.shared_dir / "plans" / f"{self.run_id}.json"

    @property
    def learnings(self) -> Path:
        return self.shared_dir / LEARNINGS_FILE

    @property
    def learnings_lock(self) -> Path:
        return self.learnings.with_name(self.learnings.name + ".lock")

    @property
    def events(self) -> Path:
        return self.shared_dir / "events" / f"{self.run_id}.jsonl"

    @property
    def tasks_dir(self) -> Path:
        return self.shared_dir / "tasks" / self.run_id

    @property
    def reviews_dir(self) -> Path:
        return self.shared_dir / "reviews" / self.run_id

    def task_file(self, task_id: str) -> Path:
        return self.tasks_dir / f"TASK-{safe_id(task_id)}.json"

    def task_files(self) -> list[Path]:
        return sorted(self.tasks_dir.glob(TASK_GLOB))

    def review_file(self, wave: int, attempt: int) -> Path:
        return self.reviews_dir / f"wave-{wave:02d}-attempt-{attempt:02d}.md"

    def supervisor_file(self, attempt: int) -> Path:
        return self.reviews_dir / f"supervisor-{attempt:02d}.md"


def _git_exclude_path(target_repo: Path) -> Path | None:
    """Resolve this checkout's local exclude file without touching .gitignore."""
    try:
        completed = subprocess.run(
            [shutil.which("git") or "git", "rev-parse", "--git-path", "info/exclude"],
            cwd=str(target_repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    path = Path(completed.stdout.strip())
    return path.resolve() if path.is_absolute() else (target_repo / path).resolve()


def _ensure_local_artifacts_excluded(target_repo: Path) -> None:
    """Keep generated artifacts out of target commits using Git-local config.

    Exact paths are excluded instead of the whole `runs/` directory, so a
    target project that already owns unrelated files there is not hidden.
    """
    exclude_path = _git_exclude_path(target_repo)
    if exclude_path is None:
        return
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    current = (
        exclude_path.read_text(encoding="utf-8", errors="replace")
        if exclude_path.exists()
        else ""
    )
    block = "\n".join(
        (_LOCAL_EXCLUDE_BEGIN, *_LOCAL_EXCLUDE_PATTERNS, _LOCAL_EXCLUDE_END)
    )
    pattern = re.compile(
        re.escape(_LOCAL_EXCLUDE_BEGIN) + r".*?" + re.escape(_LOCAL_EXCLUDE_END),
        re.DOTALL,
    )
    if pattern.search(current):
        updated = pattern.sub(block, current)
    else:
        prefix = current.rstrip("\r\n")
        updated = f"{prefix}\n\n{block}\n" if prefix else f"{block}\n"
    if updated != current:
        exclude_path.write_text(updated, encoding="utf-8")


def _copy_legacy_file(source: Path, destination: Path) -> None:
    if source.is_file() and not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        logger.info("migrated legacy artifact %s -> %s", source, destination)


def _migrate_legacy_layout(artifacts: RunArtifacts) -> None:
    """Copy this run's old flat artifacts into the category-based layout."""
    legacy = artifacts.run_dir
    _copy_legacy_file(legacy / CONTEXT_FILE, artifacts.context)
    _copy_legacy_file(legacy / USER_CHOICES_FILE, artifacts.user_choices)
    _copy_legacy_file(legacy / LEARNINGS_FILE, artifacts.learnings)
    _copy_legacy_file(legacy / PLAN_FILE, artifacts.plan)
    _copy_legacy_file(legacy / EVENTS_FILE, artifacts.events)
    for source in legacy.glob(TASK_GLOB):
        _copy_legacy_file(source, artifacts.tasks_dir / source.name)
    legacy_reviews = legacy / "reviews"
    if legacy_reviews.is_dir():
        for source in legacy_reviews.glob("*.md"):
            _copy_legacy_file(source, artifacts.reviews_dir / source.name)


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


def prepare(
    run_dir: Path | str,
    target_repo: Path | str | None = None,
) -> RunArtifacts:
    """Create the run-local and target-repository artifact directories.

    `target_repo` is optional for callers that only need an isolated artifact
    layout (notably tests and the append-learning CLI). Production pipeline
    callers always provide it, making every artifact live under the target's
    `runs/` directory while `run_dir` remains the controller's run identity and
    legacy migration source.
    """
    resolved = Path(run_dir).resolve()
    shared = (
        (Path(target_repo).resolve() / "runs")
        if target_repo is not None
        else resolved
    )
    resolved.mkdir(parents=True, exist_ok=True)
    shared.mkdir(parents=True, exist_ok=True)
    artifacts = RunArtifacts(run_dir=resolved, shared_dir=shared)
    artifacts.tasks_dir.mkdir(parents=True, exist_ok=True)
    artifacts.reviews_dir.mkdir(parents=True, exist_ok=True)
    (shared / "plans").mkdir(exist_ok=True)
    (shared / "events").mkdir(exist_ok=True)
    if target_repo is not None:
        _ensure_local_artifacts_excluded(Path(target_repo).resolve())
        _migrate_legacy_layout(artifacts)
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
        "artifacts_dir", help="Directory containing the shared learnings.md (absolute path)."
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

    run_artifacts = prepare(args.artifacts_dir)
    append_learning(run_artifacts, args.agent, message)
    print(f"appended to {run_artifacts.learnings}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
