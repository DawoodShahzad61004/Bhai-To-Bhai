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
# Bumped alongside every learnings.md write (its value is the file's new byte
# size) so a reader can ask "has anything been added since I last looked?"
# without opening and diffing the file itself — see run-shared below and
# docs/Bugs.md #54.
LEARNINGS_STAMP_FILE = "learnings.stamp"
# Per-task "where did I last leave off in learnings.md" bookmarks, one file
# each, kept out of the three agent-facing shared files rather than mixed in.
LEARNINGS_CURSORS_DIRNAME = ".learnings_cursors"
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
    def learnings_stamp(self) -> Path:
        return self.shared_dir / LEARNINGS_STAMP_FILE

    @property
    def learnings_cursors_dir(self) -> Path:
        return self.shared_dir / LEARNINGS_CURSORS_DIRNAME

    def learnings_cursor_file(self, task_id: str) -> Path:
        return self.learnings_cursors_dir / f"{safe_id(task_id)}.offset"

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
        artifacts.learnings_stamp,
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

    Also rewrites `learnings_stamp` to the file's new byte size, inside the
    same lock — so the stamp and the content it describes never disagree, and
    `peer_entries_since()` below can tell "nothing changed" from a stamp
    comparison alone, without opening `learnings.md` at all.
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
        artifacts.learnings_stamp.parent.mkdir(parents=True, exist_ok=True)
        artifacts.learnings_stamp.write_text(str(path.stat().st_size), encoding="utf-8")
    logger.debug("learnings += %s (%d chars)", agent, len(finding))


def read_learnings_stamp(artifacts: RunArtifacts) -> int:
    """`learnings.md`'s byte size as of the most recent `append_learning()` call.

    0 for a run that has never had a finding appended — the same state an
    empty, freshly-`touch()`-ed learnings.md is in.
    """
    text = read_text(artifacts.learnings_stamp, "0").strip()
    try:
        return int(text)
    except ValueError:
        return 0


def read_learnings_cursor(artifacts: RunArtifacts, task_id: str) -> int | None:
    """Where `task_id` last left off reading `learnings.md`, or None if never recorded.

    `None`, not 0, marks "never recorded" — dispatch seeds a task's cursor to 0
    whenever a run's `learnings.md` genuinely starts empty (see
    `wave_orchestrator/dispatch.py`), and 0 is that case's correct, meaningful
    value. Collapsing "unset" into 0 would make a caller's `cursor or
    read_learnings_stamp(...)` fallback override a real, freshly-seeded 0 with
    "start from wherever the file is right now" — silently hiding exactly the
    peer finding docs/Bugs.md #54 needs surfaced.
    """
    text = read_text(artifacts.learnings_cursor_file(task_id), "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def write_learnings_cursor(artifacts: RunArtifacts, task_id: str, offset: int) -> None:
    path = artifacts.learnings_cursor_file(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(offset), encoding="utf-8")


_ENTRY_HEADER = re.compile(r"^## \S+ \S+ — (.+)$", re.MULTILINE)


def _split_learning_entries(text: str) -> list[tuple[str, str]]:
    """(agent, entry text) for every `## <stamp> — <agent>` block in `text`."""
    headers = list(_ENTRY_HEADER.finditer(text))
    entries = []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        entries.append((header.group(1).strip(), text[header.start():end].rstrip("\n")))
    return entries


def peer_entries_since(artifacts: RunArtifacts, task_id: str, cursor: int) -> tuple[str, int]:
    """Findings appended by agents other than `task_id` after byte offset `cursor`.

    Returns `(formatted text or "", learnings.md's current byte size)` — the
    second value is always returned, even when the first is empty, so a caller
    can advance its cursor unconditionally rather than only on a hit.

    Reads bytes rather than `read_text()` so the offset lines up with
    `learnings_stamp` (a byte size). A cursor that lands mid-character in a
    multibyte UTF-8 sequence — real here, since these entries carry the same
    em dash `append_learning` writes into every header — decodes to a
    replacement character at worst; this channel is a best-effort nudge, not
    the record of truth, which is `learnings.md` itself, always fully
    readable by any agent regardless of what this returns.
    """
    try:
        data = artifacts.learnings.read_bytes()
    except FileNotFoundError:
        return "", 0
    end = len(data)
    cursor = min(max(cursor, 0), end)
    tail = data[cursor:].decode("utf-8", errors="replace")
    peers = [text for agent, text in _split_learning_entries(tail) if agent != task_id]
    return "\n\n".join(peers), end


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


def _first_nonblank_line(text: str, limit: int = 300) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _already_auto_recorded(
    artifacts: RunArtifacts, task_id: str, command_str: str, symptom: str
) -> bool:
    """True if `task_id` already auto-recorded this exact command/symptom pair.

    Keyed on the symptom too, not just the command: a repeated command can
    fail for a *different* reason on a later attempt (an environment issue,
    then later a real test assertion once the environment is fixed), and that
    second failure is new information no one has seen yet — only a byte-for-
    byte repeat of the same symptom is the noise this exists to cut (observed
    in production: the same task rerunning the same failing test five times
    in a row, each auto-appending a near-identical entry).

    Scoped to this task's own entries — a peer recording the identical
    finding is a different, useful signal (already surfaced via
    `peer_entries_since`), not a duplicate to suppress here. Reads directly,
    no lock: only this task's own single, sequential process ever writes
    entries under its own task_id, so nothing else can race this read.
    """
    prefix = f"[auto] `{command_str}` failed"
    suffix = f": {symptom}"
    text = read_text(artifacts.learnings)
    return any(
        agent == task_id and prefix in entry and entry.rstrip().endswith(suffix)
        for agent, entry in _split_learning_entries(text)
    )


def run_shared_command(shared_dir: str, task_id: str, command: list[str]) -> int:
    """Run `command`, then surface any peer `learnings.md` entries recorded meanwhile.

    docs/Bugs.md #54: a coding agent is told it can re-read `learnings.md` "at
    any time," but that only fires if the agent thinks to look — an agent
    hitting the exact failure a same-wave peer is *also* hitting right now has
    no reason to expect an answer is already there. This wraps the failing
    command itself instead of relying on that judgment call: on a non-zero
    exit it (1) records this task's own symptom immediately, before anyone
    diagnoses anything — skipped if this exact command already produced this
    exact symptom for this task, so retrying the same failing command does
    not spam learnings.md with repeats of a finding already there — and (2)
    prints whatever peers have recorded since this task last checked, right
    below the command's real output — the same tool result the agent is
    already about to read, not a second lookup it has to remember to make.

    The command's stdout/stderr and exit code are preserved exactly, so a
    caller's own success/failure handling behaves identically to running the
    bare command — the only difference on a failure is extra text after it,
    and on Windows the command is fed through `cmd.exe` (`shell=True` with a
    correctly quoted single string) so a `.cmd`-shimmed tool like `npm` still
    resolves via PATH the way it would when this same command is typed
    directly into PowerShell.
    """
    shared_path = Path(shared_dir).resolve()
    artifacts = RunArtifacts(run_id="_cli", root=shared_path.parent)

    cursor = read_learnings_cursor(artifacts, task_id)
    if cursor is None:
        cursor = read_learnings_stamp(artifacts)

    import subprocess

    if os.name == "nt":
        result = subprocess.run(
            subprocess.list2cmdline(command),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    else:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )

    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)

    if result.returncode != 0:
        command_str = " ".join(command)
        symptom = _first_nonblank_line(result.stderr) or _first_nonblank_line(result.stdout)
        if not _already_auto_recorded(artifacts, task_id, command_str, symptom):
            finding = f"[auto] `{command_str}` failed (exit {result.returncode}): {symptom}"
            append_learning(artifacts, task_id, finding)

    stamp = read_learnings_stamp(artifacts)
    if stamp != cursor:
        peers, new_cursor = peer_entries_since(artifacts, task_id, cursor)
        if peers:
            print("\n--- PEER FINDINGS RECORDED WHILE THIS RAN ---", file=sys.stderr)
            print(peers, file=sys.stderr)
        write_learnings_cursor(artifacts, task_id, new_cursor)
    else:
        write_learnings_cursor(artifacts, task_id, stamp)

    return result.returncode


