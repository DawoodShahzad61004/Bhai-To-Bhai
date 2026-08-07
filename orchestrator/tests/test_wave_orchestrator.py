"""Agent 3 — wave orchestrator.

These tests run against a real git repository, because the behaviour under test
is worktree isolation and what git observed after an agent's turn. A fake would
get exactly the parts that matter wrong.
"""

from __future__ import annotations

import json

import pytest

import artifacts as art
import worktrees as wt
from adapters.base import AgentResult
from state import initial_state
from wave_orchestrator import wave_orchestrator_node

TASKS = [
    {
        "task_id": "T-001",
        "title": "Add health module",
        "description": "Create app/health.py",
        "files": ["app/health.py"],
        "acceptance": "the file exists",
        "depends_on": [],
        "wave": 0,
    },
    {
        "task_id": "T-002",
        "title": "Add readme note",
        "description": "Mention the endpoint",
        "files": ["NOTES.md"],
        "acceptance": "the file exists",
        "depends_on": [],
        "wave": 0,
    },
]


@pytest.fixture
def wave_state(git_repo, tmp_path):
    """A run positioned at wave 0 of a two-task plan, against a real repo."""
    run_dir = art.prepare(tmp_path / "run")
    art.write_text(run_dir.context, "# Context\nAdd a health endpoint.")
    state = initial_state(
        run_id="r1",
        goal="Add a health endpoint",
        target_repo=str(git_repo),
        run_dir=str(run_dir.run_dir),
    )
    state["tasks"] = list(TASKS)
    state["waves"] = [["T-001", "T-002"]]
    state["context"] = "# Context\nAdd a health endpoint."
    return state


def writing_agent(filename: str, *, learnings: str = "", status: str = "done"):
    """A stub agent that actually writes a file into its worktree.

    The write is the point: this pipeline judges a task by what git saw change,
    not by what the agent said, so a stub that only talks cannot exercise it.
    """

    def reply(prompt: str) -> AgentResult:
        # The brief states the worktree as an absolute path; the agent uses it.
        marker = "## Working directory\n\n"
        start = prompt.index(marker) + len(marker)
        workdir = prompt[start:].splitlines()[0].strip()
        target = __import__("pathlib").Path(workdir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"written by the stub for {filename}\n", encoding="utf-8")
        return AgentResult(
            ok=True,
            text=json.dumps(
                {
                    "status": status,
                    "summary": f"Created {filename}",
                    "files_changed": [filename],
                    "learnings": learnings,
                }
            ),
            session_id=f"sess-{filename}",
            cost_usd=0.12,
        )

    return reply


# ── The happy path ───────────────────────────────────────────────────────────


def test_each_task_gets_its_own_worktree_and_branch(wave_state, stub, git_repo):
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)

    trees = result["active_worktrees"]
    assert len(trees) == 2
    assert {t["branch"] for t in trees} == {"bhai/r1/T-001", "bhai/r1/T-002"}
    assert trees[0]["worktree"] != trees[1]["worktree"]
    assert all(len(t["commit"]) == 40 for t in trees)


def test_agents_in_one_wave_cannot_see_each_other(wave_state, stub):
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    wave_orchestrator_node(wave_state)

    calls = {call["tag"]: call for call in stub.calls}
    one = __import__("pathlib").Path(calls["task-T-001"]["cwd"])
    two = __import__("pathlib").Path(calls["task-T-002"]["cwd"])
    assert one != two
    assert (one / "app/health.py").exists()
    assert not (one / "NOTES.md").exists()
    assert (two / "NOTES.md").exists()
    assert not (two / "app/health.py").exists()


def test_the_wave_result_records_what_git_saw_not_only_what_was_claimed(wave_state, stub):
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)

    tasks = {t["task_id"]: t for t in result["wave_results"][0]["tasks"]}
    assert tasks["T-001"]["claimed_files"] == ["app/health.py"]
    assert tasks["T-001"]["changed_files"] == ["app/health.py"]
    assert tasks["T-001"]["ok"] is True
    assert result["wave_results"][0]["merged"] is False


