"""Agent 5 — reviewer."""

from __future__ import annotations

import json

import pytest

import artifacts as art
from adapters.base import AgentResult
from reviewer import reviewer_node
from state import initial_state

APPROVED = {
    "verdict": "approved",
    "assessment": "Both tasks are implemented and consistent with the requirements.",
    "problems": [],
    "learnings": "The blueprint registry lives in app/__init__.py.",
}

REWORK = {
    "verdict": "rework",
    "assessment": "The endpoint exists but returns plain text.",
    "problems": ["`app/health.py:24` returns a string, not JSON"],
    "rework_instructions": "Return jsonify({'status': 'ok'}) with content-type application/json.",
    "learnings": "",
}


@pytest.fixture
def review_state(tmp_path):
    run_dir = art.prepare(tmp_path / "run", tmp_path)
    art.write_text(run_dir.context, "# Context\nThe endpoint must return JSON.")
    state = initial_state(
        run_id="rv1",
        goal="Add a health endpoint",
        target_repo=str(tmp_path),
        run_dir=str(run_dir.run_dir),
    )
    state["context"] = "# Context\nThe endpoint must return JSON."
    state["current_wave"] = 0
    state["integration_branch"] = "bhai/rv1/integration"
    state["tasks"] = [
        {
            "task_id": "T-001",
            "title": "Add /health",
            "description": "Create app/health.py",
            "acceptance": "returns 200 with JSON",
        }
    ]
    state["wave_results"] = [
        {
            "wave": 0,
            "attempt": 0,
            "task_ids": ["T-001"],
            "merged": True,
            "tasks": [
                {
                    "task_id": "T-001",
                    "ok": True,
                    "report": "Added the health blueprint.",
                    "claimed_files": ["app/health.py"],
                    "changed_files": ["app/health.py"],
                    "session_id": "sess-1",
                }
            ],
        }
    ]
    return state


# ── Approval ─────────────────────────────────────────────────────────────────


def test_approval_clears_the_rework_budget_for_the_next_wave(review_state, stub):
    stub.set_text("reviewer", json.dumps(APPROVED), cost_usd=0.18)

    result = reviewer_node({**review_state, "rework_count": 1})

    assert result["review_verdict"] == "approved"
    assert result["rework_count"] == 0
    assert result["review_comments"] == ""
    assert result["total_cost_usd"] == pytest.approx(0.18)


def test_the_review_is_written_as_markdown(review_state, stub):
    """The drawing says this box "writes in the md file"."""
    stub.set_text("reviewer", json.dumps(REWORK))

    reviewer_node(review_state)

    notes = art.read_text(
        art.prepare(
            review_state["run_dir"], review_state["target_repo"]
        ).review_file(0, 0)
    )
    assert "**Verdict:** rework" in notes
    assert "returns a string, not JSON" in notes
    assert "Return jsonify" in notes


def test_the_reviewer_gets_read_only_tools(review_state, stub):
    """The reviewer reviews; the coding agents fix."""
    stub.set_text("reviewer", json.dumps(APPROVED))
    reviewer_node(review_state)
    assert "Write" not in stub.calls[0]["tools"]
    assert "Edit" not in stub.calls[0]["tools"]


def test_findings_reach_the_learnings_file(review_state, stub):
    stub.set_text("reviewer", json.dumps(APPROVED))
    reviewer_node(review_state)
    learnings = art.read_text(
        art.prepare(review_state["run_dir"], review_state["target_repo"]).learnings
    )
    assert "app/__init__.py" in learnings


# ── The evidence the reviewer is handed ──────────────────────────────────────


def test_the_brief_separates_the_claim_from_what_git_saw(review_state, stub):
    """A report is a claim; the changed-file list is evidence (Bugs.md #21)."""
    stub.set_text("reviewer", json.dumps(APPROVED))

    reviewer_node(review_state)

    prompt = stub.calls[0]["prompt"]
    assert "claimed to change app/health.py" in prompt
    assert "git saw changes to app/health.py" in prompt
    assert "Read the code, not the reports" in prompt


def test_a_task_that_changed_nothing_is_flagged_in_the_evidence(review_state, stub):
    review_state["wave_results"][0]["tasks"][0]["changed_files"] = []
    stub.set_text("reviewer", json.dumps(APPROVED))

    reviewer_node(review_state)

    assert "git saw changes to NOTHING" in stub.calls[0]["prompt"]