def _cli_run_shared(argv: list[str]) -> int:
    usage = "usage: artifacts.py run-shared <artifacts_dir> <task_id> -- <command> [args...]"
    if "--" not in argv:
        print(usage, file=sys.stderr)
        return 1
    split = argv.index("--")
    head, command = argv[:split], argv[split + 1 :]
    if len(head) != 2 or not command:
        print(usage, file=sys.stderr)
        return 1
    shared_dir, task_id = head
    return run_shared_command(shared_dir, task_id, command)


def _cli(argv: list[str] | None = None) -> int:
    """`append-learning <artifacts_dir> <agent> [message]` or `run-shared <artifacts_dir> <task_id> -- <command...>`.

    The coding subagents' own entry point for writing directly to the shared
    learnings.md from inside their worktree — invoked over Bash, not imported,
    since they run as a separate OS process from this one. Reuses
    append_learning rather than reimplementing it, so there is exactly one
    place that knows the file's format and its locking.

    `artifacts_dir` is the `shared/` directory itself — the same path a coding
    agent already received via `--add-dir` (`RunArtifacts.shared_dir`) — not a
    directory this command prepares. Nothing under `records/` is reachable
    from here, by construction: these commands only ever touch `learnings.md`
    and its stamp/cursor sidecars.

    `run-shared` is dispatched before argparse ever sees the arguments: its
    trailing `<command...>` is a free-form argv tail (someone else's flags,
    not this program's), and argparse's subparser handling has no clean way
    to say "stop parsing after `--` and hand me everything raw."
    `append-learning` has no such tail, so it keeps its original argparse path
    unchanged below.

    Reconfigures stdout/stderr to UTF-8 first. This CLI is invoked as its own
    fresh OS process (over Bash, not imported), so it starts with whatever
    console codepage Windows handed it — cp1252, typically — rather than the
    UTF-8 every file in this module already insists on. `run-shared`'s peer
    findings are `learnings.md` entries verbatim, headers and all, and every
    header carries the same em dash Bugs.md #11 already names as the thing
    bare cp1252 output dies on.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "run-shared":
        return _cli_run_shared(argv[1:])

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
