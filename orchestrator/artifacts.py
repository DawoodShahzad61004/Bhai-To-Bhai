"""The files the pipeline passes along its edges.

Recorded permanently in docs/Architecture.md, the artifacts carried forward are:

    requirements -> context.md, user_choices.md -> planner
    planner      -> plan.json, TASK-*.json      -> wave orchestrator
    orchestrator -> git worktrees per subagent  -> merger
    merger       -> merged branch               -> reviewer
    reviewer     -> review notes                -> supervisor

plus `learnings.md`, which four of the six agents append to and which is
invisible in the drawing because it is a write-side channel touching most boxes.

Two properties are deliberate. Everything is written the moment it is produced,
never buffered to the end of a run — a killed process still leaves a recoverable
state, which is ADR-005's whole argument and what made docs/Bugs.md #15 findable.
And every file operation names `encoding="utf-8"` explicitly, because bare
`open()` on Windows defaults to cp1252 and dies on the em dash an agent will
eventually write (Bugs.md #11).
"""

from __future__ import annotations

import json
import re
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

    @property
    def context(self) -> Path:
        return self.run_dir / CONTEXT_FILE

    @property
    def user_choices(self) -> Path:
        return self.run_dir / USER_CHOICES_FILE

    @property
    def plan(self) -> Path:
        return self.run_dir / PLAN_FILE

    @property
    def learnings(self) -> Path:
        return self.run_dir / LEARNINGS_FILE

    @property
    def events(self) -> Path:
        return self.run_dir / EVENTS_FILE

    @property
    def reviews_dir(self) -> Path:
        return self.run_dir / "reviews"

    def task_file(self, task_id: str) -> Path:
        return self.run_dir / f"TASK-{safe_id(task_id)}.json"

    def task_files(self) -> list[Path]:
        return sorted(self.run_dir.glob(TASK_GLOB))

    def review_file(self, wave: int, attempt: int) -> Path:
        return self.reviews_dir / f"wave-{wave:02d}-attempt-{attempt:02d}.md"

    def supervisor_file(self, attempt: int) -> Path:
        return self.reviews_dir / f"supervisor-{attempt:02d}.md"


def prepare(run_dir: Path | str) -> RunArtifacts:
    """Create this run's artifact directory and return its paths."""
    resolved = Path(run_dir).resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    (resolved / "reviews").mkdir(exist_ok=True)
    return RunArtifacts(run_dir=resolved)


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


def append_learning(artifacts: RunArtifacts, agent: str, finding: str) -> None:
    """Add one finding to the shared learnings file.

    Append-only and timestamped. Four of the six agents write here, so an
    overwrite would lose whichever of them ran first.
    """
    finding = finding.strip()
    if not finding:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    entry = f"\n## {stamp} — {agent}\n\n{finding}\n"
    path = artifacts.learnings
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        if path.stat().st_size == 0:
            handle.write("# Learnings\n\nFindings reported by agents during this run.\n")
        handle.write(entry)
    logger.debug("learnings += %s (%d chars)", agent, len(finding))


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
