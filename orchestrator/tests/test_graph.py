"""The assembled pipeline: routing, the two loops, and the config toggles.

End-to-end over the stub transport, so a whole run — six agents, real git
worktrees, real merges — costs nothing and is deterministic.
"""

from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import artifacts as art
import routes
import worktrees as wt
from adapters.base import AgentResult
from graph import compile_graph
from state import initial_state

# ── Scripted agents ──────────────────────────────────────────────────────────

SURVEY = {
    "understanding": "A small repo with a README.",
    "requirements": "- Add NOTES.md\n- Add DOCS.md",
    "constraints": "None.",
    "explicit_choices": ["Markdown, not plain text"],
    "questions": [],
}


def plan_reply(task_specs):
    return json.dumps(
        {
            "summary": "Write the files.",
            "tasks": [
                {
                    "task_id": task_id,
                    "title": f"Write {filename}",
                    "description": f"Create {filename}",
                    "files": [filename],
                    "acceptance": "the file exists",
                    "depends_on": list(deps),
                }
                for task_id, filename, deps in task_specs
            ],
        }
    )


def approved_reply(*task_ids: str, assessment: str = "fine") -> str:
    """A reviewer reply keeping every named task_id.

    Extra task_ids beyond what a given review call actually needed a verdict
    for are harmless — the reviewer only reads entries for tasks it asked
    about — so one reply naming every task_id in a multi-wave plan is safe to
    reuse across every wave's review in the same test.
    """
    return json.dumps(
        {
            "assessment": assessment,
            "task_verdicts": [{"task_id": t, "verdict": "keep", "reason": ""} for t in task_ids],
            "learnings": "",
        }
    )


def rework_reply(*task_ids: str, assessment: str = "not acceptable", reason: str = "not acceptable") -> str:
    return json.dumps(
        {
            "assessment": assessment,
            "task_verdicts": [{"task_id": t, "verdict": "rework", "reason": reason} for t in task_ids],
            "learnings": "",
        }
    )


def writing_agent(filename: str):
    """A coding subagent that actually writes into its worktree.

    On a rework it writes *different* content, as a real agent addressing review
    comments would. That matters: the pipeline judges a task by what git saw
    change, so an agent that re-emits byte-identical content after being told to
    fix something has genuinely changed nothing and is correctly failed.
    """
    attempts = {"n": 0}

    def reply(prompt: str) -> AgentResult:
        from pathlib import Path

        marker = "## Working directory\n\n"
        workdir = Path(prompt[prompt.index(marker) + len(marker) :].splitlines()[0].strip())
        attempts["n"] += 1
        body = f"content for {filename}\n"
        if "This is a rework" in prompt:
            body = f"revised content for {filename} (attempt {attempts['n']})\n"
        (workdir / filename).write_text(body, encoding="utf-8")
        return AgentResult(
            ok=True,
            text=json.dumps(
                {"status": "done", "summary": f"wrote {filename}", "files_changed": [filename]}
            ),
            session_id=f"sess-{filename}",
            cost_usd=0.1,
        )

    return reply


@pytest.fixture
def pipeline(git_repo, tmp_path, stub, monkeypatch):
    """A ready-to-run pipeline over a real repo, with the common agents scripted."""
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", False)
    state = initial_state(
        run_id="g1",
        goal="Add two markdown files",
        target_repo=str(git_repo),
    )
    stub.set_text("requirements", json.dumps(SURVEY))
    stub.set_reply("task-T-001", writing_agent("NOTES.md"))
    stub.set_reply("task-T-002", writing_agent("DOCS.md"))
    return state


def run(state, *, reviewer=True, supervisor=True, thread_id="t"):
    graph = compile_graph(
        checkpointer=InMemorySaver(), enable_reviewer=reviewer, enable_supervisor=supervisor
    )
    return graph.invoke(
        state, {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    )


# ── Graph shape ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reviewer,supervisor", [(True, True), (True, False), (False, True), (False, False)]
)
def test_every_toggle_combination_compiles(reviewer, supervisor):
    compile_graph(enable_reviewer=reviewer, enable_supervisor=supervisor)


