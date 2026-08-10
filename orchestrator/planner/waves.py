"""Turning a task list with dependencies into execution waves.

This is deliberately **not** the planner agent's job. The agent decides what the
tasks are and which depend on which — genuine judgment. Grouping them into waves
is arithmetic, and it is a correctness property: a task dispatched before its
dependency merges is working against a tree that does not contain the thing it
was told to build on.

ADR-011's argument, applied outside termination: a safety property cannot rest on
the cooperation of the component it constrains. A model asked to "put independent
tasks in the same wave" will usually comply, and the failure when it does not is
silent — the wave runs, the agent finds the file missing, and invents something.
So the model proposes edges and this module derives the schedule.

Pure functions, no I/O, no config. Cheap to test exhaustively, which is the point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Schedule:
    """A validated task list and its wave assignment, or the reason there isn't one."""

    ok: bool
    tasks: list[dict[str, Any]] | None = None
    waves: list[list[str]] | None = None
    error: str = ""


def normalise_tasks(raw: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Coerce the agent's task list into records, reporting what it got wrong.

    Returns (tasks, problems). A task missing an id or a description is dropped
    and named — a task the pipeline cannot dispatch is worse than one that is
    absent, because it would produce a wave that silently does nothing.
    """
    tasks: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: set[str] = set()

    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(f"task {index} is a {type(item).__name__}, not an object")
            continue

        task_id = item.get("task_id") or item.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            problems.append(f"task {index} has no usable task_id")
            continue
        task_id = task_id.strip()

        if task_id in seen:
            problems.append(f"task_id {task_id!r} appears more than once")
            continue
        seen.add(task_id)

        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            problems.append(f"task {task_id!r} has no description")
            continue

        depends = item.get("depends_on")
        if isinstance(depends, str):
            depends = [depends]
        if not isinstance(depends, list):
            depends = []

        files = item.get("files")
        if isinstance(files, str):
            files = [files]
        if not isinstance(files, list):
            files = []

        tasks.append(
            {
                "task_id": task_id,
                "title": str(item.get("title") or task_id).strip(),
                "description": description.strip(),
                "files": [str(f).strip() for f in files if str(f).strip()],
                "acceptance": str(item.get("acceptance") or "").strip(),
                "depends_on": [
                    str(d).strip() for d in depends if isinstance(d, str) and d.strip()
                ],
            }
        )

    return tasks, problems


def normalise_coding_agents(
    raw: Any,
    *,
    max_count: int,
    allowed: list[tuple[str, str]],
) -> tuple[list[dict[str, str]], list[str]]:
    """Coerce the agent's requested coding-agent roster into `{backend, model}` dicts.

    Returns (agents, problems). Each requested slot must name a (model, backend)
    pair from `allowed` — normally config.SMALL_MEDIUM_MODELS + EXPERT_MODELS —
    the same menu the plan prompt offered. A slot naming anything else is
    dropped and named, the discipline normalise_tasks applies to a task the
    pipeline cannot dispatch. An empty or entirely-invalid result is reported as
    a problem but is not itself a failure: the caller decides the fallback.
    """
    allowed_pairs = {(backend, model) for model, backend in allowed}
    agents: list[dict[str, str]] = []
    problems: list[str] = []

    items = raw if isinstance(raw, list) else []
    if not isinstance(raw, list) and raw is not None:
        problems.append(f"'coding_agents' is a {type(raw).__name__}, not a list")

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"coding agent {index} is a {type(item).__name__}, not an object")
            continue
        backend = item.get("backend")
        model = item.get("model", "")
        if not isinstance(backend, str) or not isinstance(model, str):
            problems.append(f"coding agent {index} has a non-string backend or model")
            continue
        if (backend, model) not in allowed_pairs:
            problems.append(
                f"coding agent {index} names backend={backend!r} model={model!r}, "
                "which is not on the menu"
            )
            continue
        agents.append({"backend": backend, "model": model})

    if len(agents) > max_count:
        problems.append(f"{len(agents)} coding agent(s) requested; capped at {max_count}")
        agents = agents[:max_count]

    return agents, problems


def assign_waves(tasks: list[dict[str, Any]]) -> Schedule:
    """Group tasks into dependency-ordered waves.

    Wave 0 is everything with no dependencies; wave N is everything whose
    dependencies all landed in waves below N. Kahn's algorithm by levels.

    Two failures are reported rather than worked around, because both mean the
    plan does not describe something that can be built:

      * A dependency naming a task that does not exist. Dropping it would let the
        task run early against a tree missing what it was told to build on, and
        the agent would then invent the missing piece — a wrong result that looks
        like a right one.
      * A cycle. There is no order that satisfies it.
    """
    if not tasks:
        return Schedule(ok=False, error="the plan contains no tasks")

    by_id = {task["task_id"]: task for task in tasks}

    dangling = []
    for task in tasks:
        for dependency in task["depends_on"]:
            if dependency not in by_id:
                dangling.append(f"{task['task_id']!r} depends on unknown task {dependency!r}")
    if dangling:
        return Schedule(ok=False, error="; ".join(dangling))

    # Self-dependency is a cycle of length one, and reads more clearly named.
    for task in tasks:
        if task["task_id"] in task["depends_on"]:
            return Schedule(ok=False, error=f"task {task['task_id']!r} depends on itself")

    remaining = {task["task_id"]: set(task["depends_on"]) for task in tasks}
    placed: set[str] = set()
    waves: list[list[str]] = []

    while remaining:
        ready = sorted(
            task_id for task_id, deps in remaining.items() if deps <= placed
        )
        if not ready:
            stuck = ", ".join(sorted(remaining))
            return Schedule(
                ok=False,
                error=f"dependency cycle among tasks: {stuck}",
            )
        waves.append(ready)
        placed.update(ready)
        for task_id in ready:
            del remaining[task_id]

    # Stamp each task with the wave it landed in, so a TASK-*.json on disk says
    # when it runs without the reader having to recompute this.
    for index, wave in enumerate(waves):
        for task_id in wave:
            by_id[task_id]["wave"] = index

    ordered = [by_id[task_id] for wave in waves for task_id in wave]
    return Schedule(ok=True, tasks=ordered, waves=waves)
