"""Agent 2 — planner, and the wave scheduler it depends on."""

from __future__ import annotations

import json

import pytest

import artifacts as art
from adapters.base import AgentResult
from planner import assign_waves, normalise_tasks, planner_node

PLAN = {
    "summary": "Add the endpoint, then its tests.",
    "tasks": [
        {
            "task_id": "T-001",
            "title": "Add /health",
            "description": "Register a blueprint exposing GET /health.",
            "files": ["app/health.py"],
            "acceptance": "curl localhost:5000/health returns 200",
            "depends_on": [],
        },
        {
            "task_id": "T-002",
            "title": "Test /health",
            "description": "Add a pytest covering the endpoint.",
            "files": ["tests/test_health.py"],
            "acceptance": "pytest passes",
            "depends_on": ["T-001"],
        },
    ],
}


def _task(task_id, deps=(), **kw):
    return {
        "task_id": task_id,
        "title": task_id,
        "description": f"do {task_id}",
        "depends_on": list(deps),
        **kw,
    }


# ── The wave scheduler: pure, so tested exhaustively ─────────────────────────


def test_independent_tasks_share_a_wave():
    schedule = assign_waves([_task("A"), _task("B"), _task("C")])
    assert schedule.ok
    assert schedule.waves == [["A", "B", "C"]]


def test_a_dependency_forces_a_later_wave():
    schedule = assign_waves([_task("B", ["A"]), _task("A")])
    assert schedule.ok
    assert schedule.waves == [["A"], ["B"]]


def test_a_diamond_collapses_to_three_waves():
    schedule = assign_waves(
        [_task("A"), _task("B", ["A"]), _task("C", ["A"]), _task("D", ["B", "C"])]
    )
    assert schedule.ok
    assert schedule.waves == [["A"], ["B", "C"], ["D"]]


def test_a_task_waits_for_its_deepest_dependency():
    """A task ready early still waits if anything it needs is not merged."""
    schedule = assign_waves([_task("A"), _task("B", ["A"]), _task("C", ["B"]), _task("D")])
    assert schedule.waves == [["A", "D"], ["B"], ["C"]]


def test_each_task_is_stamped_with_its_wave():
    schedule = assign_waves([_task("B", ["A"]), _task("A")])
    by_id = {task["task_id"]: task for task in schedule.tasks}
    assert by_id["A"]["wave"] == 0
    assert by_id["B"]["wave"] == 1


def test_tasks_come_back_in_execution_order():
    schedule = assign_waves([_task("C", ["B"]), _task("A"), _task("B", ["A"])])
    assert [task["task_id"] for task in schedule.tasks] == ["A", "B", "C"]


def test_a_dangling_dependency_is_a_failure_not_a_dropped_edge():
    """Dropping it would run a task against a tree missing what it needs."""
    schedule = assign_waves([_task("A", ["NOPE"])])
    assert schedule.ok is False
    assert "unknown task 'NOPE'" in schedule.error
    assert "'A'" in schedule.error


def test_a_cycle_is_reported_with_its_members():
    schedule = assign_waves([_task("A", ["B"]), _task("B", ["A"])])
    assert schedule.ok is False
    assert "cycle" in schedule.error
    assert "A" in schedule.error and "B" in schedule.error


def test_a_self_dependency_is_named_as_such():
    schedule = assign_waves([_task("A", ["A"])])
    assert schedule.ok is False
    assert "depends on itself" in schedule.error


def test_an_empty_plan_is_a_failure():
    assert assign_waves([]).ok is False


# ── Normalisation: what the agent gets wrong, named ──────────────────────────


def test_usable_tasks_survive_and_unusable_ones_are_named():
    tasks, problems = normalise_tasks(
        [
            _task("A"),
            {"title": "no id"},
            {"task_id": "B"},  # no description
            _task("A"),  # duplicate
            "not an object",
        ]
    )
    assert [task["task_id"] for task in tasks] == ["A"]
    assert len(problems) == 4
    assert any("no usable task_id" in p for p in problems)
    assert any("'B'" in p and "description" in p for p in problems)
    assert any("more than once" in p for p in problems)
    assert any("not an object" in p or "str, not an object" in p for p in problems)


def test_a_string_where_a_list_belongs_is_accepted():
    """A single dependency written bare is a model habit, not a defect."""
    tasks, problems = normalise_tasks(
        [{"task_id": "A", "description": "d", "depends_on": "B", "files": "x.py"}]
    )
    assert problems == []
    assert tasks[0]["depends_on"] == ["B"]
    assert tasks[0]["files"] == ["x.py"]


def test_an_id_field_is_accepted_in_place_of_task_id():
    tasks, _ = normalise_tasks([{"id": "A", "description": "d"}])
    assert tasks[0]["task_id"] == "A"


# ── The node ─────────────────────────────────────────────────────────────────


def test_plan_and_task_files_are_written(state, stub):
    stub.set_text("planner", json.dumps(PLAN), cost_usd=0.31)

    result = planner_node(state)

    artifacts = art.prepare(state["run_dir"])
    plan = art.read_json(artifacts.plan)
    assert plan["task_count"] == 2
    assert plan["waves"] == [["T-001"], ["T-002"]]
    assert plan["summary"].startswith("Add the endpoint")

    assert [t["task_id"] for t in art.load_tasks(artifacts)] == ["T-001", "T-002"]
    assert result["waves"] == [["T-001"], ["T-002"]]
    assert result["total_cost_usd"] == pytest.approx(0.31)


