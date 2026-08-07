"""Agent 6 — supervisor."""

from __future__ import annotations

import json

import pytest

import artifacts as art
from adapters.base import AgentResult
from state import initial_state
from supervisor import supervisor_node

ACCEPTED = {
    "verdict": "accepted",
    "assessment": "Every requirement in context.md is satisfied by the merged code.",
    "unmet": [],
    "learnings": "Health endpoints in this codebase belong in app/ops/.",
}

REPLAN = {
    "verdict": "replan",
    "assessment": "The endpoint exists but nothing checks the database.",
    "unmet": ["The health check must report database reachability"],
    "replan_guidance": "The plan never produced a task for the database probe.",
    "learnings": "",
}


@pytest.fixture
def final_state(tmp_path):
    run_dir = art.prepare(tmp_path / "run")
    art.write_text(run_dir.context, "# Context\nHealth check must probe the database.")
    art.write_text(run_dir.user_choices, "# User choices\n- Must return JSON")
    art.write_json(run_dir.plan, {"summary": "Add the endpoint, then its tests."})

    state = initial_state(
        run_id="sv1",
        goal="Add a health endpoint",
        target_repo=str(tmp_path),
        run_dir=str(run_dir.run_dir),
    )
    state["context"] = "# Context\nHealth check must probe the database."
    state["waves"] = [["T-001"], ["T-002"]]
    state["integration_branch"] = "bhai/sv1/integration"
    state["wave_results"] = [
        {
            "wave": 0,
            "attempt": 0,
            "merged": True,
            "review_verdict": "approved",
            "tasks": [
                {
                    "task_id": "T-001",
                    "ok": True,
                    "report": "Added the endpoint.",
                    "changed_files": ["app/health.py"],
                }
            ],
        },
        {
            "wave": 1,
            "attempt": 0,
            "merged": True,
            "review_verdict": "approved",
            "tasks": [
                {
                    "task_id": "T-002",
                    "ok": True,
                    "report": "Added the test.",
                    "changed_files": ["tests/test_health.py"],
                }
            ],
        },
    ]
    return state


# ── Acceptance ───────────────────────────────────────────────────────────────


def test_acceptance_completes_the_run(final_state, stub):
    stub.set_text("supervisor", json.dumps(ACCEPTED), cost_usd=0.22)

    result = supervisor_node(final_state)

    assert result["supervisor_verdict"] == "accepted"
    assert result["status"] == "completed"
    assert "accepted the result" in result["stop_reason"]
    assert result["total_cost_usd"] == pytest.approx(0.22)


def test_the_assessment_is_written_as_markdown(final_state, stub):
    stub.set_text("supervisor", json.dumps(REPLAN))

    supervisor_node(final_state)

    notes = art.read_text(art.prepare(final_state["run_dir"]).supervisor_file(0))
    assert "**Verdict:** replan" in notes
    assert "database reachability" in notes
    assert "never produced a task for the database probe" in notes


def test_the_supervisor_gets_read_only_tools(final_state, stub):
    stub.set_text("supervisor", json.dumps(ACCEPTED))
    supervisor_node(final_state)
    assert "Write" not in stub.calls[0]["tools"]
    assert "Edit" not in stub.calls[0]["tools"]


def test_findings_reach_the_learnings_file(final_state, stub):
    stub.set_text("supervisor", json.dumps(ACCEPTED))
    supervisor_node(final_state)
    learnings = art.read_text(art.prepare(final_state["run_dir"]).learnings)
    assert "app/ops/" in learnings


# ── What the supervisor is handed ────────────────────────────────────────────


def test_the_brief_carries_the_requirements_and_the_user_choices(final_state, stub):
    stub.set_text("supervisor", json.dumps(ACCEPTED))

    supervisor_node(final_state)

    prompt = stub.calls[0]["prompt"]
    assert "Health check must probe the database." in prompt
    assert "Must return JSON" in prompt
    assert "Add the endpoint, then its tests." in prompt