def test_a_disabled_stage_is_absent_from_the_graph_not_bypassed():
    """Switching a gate off removes the node; nothing can reach it by accident."""
    both = set(compile_graph().get_graph().nodes)
    assert "review" in both and "supervise" in both

    neither = set(compile_graph(enable_reviewer=False, enable_supervisor=False).get_graph().nodes)
    assert "review" not in neither
    assert "supervise" not in neither

    no_reviewer = set(compile_graph(enable_reviewer=False).get_graph().nodes)
    assert "review" not in no_reviewer
    assert "supervise" in no_reviewer


# ── Routers ──────────────────────────────────────────────────────────────────


def test_a_stopped_run_leaves_every_router_immediately():
    for status in ("failed", "bounded", "completed"):
        state = {"status": status, "waves": [["a"], ["b"]], "review_verdict": "rework"}
        assert routes.after_plan(state) == "stop"
        assert routes.after_orchestrate(state) == "stop"
        assert routes.after_merge(state) == "stop"
        assert routes.after_review(state) == "stop"
        assert routes.after_supervisor(state) == "stop"


def test_after_review_sends_a_rejected_wave_back():
    state = {"review_verdict": "rework", "current_wave": 0, "waves": [["a"], ["b"]]}
    assert routes.after_review(state) == "rework"


def test_after_review_advances_while_waves_remain():
    state = {"review_verdict": "approved", "current_wave": 0, "waves": [["a"], ["b"]]}
    assert routes.after_review(state) == "next_wave"


def test_after_review_leaves_the_loop_on_the_last_wave():
    state = {"review_verdict": "approved", "current_wave": 1, "waves": [["a"], ["b"]]}
    assert routes.after_review(state) == "supervise"


def test_a_spent_rework_budget_wins_over_the_rework_verdict():
    """Ordering: a bound that fired has already set status, and it is checked first."""
    state = {
        "status": "bounded",
        "review_verdict": "rework",
        "current_wave": 0,
        "waves": [["a"]],
    }
    assert routes.after_review(state) == "stop"


def test_advance_wave_moves_the_cursor_and_resets_the_budget():
    update = routes.advance_wave({"current_wave": 1, "rework_count": 2})
    assert update["current_wave"] == 2
    assert update["rework_count"] == 0
    assert update["review_verdict"] is None


def test_finalise_names_the_gate_that_did_not_run():
    """A result nothing assessed must not read like one that passed."""
    update = routes.finalise({"waves": [["a"], ["b"]], "status": "running"})
    assert update["status"] == "completed"
    assert "ENABLE_SUPERVISOR is off" in update["stop_reason"]
    assert "not that it is what was asked for" in update["stop_reason"]


def test_finalise_leaves_an_already_stopped_run_alone():
    assert routes.finalise({"status": "bounded", "waves": []}) == {}


# ── End to end, all six agents ───────────────────────────────────────────────


def test_a_full_run_with_both_gates_on(pipeline, stub, git_repo):
    stub.set_text("planner", plan_reply([("T-001", "NOTES.md", []), ("T-002", "DOCS.md", ["T-001"])]))
    stub.set_text("reviewer", approved_reply("T-001", "T-002"))
    stub.set_text("supervisor", json.dumps({"verdict": "accepted", "assessment": "all met", "unmet": []}))

    final = run(pipeline)

    assert final["status"] == "completed"
    assert final["supervisor_verdict"] == "accepted"
    assert len(final["waves"]) == 2
    assert all(record["merged"] for record in final["wave_results"])

    # The work is really on the integration branch.
    wt.git(git_repo, "checkout", final["integration_branch"])
    assert (git_repo / "NOTES.md").exists()
    assert (git_repo / "DOCS.md").exists()


def test_two_independent_tasks_share_one_wave(pipeline, stub, git_repo):
    stub.set_text("planner", plan_reply([("T-001", "NOTES.md", []), ("T-002", "DOCS.md", [])]))
    stub.set_text("reviewer", approved_reply("T-001", "T-002"))
    stub.set_text("supervisor", json.dumps({"verdict": "accepted", "assessment": "ok", "unmet": []}))

    final = run(pipeline)

    assert final["waves"] == [["T-001", "T-002"]]
    assert len(final["wave_results"]) == 1
    wt.git(git_repo, "checkout", final["integration_branch"])
    assert (git_repo / "NOTES.md").exists()
    assert (git_repo / "DOCS.md").exists()