def test_a_failed_task_is_reported_as_failed_not_omitted(review_state, stub):
    review_state["wave_results"][0]["tasks"][0].update(
        {"ok": False, "error_kind": "timeout", "error_message": "ran past 900s"}
    )
    stub.set_text("reviewer", json.dumps(REWORK))

    reviewer_node(review_state)

    assert "T-001: FAILED (timeout)" in stub.calls[0]["prompt"]


def test_the_brief_scopes_judgement_to_this_wave(review_state, stub):
    """Later waves have not run; their work being absent is not a defect."""
    stub.set_text("reviewer", json.dumps(APPROVED))
    reviewer_node(review_state)
    assert "Scope your judgement to THIS wave" in stub.calls[0]["prompt"]


# ── Rework ───────────────────────────────────────────────────────────────────


def test_rework_carries_actionable_comments_forward(review_state, stub):
    stub.set_text("reviewer", json.dumps(REWORK))

    result = reviewer_node(review_state)

    assert result["review_verdict"] == "rework"
    assert result["rework_count"] == 1
    assert "Return jsonify" in result["review_comments"]
    assert "returns a string, not JSON" in result["review_comments"]
    assert result.get("status") != "bounded"


def test_the_verdict_is_recorded_on_the_attempt_it_judged(review_state, stub):
    stub.set_text("reviewer", json.dumps(REWORK))
    result = reviewer_node(review_state)
    record = result["wave_results"][0]
    assert (record["wave"], record["attempt"]) == (0, 0)
    assert record["review_verdict"] == "rework"


def test_rework_rounds_are_bounded_and_the_stop_says_so(review_state, stub, monkeypatch):
    """Stopped-because-bounded must not be recorded as stopped-because-done."""
    monkeypatch.setattr("config.MAX_REWORK_ROUNDS", 2)
    stub.set_text("reviewer", json.dumps(REWORK))

    result = reviewer_node({**review_state, "rework_count": 2})

    assert result["status"] == "bounded"
    assert "MAX_REWORK_ROUNDS is 2" in result["stop_reason"]
    assert "this is not a successful run" in result["stop_reason"]
    assert "Return jsonify" in result["stop_reason"]
    assert result["events"][-1]["kind"] == "rework_bound_reached"


def test_an_exhausted_bound_records_what_was_left_undone(review_state, stub, monkeypatch):
    monkeypatch.setattr("config.MAX_REWORK_ROUNDS", 1)
    stub.set_text("reviewer", json.dumps(REWORK))

    reviewer_node({**review_state, "rework_count": 1})

    learnings = art.read_text(
        art.prepare(review_state["run_dir"], review_state["target_repo"]).learnings
    )
    assert "could not be brought to an acceptable state" in learnings
    assert "Return jsonify" in learnings


def test_the_bound_is_per_wave_not_per_run(review_state, stub, monkeypatch):
    """A later wave gets its own budget; approval is what resets it."""
    monkeypatch.setattr("config.MAX_REWORK_ROUNDS", 1)
    stub.set_text("reviewer", json.dumps(APPROVED))

    result = reviewer_node({**review_state, "rework_count": 1})

    assert result["rework_count"] == 0
    assert result.get("status") != "bounded"


# ── Failure handling ─────────────────────────────────────────────────────────


def test_an_unreadable_verdict_is_not_treated_as_approval(review_state, stub):
    """The pipeline must not agree with itself about work nothing assessed."""
    stub.set_text("reviewer", "Looks good to me, ship it.")

    result = reviewer_node(review_state)

    assert result["status"] == "failed"
    assert result.get("review_verdict") is None
    assert "nothing has been approved" in result["stop_reason"]


def test_a_verdict_outside_the_closed_set_is_not_treated_as_approval(review_state, stub):
    stub.set_text("reviewer", json.dumps({"verdict": "looks fine", "assessment": "ok"}))
    result = reviewer_node(review_state)
    assert result["status"] == "failed"
    assert "could not be read" in result["stop_reason"]


def test_a_failed_review_agent_stops_the_run(review_state, stub):
    stub.set_reply(
        "reviewer", AgentResult(ok=False, error_kind="rate_limit", error_message="usage limit")
    )
    result = reviewer_node(review_state)
    assert result["status"] == "failed"
    assert "usage limit" in result["stop_reason"]


def test_reaching_the_reviewer_with_no_wave_stops_the_run(tmp_path):
    run_dir = art.prepare(tmp_path / "run")
    empty = initial_state(run_id="x", goal="g", target_repo=str(tmp_path), run_dir=str(run_dir.run_dir))
    result = reviewer_node(empty)
    assert result["status"] == "failed"
    assert "no wave to review" in result["stop_reason"]