def test_the_brief_distinguishes_this_question_from_the_reviewers(final_state, stub):
    """Every task done well and the goal still missed is what this box catches."""
    stub.set_text("supervisor", json.dumps(ACCEPTED))
    supervisor_node(final_state)
    prompt = stub.calls[0]["prompt"]
    assert "different from the reviewer's" in prompt
    assert "back to the planner" in prompt


def test_every_merged_wave_appears_in_the_summary(final_state, stub):
    stub.set_text("supervisor", json.dumps(ACCEPTED))

    supervisor_node(final_state)

    prompt = stub.calls[0]["prompt"]
    assert "### Wave 0" in prompt
    assert "### Wave 1" in prompt
    assert "T-001 [ok] changed app/health.py" in prompt


def test_a_superseded_attempt_is_left_out_of_the_summary(final_state, stub):
    """A rejected attempt was replaced; only what landed describes the result."""
    final_state["wave_results"].insert(
        0,
        {
            "wave": 0,
            "attempt": 0,
            "merged": False,
            "review_verdict": "rework",
            "tasks": [{"task_id": "T-001", "ok": True, "report": "first try", "changed_files": ["x.py"]}],
        },
    )
    stub.set_text("supervisor", json.dumps(ACCEPTED))

    supervisor_node(final_state)

    assert "first try" not in stub.calls[0]["prompt"]


# ── Replan ───────────────────────────────────────────────────────────────────


def test_replan_carries_guidance_back_to_the_planner(final_state, stub):
    stub.set_text("supervisor", json.dumps(REPLAN))

    result = supervisor_node(final_state)

    assert result["supervisor_verdict"] == "replan"
    assert result["replan_count"] == 1
    assert "never produced a task for the database probe" in result["supervisor_comments"]
    assert "database reachability" in result["supervisor_comments"]
    assert result.get("status") != "bounded"


def test_replans_are_bounded_and_the_stop_says_so(final_state, stub, monkeypatch):
    """The diagram's open question, closed: it escalates rather than looping."""
    monkeypatch.setattr("config.MAX_REPLAN_ROUNDS", 1)
    stub.set_text("supervisor", json.dumps(REPLAN))

    result = supervisor_node({**final_state, "replan_count": 1})

    assert result["status"] == "bounded"
    assert "MAX_REPLAN_ROUNDS is 1" in result["stop_reason"]
    assert "this is not a successful run" in result["stop_reason"]
    assert "database probe" in result["stop_reason"]
    assert result["events"][-1]["kind"] == "replan_bound_reached"


def test_an_exhausted_bound_records_what_was_left_unmet(final_state, stub, monkeypatch):
    monkeypatch.setattr("config.MAX_REPLAN_ROUNDS", 1)
    stub.set_text("supervisor", json.dumps(REPLAN))

    supervisor_node({**final_state, "replan_count": 1})

    learnings = art.read_text(art.prepare(final_state["run_dir"]).learnings)
    assert "The goal was not reached" in learnings
    assert "database probe" in learnings


# ── Failure handling ─────────────────────────────────────────────────────────


def test_an_unreadable_verdict_is_not_treated_as_acceptance(final_state, stub):
    """The last gate must not let a run declare itself finished on prose."""
    stub.set_text("supervisor", "Yes, this all looks correct to me.")

    result = supervisor_node(final_state)

    assert result["status"] == "failed"
    assert result.get("supervisor_verdict") is None
    assert "has not been accepted" in result["stop_reason"]


def test_a_failed_supervising_agent_stops_the_run(final_state, stub):
    stub.set_reply(
        "supervisor", AgentResult(ok=False, error_kind="timeout", error_message="ran past 600s")
    )
    result = supervisor_node(final_state)
    assert result["status"] == "failed"
    assert "ran past 600s" in result["stop_reason"]