def test_the_events_log_replays_the_whole_run(pipeline, stub):
    """A run has to be reconstructable after the fact from what it wrote."""
    stub.set_text("planner", plan_reply([("T-001", "NOTES.md", [])]))
    stub.set_text("reviewer", approved_reply("T-001"))
    stub.set_text("supervisor", json.dumps({"verdict": "accepted", "assessment": "ok", "unmet": []}))

    final = run(pipeline)

    kinds = [entry["kind"] for entry in final["events"]]
    assert kinds == [
        "requirements_ready",
        "plan_ready",
        "wave_finished",
        "wave_merged",
        "review_verdict",
        "supervisor_verdict",
    ]
    # And the same story is on disk, written as it happened.
    on_disk = art.prepare(
        pipeline["run_id"], pipeline["target_repo"]
    ).events.read_text(encoding="utf-8")
    assert "wave_started" in on_disk
    assert "supervisor_verdict" in on_disk


# ── The two feedback loops ───────────────────────────────────────────────────


def test_the_review_rework_loop_re_runs_the_same_wave(pipeline, stub, git_repo):
    stub.set_text("planner", plan_reply([("T-001", "NOTES.md", [])]))
    stub.set_text("supervisor", json.dumps({"verdict": "accepted", "assessment": "ok", "unmet": []}))

    verdicts = iter(
        [
            rework_reply("T-001", assessment="too short", reason="NOTES.md is empty. Write actual content."),
            approved_reply("T-001", assessment="better"),
        ]
    )
    stub.set_reply("reviewer", lambda prompt: AgentResult(ok=True, text=next(verdicts)))

    final = run(pipeline)

    assert final["status"] == "completed"
    attempts = [(r["wave"], r["attempt"]) for r in final["wave_results"]]
    assert attempts == [(0, 0), (0, 1)]
    # The rework instructions reached the coding agent.
    reworked = [c for c in stub.calls if c["tag"] == "task-T-001"][1]
    assert "Write actual content." in reworked["prompt"]
    assert reworked["resume_session"] == "sess-NOTES.md"


def test_the_supervisor_replan_loop_returns_to_the_planner(pipeline, stub):
    stub.set_text("planner", plan_reply([("T-001", "NOTES.md", [])]))
    stub.set_text("reviewer", approved_reply("T-001"))

    verdicts = iter(
        [
            {"verdict": "replan", "assessment": "misses the point", "unmet": ["No DOCS.md"],
             "replan_guidance": "The plan never covered DOCS.md."},
            {"verdict": "accepted", "assessment": "now complete", "unmet": []},
        ]
    )
    stub.set_reply("supervisor", lambda prompt: AgentResult(ok=True, text=json.dumps(next(verdicts))))

    final = run(pipeline)

    assert final["status"] == "completed"
    assert final["replan_count"] == 1
    planner_calls = [c for c in stub.calls if c["tag"] == "planner"]
    assert len(planner_calls) == 2
    assert "The plan never covered DOCS.md." in planner_calls[1]["prompt"]


def test_an_exhausted_rework_budget_stops_the_run_as_bounded(pipeline, stub, monkeypatch):
    """Stopped-because-bounded, never recorded as stopped-because-done."""
    monkeypatch.setattr("config.MAX_REWORK_ROUNDS", 1)
    stub.set_text("planner", plan_reply([("T-001", "NOTES.md", [])]))
    stub.set_text("reviewer", rework_reply("T-001", assessment="no", reason="still wrong. Fix it."))

    final = run(pipeline)

    assert final["status"] == "bounded"
    assert "MAX_REWORK_ROUNDS is 1" in final["stop_reason"]
    assert final.get("supervisor_verdict") is None  # never reached the last gate


