"""Agent 5 — reviewer."""

from __future__ import annotations

import json

import pytest

import artifacts as art
from adapters.base import AgentResult
from reviewer import reviewer_node
from state import initial_state

APPROVED = {
    "assessment": "Both tasks are implemented and consistent with the requirements.",
    "task_verdicts": [{"task_id": "T-001", "verdict": "keep", "reason": ""}],
    "learnings": "The blueprint registry lives in app/__init__.py.",
}

REWORK = {
    "assessment": "The endpoint exists but returns plain text.",
    "task_verdicts": [
        {
            "task_id": "T-001",
            "verdict": "rework",
            "reason": (
                "`app/health.py:24` returns a string, not JSON. Return "
                "jsonify({'status': 'ok'}) with content-type application/json."
            ),
        }
    ],
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


def test_a_task_verdict_outside_the_closed_set_defaults_to_rework(review_state, stub):
    """A per-task verdict the pipeline doesn't recognise is not a "keep"."""
    stub.set_text(
        "reviewer",
        json.dumps(
            {
                "assessment": "ok",
                "task_verdicts": [{"task_id": "T-001", "verdict": "looks fine", "reason": ""}],
            }
        ),
    )
    result = reviewer_node(review_state)
    assert result["review_verdict"] == "rework"
    assert result["wave_results"][0]["task_verdicts"]["T-001"]["verdict"] == "rework"


def test_a_failed_review_agent_stops_the_run(review_state, stub):
    stub.set_reply(
        "reviewer", AgentResult(ok=False, error_kind="rate_limit", error_message="usage limit")
    )
    result = reviewer_node(review_state)
    assert result["status"] == "failed"
    assert "usage limit" in result["stop_reason"]


# ── Per-task verdicts (docs/Bugs.md #35) ────────────────────────────────────


@pytest.fixture
def two_task_review_state(review_state):
    state = {**review_state}
    state["tasks"] = review_state["tasks"] + [
        {
            "task_id": "T-002",
            "title": "Add /health/ready",
            "description": "Create app/ready.py",
            "acceptance": "returns 200",
        }
    ]
    state["wave_results"] = [
        {
            **review_state["wave_results"][0],
            "task_ids": ["T-001", "T-002"],
            "tasks": review_state["wave_results"][0]["tasks"]
            + [
                {
                    "task_id": "T-002",
                    "ok": True,
                    "report": "Added the readiness endpoint.",
                    "claimed_files": ["app/ready.py"],
                    "changed_files": ["app/ready.py"],
                    "session_id": "sess-2",
                }
            ],
        }
    ]
    return state


def test_one_rejected_task_among_kept_ones_derives_a_wave_level_rework(
    two_task_review_state, stub
):
    stub.set_text(
        "reviewer",
        json.dumps(
            {
                "assessment": "T-001 is fine; T-002 is broken.",
                "task_verdicts": [
                    {"task_id": "T-001", "verdict": "keep", "reason": ""},
                    {"task_id": "T-002", "verdict": "rework", "reason": "returns 500"},
                ],
                "learnings": "",
            }
        ),
    )

    result = reviewer_node(two_task_review_state)

    assert result["review_verdict"] == "rework"
    record = result["wave_results"][0]["task_verdicts"]
    assert record["T-001"]["verdict"] == "keep"
    assert record["T-002"]["verdict"] == "rework"
    # T-001's own feedback never leaks into T-002's — and vice versa.
    assert result["review_comments"] == "- T-002: returns 500"


def test_all_tasks_kept_derives_approved(two_task_review_state, stub):
    stub.set_text(
        "reviewer",
        json.dumps(
            {
                "assessment": "Both are fine.",
                "task_verdicts": [
                    {"task_id": "T-001", "verdict": "keep", "reason": ""},
                    {"task_id": "T-002", "verdict": "keep", "reason": ""},
                ],
                "learnings": "",
            }
        ),
    )

    result = reviewer_node(two_task_review_state)

    assert result["review_verdict"] == "approved"


def test_a_failed_task_is_forced_to_rework_regardless_of_the_model(
    two_task_review_state, stub
):
    """A task with nothing mergeable is never a judgement call."""
    two_task_review_state["wave_results"][0]["tasks"][1]["ok"] = False
    two_task_review_state["wave_results"][0]["tasks"][1]["error_kind"] = "no_changes"
    stub.set_text(
        "reviewer",
        json.dumps(
            {
                "assessment": "T-001 is fine.",
                # The model is not even asked about T-002 (it never merged), but
                # even if it tried to weigh in, the failed status wins.
                "task_verdicts": [
                    {"task_id": "T-001", "verdict": "keep", "reason": ""},
                    {"task_id": "T-002", "verdict": "keep", "reason": "looks fine to me"},
                ],
                "learnings": "",
            }
        ),
    )

    result = reviewer_node(two_task_review_state)

    assert result["review_verdict"] == "rework"
    assert result["wave_results"][0]["task_verdicts"]["T-002"]["verdict"] == "rework"


def test_the_prompt_asks_a_verdict_only_for_tasks_that_actually_merged(
    two_task_review_state, stub
):
    two_task_review_state["wave_results"][0]["tasks"][1]["ok"] = False
    stub.set_text("reviewer", json.dumps(APPROVED))

    reviewer_node(two_task_review_state)

    prompt = stub.calls[0]["prompt"]
    assert "excluded" in prompt


def test_a_task_kept_in_an_earlier_attempt_is_named_as_context_not_rejudged(
    two_task_review_state, stub
):
    # Simulate attempt 1 of this wave, where T-001 was already kept in attempt 0.
    two_task_review_state["wave_results"] = [
        {
            "wave": 0,
            "attempt": 0,
            "task_ids": ["T-001"],
            "merged": True,
            "tasks": two_task_review_state["wave_results"][0]["tasks"][:1],
            "task_verdicts": {"T-001": {"verdict": "keep", "reason": ""}},
        },
        {
            "wave": 0,
            "attempt": 1,
            "task_ids": ["T-002"],
            "merged": True,
            "tasks": two_task_review_state["wave_results"][0]["tasks"][1:],
        },
    ]
    stub.set_text("reviewer", json.dumps(APPROVED))

    reviewer_node(two_task_review_state)

    assert "T-001" in stub.calls[0]["prompt"]
    assert "already accepted in an earlier attempt" in stub.calls[0]["prompt"].lower()


def test_reaching_the_reviewer_with_no_wave_stops_the_run(tmp_path):
    run_dir = art.prepare(tmp_path / "run")
    empty = initial_state(run_id="x", goal="g", target_repo=str(tmp_path), run_dir=str(run_dir.run_dir))
    result = reviewer_node(empty)
    assert result["status"] == "failed"
    assert "no wave to review" in result["stop_reason"]
