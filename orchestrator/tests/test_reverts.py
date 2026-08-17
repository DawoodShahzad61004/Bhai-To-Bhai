"""What "the work is reverted" has to mean, given the order of the boxes.

The drawing puts Merge before Review, and the Supervisor after everything. So by
the time either feedback edge fires, the work it is rejecting has already been
integrated. Both loops therefore have to move the integration branch backwards,
and neither is done by deleting a worktree.

These were found by two end-to-end tests failing on their second pass, with the
coding agent reporting work that produced no diff — because it was being asked to
create a file that its own rejected attempt had already merged.
"""

from __future__ import annotations

import json

import pytest

import artifacts as art
import worktrees as wt
from adapters.base import AgentResult
from planner import planner_node
from state import initial_state
from wave_orchestrator import wave_orchestrator_node


def _writer(filename: str, body: str):
    def reply(prompt: str) -> AgentResult:
        from pathlib import Path

        marker = "## Working directory\n\n"
        workdir = Path(prompt[prompt.index(marker) + len(marker) :].splitlines()[0].strip())
        (workdir / filename).write_text(body, encoding="utf-8")
        return AgentResult(
            ok=True,
            text=json.dumps({"status": "done", "summary": "wrote it", "files_changed": [filename]}),
            session_id="sess-1",
        )

    return reply


@pytest.fixture
def state(git_repo, tmp_path):
    run_dir = art.prepare(tmp_path / "run")
    art.write_text(run_dir.context, "# Context")
    s = initial_state(
        run_id="rv",
        goal="g",
        target_repo=str(git_repo),
        run_dir=str(run_dir.run_dir),
    )
    s["context"] = "# Context"
    s["tasks"] = [
        {"task_id": "T-001", "title": "t", "description": "d", "files": ["a.txt"], "wave": 0}
    ]
    s["waves"] = [["T-001"]]
    return s


# ── Rework: back to where the wave started ───────────────────────────────────


def test_the_first_attempt_records_where_the_wave_started(state, stub, git_repo):
    stub.set_reply("task-T-001", _writer("a.txt", "first\n"))
    before = wt.git(git_repo, "rev-parse", "HEAD").stdout

    result = wave_orchestrator_node(state)

    assert result["wave_base_sha"] == before
    assert result["run_base_sha"] == before


def test_a_rework_resets_the_integration_branch_to_the_wave_base(state, stub, git_repo):
    """Deleting the worktree is not enough: the wave was already merged."""
    stub.set_reply("task-T-001", _writer("a.txt", "first\n"))
    first = wave_orchestrator_node(state)
    branch = "bhai/rv/integration"

    # The merger has landed the rejected attempt on the integration branch.
    wt.merge(git_repo, branch=first["active_worktrees"][0]["branch"], into=branch, message="m")
    wt.git(git_repo, "checkout", branch)
    assert (git_repo / "a.txt").exists()

    stub.set_reply("task-T-001", _writer("a.txt", "second\n"))
    second = wave_orchestrator_node(
        {**state, **first, "review_verdict": "rework", "review_comments": "wrong"}
    )

    # The branch is back where the wave started, so the retry's own change is
    # measured against a tree that does not already contain it.
    assert wt.git(git_repo, "rev-parse", branch).stdout == first["wave_base_sha"]
    task = second["wave_results"][0]["tasks"][0]
    assert task["ok"] is True
    assert task["changed_files"] == ["a.txt"]


def test_the_rejected_attempts_branch_survives_the_reset(state, stub, git_repo):
    """The rejected work stays inspectable; only the integration branch moves."""
    stub.set_reply("task-T-001", _writer("a.txt", "first\n"))
    first = wave_orchestrator_node(state)
    rejected_branch = first["active_worktrees"][0]["branch"]

    stub.set_reply("task-T-001", _writer("a.txt", "second\n"))
    wave_orchestrator_node({**state, **first, "review_verdict": "rework", "review_comments": "no"})

    assert wt.git(git_repo, "rev-parse", "--verify", rejected_branch).ok


def test_a_rework_returns_to_the_waves_start_not_the_last_attempt(state, stub, git_repo):
    """Two rejected attempts both rewind to the same commit, not to each other."""
    stub.set_reply("task-T-001", _writer("a.txt", "first\n"))
    first = wave_orchestrator_node(state)
    start = first["wave_base_sha"]

    stub.set_reply("task-T-001", _writer("a.txt", "second\n"))
    second = wave_orchestrator_node(
        {**state, **first, "review_verdict": "rework", "review_comments": "no"}
    )
    assert second["wave_base_sha"] == start

    stub.set_reply("task-T-001", _writer("a.txt", "third\n"))
    third = wave_orchestrator_node(
        {**state, **second, "review_verdict": "rework", "review_comments": "still no"}
    )
    assert third["wave_base_sha"] == start


# ── Replan: back to where the run started ────────────────────────────────────


def test_a_replan_rewinds_the_integration_branch_to_the_runs_start(state, stub, git_repo):
    """A new plan is a fresh decomposition, not a patch on the one it replaces."""
    stub.set_reply("task-T-001", _writer("a.txt", "first\n"))
    first = wave_orchestrator_node(state)
    run_base = first["run_base_sha"]
    branch = "bhai/rv/integration"

    wt.merge(git_repo, branch=first["active_worktrees"][0]["branch"], into=branch, message="m")
    assert wt.git(git_repo, "rev-parse", branch).stdout != run_base

    stub.set_text(
        "planner",
        json.dumps({"summary": "again", "tasks": [{"task_id": "T-001", "description": "d"}]}),
    )
    result = planner_node(
        {**state, **first, "replan_count": 1, "supervisor_comments": "missed the point"}
    )

    assert wt.git(git_repo, "rev-parse", branch).stdout == run_base
    assert result["wave_base_sha"] == ""
    assert result["current_wave"] == 0


def test_a_first_plan_rewinds_nothing(state, stub, git_repo):
    stub.set_text(
        "planner",
        json.dumps({"summary": "s", "tasks": [{"task_id": "T-001", "description": "d"}]}),
    )
    before = wt.git(git_repo, "rev-parse", "HEAD").stdout

    planner_node(state)

    assert wt.git(git_repo, "rev-parse", "HEAD").stdout == before


def test_a_replan_with_no_recorded_base_says_so_rather_than_guessing(state, stub, caplog):
    """Never silently reset to something that was not measured."""
    stub.set_text(
        "planner",
        json.dumps({"summary": "s", "tasks": [{"task_id": "T-001", "description": "d"}]}),
    )

    result = planner_node({**state, "replan_count": 1, "supervisor_comments": "again"})

    assert "no run base recorded" in caplog.text
    assert result["current_wave"] == 0


def test_the_rewind_is_recorded_in_the_event_log(state, stub, git_repo):
    stub.set_reply("task-T-001", _writer("a.txt", "first\n"))
    first = wave_orchestrator_node(state)
    stub.set_text(
        "planner",
        json.dumps({"summary": "s", "tasks": [{"task_id": "T-001", "description": "d"}]}),
    )

    planner_node({**state, **first, "replan_count": 1, "supervisor_comments": "no"})

    events = art.prepare(state["run_dir"], state["target_repo"]).events.read_text(
        encoding="utf-8"
    )
    assert "replan_rewind" in events