def test_the_planner_is_given_read_only_tools(state, stub):
    """The planner plans; the coding subagents implement."""
    stub.set_text("planner", json.dumps(PLAN))
    planner_node(state)
    assert "Write" not in stub.calls[0]["tools"]
    assert "Edit" not in stub.calls[0]["tools"]


def test_the_prompt_carries_the_requirements_and_the_user_choices(state, stub):
    artifacts = art.prepare(state["run_dir"])
    art.write_text(artifacts.context, "# Context\nMust expose /health.")
    art.write_text(artifacts.user_choices, "# User choices\n- JSON, not plain text")
    stub.set_text("planner", json.dumps(PLAN))

    planner_node({**state, "context": "# Context\nMust expose /health."})

    prompt = stub.calls[0]["prompt"]
    assert "Must expose /health." in prompt
    assert "JSON, not plain text" in prompt


def test_waves_are_derived_not_taken_from_the_agent(state, stub):
    """A 'waves' field in the reply must not override the derived schedule."""
    lying = {
        **PLAN,
        "waves": [["T-001", "T-002"]],  # would run the test before the endpoint
    }
    stub.set_text("planner", json.dumps(lying))

    result = planner_node(state)

    assert result["waves"] == [["T-001"], ["T-002"]]


def test_a_replan_carries_the_supervisor_feedback(state, stub):
    stub.set_text("planner", json.dumps(PLAN))

    planner_node(
        {
            **state,
            "replan_count": 1,
            "supervisor_comments": "No error handling for a dead database.",
        }
    )

    prompt = stub.calls[0]["prompt"]
    assert "No error handling for a dead database." in prompt
    assert "re-plan, not a revision" in prompt


def test_a_first_pass_carries_no_replan_section(state, stub):
    stub.set_text("planner", json.dumps(PLAN))
    planner_node({**state, "supervisor_comments": "stale from a previous run"})
    assert "stale from a previous run" not in stub.calls[0]["prompt"]


def test_a_replan_resets_the_wave_cursor_and_rework_count(state, stub):
    """The old schedule no longer exists; resuming into it would be nonsense."""
    stub.set_text("planner", json.dumps(PLAN))

    result = planner_node({**state, "current_wave": 3, "rework_count": 2, "replan_count": 1})

    assert result["current_wave"] == 0
    assert result["rework_count"] == 0


def test_discarded_tasks_are_recorded_as_a_learning(state, stub):
    """The plan that reaches the coding agents is smaller than the one written."""
    stub.set_text(
        "planner",
        json.dumps({**PLAN, "tasks": PLAN["tasks"] + [{"title": "no id at all"}]}),
    )

    planner_node(state)

    learnings = art.read_text(art.prepare(state["run_dir"]).learnings)
    assert "no usable task_id" in learnings


# ── Failure handling ─────────────────────────────────────────────────────────


def test_a_failed_agent_stops_the_run_with_a_reason(state, stub):
    stub.set_reply(
        "planner",
        AgentResult(ok=False, error_kind="timeout", error_message="ran past 600s"),
    )
    result = planner_node(state)
    assert result["status"] == "failed"
    assert "ran past 600s" in result["stop_reason"]


def test_an_unreadable_reply_stops_the_run(state, stub):
    stub.set_text("planner", "I have thought about this at length.")
    result = planner_node(state)
    assert result["status"] == "failed"
    assert "could not be read" in result["stop_reason"]


def test_a_reply_with_no_tasks_list_stops_the_run(state, stub):
    stub.set_text("planner", json.dumps({"summary": "all done, nothing needed"}))
    result = planner_node(state)
    assert result["status"] == "failed"
    assert "no 'tasks' list" in result["stop_reason"]


def test_an_unschedulable_plan_stops_the_run_naming_the_reason(state, stub):
    stub.set_text(
        "planner",
        json.dumps({"tasks": [_task("A", ["B"]), _task("B", ["A"])]}),
    )
    result = planner_node(state)
    assert result["status"] == "failed"
    assert "cannot be scheduled" in result["stop_reason"]
    assert "cycle" in result["stop_reason"]


def test_too_many_waves_is_bounded_and_says_so(state, stub, monkeypatch):
    """A bound that fires must be distinguishable from work completing."""
    monkeypatch.setattr("config.MAX_WAVES", 2)
    chain = [_task("A"), _task("B", ["A"]), _task("C", ["B"])]
    stub.set_text("planner", json.dumps({"tasks": chain}))

    result = planner_node(state)

    assert result["status"] == "failed"
    assert "MAX_WAVES is 2" in result["stop_reason"]
    assert result["events"][0]["error_kind"] == "bounded"


def test_a_plan_whose_tasks_are_all_unusable_stops_the_run(state, stub):
    """Silently planning nothing is the Bugs.md #15 shape."""
    stub.set_text("planner", json.dumps({"tasks": [{"title": "x"}, {"title": "y"}]}))
    result = planner_node(state)
    assert result["status"] == "failed"
    assert "no tasks" in result["stop_reason"]