def test_an_exhausted_replan_budget_stops_the_run_as_bounded(pipeline, stub, monkeypatch):
    monkeypatch.setattr("config.MAX_REPLAN_ROUNDS", 1)
    stub.set_text("planner", plan_reply([("T-001", "NOTES.md", [])]))
    stub.set_text("reviewer", approved_reply("T-001"))
    stub.set_text(
        "supervisor",
        json.dumps({"verdict": "replan", "assessment": "no", "unmet": ["nothing works"],
                    "replan_guidance": "Start over."}),
    )

    final = run(pipeline)

    assert final["status"] == "bounded"
    assert "MAX_REPLAN_ROUNDS is 1" in final["stop_reason"]
    assert final["replan_count"] == 1


# ── The toggles, end to end ──────────────────────────────────────────────────


def test_with_the_reviewer_off_a_wave_is_accepted_on_merge(pipeline, stub, git_repo):
    stub.set_text("planner", plan_reply([("T-001", "NOTES.md", []), ("T-002", "DOCS.md", ["T-001"])]))
    stub.set_text("supervisor", json.dumps({"verdict": "accepted", "assessment": "ok", "unmet": []}))

    final = run(pipeline, reviewer=False)

    assert final["status"] == "completed"
    assert not any(call["tag"] == "reviewer" for call in stub.calls)
    assert len(final["wave_results"]) == 2  # both waves still ran
    wt.git(git_repo, "checkout", final["integration_branch"])
    assert (git_repo / "DOCS.md").exists()


def test_with_the_supervisor_off_the_run_ends_at_the_last_wave(pipeline, stub):
    stub.set_text("planner", plan_reply([("T-001", "NOTES.md", [])]))
    stub.set_text("reviewer", approved_reply("T-001"))

    final = run(pipeline, supervisor=False)

    assert final["status"] == "completed"
    assert not any(call["tag"] == "supervisor" for call in stub.calls)
    assert "ENABLE_SUPERVISOR is off" in final["stop_reason"]


def test_with_both_off_the_pipeline_is_a_straight_line(pipeline, stub, git_repo):
    stub.set_text("planner", plan_reply([("T-001", "NOTES.md", []), ("T-002", "DOCS.md", ["T-001"])]))

    final = run(pipeline, reviewer=False, supervisor=False)

    assert final["status"] == "completed"
    tags = {call["tag"] for call in stub.calls}
    assert "reviewer" not in tags
    assert "supervisor" not in tags
    assert {"requirements", "planner", "task-T-001", "task-T-002"} <= tags
    wt.git(git_repo, "checkout", final["integration_branch"])
    assert (git_repo / "NOTES.md").exists()
    assert (git_repo / "DOCS.md").exists()


def test_with_the_reviewer_off_there_is_no_rework_loop(pipeline, stub):
    """The verdict is never set, so the rework branch is simply unreachable."""
    stub.set_text("planner", plan_reply([("T-001", "NOTES.md", [])]))
    stub.set_text("supervisor", json.dumps({"verdict": "accepted", "assessment": "ok", "unmet": []}))

    final = run(pipeline, reviewer=False)

    assert final.get("review_verdict") is None
    assert len([c for c in stub.calls if c["tag"] == "task-T-001"]) == 1


# ── Failures propagate out of the loop ───────────────────────────────────────


def test_a_failed_requirements_agent_ends_the_run_before_planning(pipeline, stub):
    stub.set_reply(
        "requirements", AgentResult(ok=False, error_kind="rate_limit", error_message="limit")
    )

    final = run(pipeline)

    assert final["status"] == "failed"
    assert not any(call["tag"] == "planner" for call in stub.calls)


def test_a_wave_that_produced_nothing_ends_the_run_before_merging(pipeline, stub):
    stub.set_text("planner", plan_reply([("T-001", "NOTES.md", [])]))
    stub.set_reply("task-T-001", AgentResult(ok=False, error_kind="timeout", error_message="slow"))

    final = run(pipeline)

    assert final["status"] == "failed"
    assert "Every task in wave 0 failed" in final["stop_reason"]
    assert not any(call["tag"] == "reviewer" for call in stub.calls)