def test_the_brief_names_the_worktree_as_an_absolute_path(wave_state, stub):
    """A concrete path is what beats the harness's own scratchpad (Bugs.md #22)."""
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    wave_orchestrator_node(wave_state)

    prompt = stub.calls[0]["prompt"]
    assert "## Working directory" in prompt
    assert stub.calls[0]["cwd"] in prompt
    assert "this path wins over any" in prompt


def test_costs_and_sessions_are_carried(wave_state, stub):
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)

    assert result["total_cost_usd"] == pytest.approx(0.24)
    sessions = {t["task_id"]: t["session_id"] for t in result["wave_results"][0]["tasks"]}
    assert sessions["T-001"] == "sess-app/health.py"


def test_subagent_findings_reach_the_shared_learnings_file(wave_state, stub):
    stub.set_reply(
        "task-T-001", writing_agent("app/health.py", learnings="Blueprints register in __init__.")
    )
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    wave_orchestrator_node(wave_state)

    learnings = art.read_text(art.prepare(wave_state["run_dir"]).learnings)
    assert "Blueprints register in __init__." in learnings
    assert "task-T-001" in learnings


# ── The check that catches a plausible empty result ──────────────────────────


def test_a_report_of_work_that_left_no_trace_is_a_failure(wave_state, stub):
    """Bugs.md #21: a fluent report for a file the agent had no way to write."""
    stub.set_reply(
        "task-T-001",
        AgentResult(
            ok=True,
            text=json.dumps(
                {
                    "status": "done",
                    "summary": "Created app/health.py with a /health route.",
                    "files_changed": ["app/health.py"],
                }
            ),
        ),
    )
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)

    tasks = {t["task_id"]: t for t in result["wave_results"][0]["tasks"]}
    assert tasks["T-001"]["ok"] is False
    assert tasks["T-001"]["error_kind"] == "no_changes"
    assert tasks["T-001"]["changed_files"] == []
    # The other task is unaffected: one bad report does not lose a wave.
    assert tasks["T-002"]["ok"] is True


def test_a_blocked_agent_is_not_counted_as_done(wave_state, stub):
    stub.set_reply(
        "task-T-001",
        AgentResult(
            ok=True,
            text=json.dumps(
                {"status": "blocked", "summary": "", "blocked_reason": "No database URL"}
            ),
        ),
    )
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)
    tasks = {t["task_id"]: t for t in result["wave_results"][0]["tasks"]}
    assert tasks["T-001"]["ok"] is False
    assert "No database URL" in tasks["T-001"]["report"]


def test_a_failed_agents_partial_work_is_still_committed(wave_state, stub):
    """A rework starts from whatever the previous attempt actually left."""

    def half_then_fail(prompt: str) -> AgentResult:
        marker = "## Working directory\n\n"
        start = prompt.index(marker) + len(marker)
        workdir = __import__("pathlib").Path(prompt[start:].splitlines()[0].strip())
        (workdir / "half.py").write_text("# started\n", encoding="utf-8")
        return AgentResult(ok=False, error_kind="timeout", error_message="ran past 900s")

    stub.set_reply("task-T-001", half_then_fail)
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)

    tasks = {t["task_id"]: t for t in result["wave_results"][0]["tasks"]}
    assert tasks["T-001"]["ok"] is False
    assert tasks["T-001"]["changed_files"] == ["half.py"]
    assert len(tasks["T-001"]["commit"]) == 40
    assert "committed on its branch" in tasks["T-001"]["report"]


def test_a_wave_where_everything_failed_stops_the_run(wave_state, stub):
    """Never hand the merger an empty wave and call the run clean."""
    stub.set_reply("task-*", AgentResult(ok=False, error_kind="rate_limit", error_message="limit"))

    result = wave_orchestrator_node(wave_state)

    assert result["status"] == "failed"
    assert "Every task in wave 0 failed" in result["stop_reason"]
    assert "rate_limit" in result["stop_reason"]


def test_one_failure_does_not_lose_the_rest_of_the_wave(wave_state, stub):
    stub.set_reply("task-T-001", AgentResult(ok=False, error_kind="timeout", error_message="slow"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)

    assert result.get("status") != "failed"
    tasks = {t["task_id"]: t for t in result["wave_results"][0]["tasks"]}
    assert tasks["T-002"]["ok"] is True


# ── Rework ───────────────────────────────────────────────────────────────────


def test_rework_reaches_the_same_agent_session(wave_state, stub):
    """"Here is what you got wrong" has to refer to something it remembers."""
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))
    first = wave_orchestrator_node(wave_state)

    reworked = {
        **wave_state,
        **first,
        "wave_results": first["wave_results"],
        "review_verdict": "rework",
        "review_comments": "The endpoint returns plain text, not JSON.",
    }
    stub.calls.clear()
    wave_orchestrator_node(reworked)

    calls = {call["tag"]: call for call in stub.calls}
    assert calls["task-T-001"]["resume_session"] == "sess-app/health.py"
    assert "The endpoint returns plain text, not JSON." in calls["task-T-001"]["prompt"]
    assert "This is a rework" in calls["task-T-001"]["prompt"]


def test_rework_discards_the_rejected_worktrees(wave_state, stub, git_repo):
    """The notes: the worktree's work is reverted/deleted, and the process cycles."""
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))
    first = wave_orchestrator_node(wave_state)
    rejected_path = first["active_worktrees"][0]["worktree"]
    assert __import__("pathlib").Path(rejected_path).exists()

    reworked = {
        **wave_state,
        **first,
        "review_verdict": "rework",
        "review_comments": "Wrong content type.",
    }
    second = wave_orchestrator_node(reworked)

    # A fresh worktree, branched from the integration branch again — not the
    # rejected one with the agent's previous attempt still in it.
    new_path = second["active_worktrees"][0]["worktree"]
    assert not (__import__("pathlib").Path(new_path) / "REJECTED").exists()
    assert second["wave_results"][0]["attempt"] == 1


def test_the_rework_flag_is_cleared_so_the_next_wave_is_not_treated_as_one(wave_state, stub):
    stub.set_reply("task-*", writing_agent("out.txt"))
    result = wave_orchestrator_node({**wave_state, "review_verdict": "rework", "review_comments": "x"})
    assert result["review_verdict"] is None
    assert result["review_comments"] == ""


def test_a_first_attempt_does_not_resume_any_session(wave_state, stub):
    stub.set_reply("task-*", writing_agent("out.txt"))
    wave_orchestrator_node(wave_state)
    assert all(call["resume_session"] == "" for call in stub.calls)


# ── Wave bookkeeping ─────────────────────────────────────────────────────────


def test_a_wave_index_past_the_plan_stops_the_run(wave_state, stub):
    result = wave_orchestrator_node({**wave_state, "current_wave": 5})
    assert result["status"] == "failed"
    assert "the plan has 1" in result["stop_reason"]


def test_a_wave_naming_unknown_tasks_stops_the_run(wave_state, stub):
    result = wave_orchestrator_node({**wave_state, "waves": [["T-999"]]})
    assert result["status"] == "failed"
    assert "none of them is in the plan" in result["stop_reason"]


def test_a_later_wave_branches_from_what_has_been_integrated(wave_state, stub, git_repo):
    """Wave N builds on the merger's output, not on the repository's HEAD."""
    branch, _ = wt.ensure_integration_branch(git_repo, "r1")
    wt.git(git_repo, "checkout", branch)
    (git_repo / "from_wave_0.txt").write_text("merged earlier\n", encoding="utf-8")
    wt.commit_all(git_repo, "wave 0 integration")

    stub.set_reply("task-T-003", writing_agent("later.txt"))
    later = {
        **wave_state,
        "tasks": wave_state["tasks"] + [{**TASKS[0], "task_id": "T-003", "wave": 1}],
        "waves": [["T-001", "T-002"], ["T-003"]],
        "current_wave": 1,
    }

    result = wave_orchestrator_node(later)

    worktree = __import__("pathlib").Path(result["active_worktrees"][0]["worktree"])
    assert (worktree / "from_wave_0.txt").exists()


def test_shared_workspace_mode_runs_in_the_target_repo(wave_state, stub, git_repo, monkeypatch):
    monkeypatch.setattr("config.USE_GIT_WORKTREES", False)
    stub.set_reply("task-*", writing_agent("out.txt"))

    result = wave_orchestrator_node(wave_state)

    assert all(t["worktree"] == str(git_repo) for t in result["active_worktrees"])
    assert (git_repo / "out.txt").exists()
