"""Agent 1 — requirements gathering."""

from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

import artifacts as art
import parsing
from adapters.base import AgentResult
from requirements import (
    requirements_clarify_node,
    requirements_survey_node,
    route_after_survey,
)
from state import PipelineState

SURVEY = {
    "understanding": "A small Flask service with no monitoring endpoints.",
    "requirements": "- Expose `GET /health`\n- Return 200 with a JSON body",
    "constraints": "Blueprints are registered in `app/__init__.py`.",
    "explicit_choices": ["Must return JSON, not plain text"],
    "questions": ["Should /health check the database connection?"],
}

FINAL = {
    "understanding": "A small Flask service with no monitoring endpoints.",
    "requirements": "- Expose `GET /health`\n- Return 200 with JSON\n- Ping the database",
    "constraints": "Blueprints are registered in `app/__init__.py`.",
    "open_risks": ["A DB ping makes /health fail during planned maintenance"],
}


def _graph():
    """Agent 1's two nodes wired as graph.py wires them, so interrupt() is real.

    A checkpointer is required for interrupt() to suspend anything, which is the
    same requirement main.py has to satisfy.
    """
    builder = StateGraph(PipelineState)
    builder.add_node("requirements", requirements_survey_node)
    builder.add_node("requirements_clarify", requirements_clarify_node)
    builder.add_edge(START, "requirements")
    builder.add_conditional_edges(
        "requirements",
        route_after_survey,
        {"clarify": "requirements_clarify", "done": END, "failed": END},
    )
    builder.add_edge("requirements_clarify", END)
    return builder.compile(checkpointer=InMemorySaver())


# ── Non-interactive path ─────────────────────────────────────────────────────


def test_writes_both_artifacts_and_returns_them(state, stub, monkeypatch):
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", False)
    stub.set_text("requirements", json.dumps(SURVEY), cost_usd=0.07)

    result = requirements_survey_node(state)

    assert result["context_path"].endswith("context.md")
    assert result["user_choices_path"].endswith("user_choices.md")
    assert "Expose `GET /health`" in result["context"]
    assert result["total_cost_usd"] == pytest.approx(0.07)


def test_the_agent_is_given_read_only_tools(state, stub, monkeypatch):
    """Agent 1 inspects the project; it must not change it."""
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", False)
    stub.set_text("requirements", json.dumps(SURVEY))

    requirements_survey_node(state)

    tools = stub.calls[0]["tools"]
    assert "Read" in tools
    assert "Write" not in tools
    assert "Edit" not in tools


def test_unasked_questions_are_recorded_as_a_learning(state, stub, monkeypatch):
    """A question the pipeline declined to ask is an assumption downstream."""
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", False)
    stub.set_text("requirements", json.dumps(SURVEY))

    requirements_survey_node(state)

    learnings = art.read_text(art.prepare(state["run_dir"]).learnings)
    assert "database connection" in learnings
    assert "assumptions" in learnings


def test_a_survey_with_no_questions_never_pauses(state, stub, monkeypatch):
    """Interactive mode plus an unambiguous goal is still a single call."""
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", True)
    stub.set_text("requirements", json.dumps({**SURVEY, "questions": []}))

    result = requirements_survey_node(state)

    assert len(stub.calls) == 1
    assert "context_path" in result


# ── user_choices.md: the strict file ─────────────────────────────────────────


def test_user_choices_records_only_what_the_user_said(state, stub, monkeypatch):
    """The notes' rule: no assumptions, no inferences, no derived requirements."""
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", False)
    stub.set_text("requirements", json.dumps(SURVEY))

    requirements_survey_node(state)
    choices = art.read_text(art.prepare(state["run_dir"]).user_choices)

    assert state["goal"] in choices
    assert "Must return JSON, not plain text" in choices
    # The agent's own reading of the codebase is context, not a user choice.
    assert "app/__init__.py" not in choices
    # An unanswered question is not a decision.
    assert "Should /health check" not in choices
    assert "No clarifying questions were asked" in choices


def test_answers_are_recorded_verbatim_not_paraphrased(state, stub, monkeypatch):
    """The pipeline owns this file, so a model cannot reword what the user said."""
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", True)
    stub.set_reply(
        "requirements",
        lambda prompt: AgentResult(ok=True, text=json.dumps(SURVEY), session_id="s-1"),
    )
    stub.set_text("requirements-finalise", json.dumps(FINAL))

    graph = _graph()
    thread = {"configurable": {"thread_id": "t1"}}
    graph.invoke(state, thread)
    graph.invoke(Command(resume=["Yes, but with a 200ms timeout"]), thread)

    choices = art.read_text(art.prepare(state["run_dir"]).user_choices)
    assert "Yes, but with a 200ms timeout" in choices
    assert "Should /health check the database connection?" in choices


# ── The interrupt ────────────────────────────────────────────────────────────


def test_material_questions_suspend_the_graph(state, stub, monkeypatch):
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", True)
    stub.set_text("requirements", json.dumps(SURVEY))

    graph = _graph()
    thread = {"configurable": {"thread_id": "t2"}}
    paused = graph.invoke(state, thread)

    assert "__interrupt__" in paused
    payload = paused["__interrupt__"][0].value
    assert payload["questions"] == ["Should /health check the database connection?"]
    assert payload["agent"] == "requirements"
    # Suspended before any file was written.
    assert not art.prepare(state["run_dir"]).context.exists()


def test_the_survey_is_paid_for_exactly_once_across_a_pause(state, stub, monkeypatch):
    """The reason agent 1 is two nodes rather than one.

    LangGraph re-executes an interrupted node from its first line on resume, and
    the interrupted run's updates were never applied. A single node that
    surveyed and then paused made two survey calls — measured, not assumed. The
    survey therefore finishes before the pause, and `clarify` opens with it.
    """
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", True)
    stub.set_reply(
        "requirements",
        lambda prompt: AgentResult(ok=True, text=json.dumps(SURVEY), session_id="s"),
    )
    stub.set_text("requirements-finalise", json.dumps(FINAL))

    graph = _graph()
    thread = {"configurable": {"thread_id": "t-once"}}
    graph.invoke(state, thread)
    graph.invoke(Command(resume=["Yes"]), thread)

    tags = [call["tag"] for call in stub.calls]
    assert tags.count("requirements") == 1
    assert tags.count("requirements-finalise") == 1


def test_resuming_folds_the_answers_into_the_requirements(state, stub, monkeypatch):
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", True)
    stub.set_reply(
        "requirements",
        lambda prompt: AgentResult(ok=True, text=json.dumps(SURVEY), session_id="sess-42"),
    )
    stub.set_text("requirements-finalise", json.dumps(FINAL))

    graph = _graph()
    thread = {"configurable": {"thread_id": "t3"}}
    graph.invoke(state, thread)
    done = graph.invoke(Command(resume=["Yes, ping it"]), thread)

    assert "Ping the database" in done["context"]
    assert "planned maintenance" in done["context"]  # open_risks surfaced


def test_the_second_call_resumes_the_same_vendor_session(state, stub, monkeypatch):
    """Re-reading the project for a second turn is the cost this avoids."""
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", True)
    stub.set_reply(
        "requirements",
        lambda prompt: AgentResult(ok=True, text=json.dumps(SURVEY), session_id="sess-42"),
    )
    stub.set_text("requirements-finalise", json.dumps(FINAL))

    graph = _graph()
    thread = {"configurable": {"thread_id": "t4"}}
    graph.invoke(state, thread)
    graph.invoke(Command(resume=["Yes"]), thread)

    finalise = [call for call in stub.calls if call["tag"] == "requirements-finalise"][0]
    assert finalise["resume_session"] == "sess-42"


def test_an_unanswered_question_is_not_recorded_as_answered(state, stub, monkeypatch):
    """Two questions asked, one answered: the blank one is dropped, not blanked."""
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", True)
    survey = {**SURVEY, "questions": ["Check the DB?", "Require auth?"]}
    stub.set_text("requirements", json.dumps(survey))
    stub.set_text("requirements-finalise", json.dumps(FINAL))

    graph = _graph()
    thread = {"configurable": {"thread_id": "t5"}}
    graph.invoke(state, thread)
    graph.invoke(Command(resume=["Yes", "   "]), thread)

    choices = art.read_text(art.prepare(state["run_dir"]).user_choices)
    assert "Check the DB?" in choices
    assert "Require auth?" not in choices


# ── Failure handling ─────────────────────────────────────────────────────────


def test_a_failed_agent_stops_the_run_with_a_reason(state, stub, monkeypatch):
    """Never a plausible empty result: say what went wrong (Bugs.md #15)."""
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", False)
    stub.set_reply(
        "requirements",
        AgentResult(ok=False, error_kind="rate_limit", error_message="usage limit reached"),
    )

    result = requirements_survey_node(state)

    assert result["status"] == "failed"
    assert "usage limit reached" in result["stop_reason"]
    assert result["events"][0]["error_kind"] == "rate_limit"
    assert "context_path" not in result


def test_an_unreadable_reply_stops_the_run(state, stub, monkeypatch):
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", False)
    stub.set_text("requirements", "I had a good look around and it all seems fine!")

    result = requirements_survey_node(state)

    assert result["status"] == "failed"
    assert "could not be read" in result["stop_reason"]


def test_a_failed_finalise_keeps_the_survey_and_the_answers(state, stub, monkeypatch):
    """A second-call failure must not lose what the user already told us."""
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", True)
    stub.set_text("requirements", json.dumps(SURVEY))
    stub.set_reply(
        "requirements-finalise",
        AgentResult(ok=False, error_kind="timeout", error_message="ran long"),
    )

    graph = _graph()
    thread = {"configurable": {"thread_id": "t6"}}
    graph.invoke(state, thread)
    done = graph.invoke(Command(resume=["Yes, ping it"]), thread)

    assert done.get("status") != "failed"
    assert "Expose `GET /health`" in done["context"]  # the survey draft survived
    choices = art.read_text(art.prepare(state["run_dir"]).user_choices)
    assert "Yes, ping it" in choices


# ── The parsing service these depend on ──────────────────────────────────────


def test_json_is_found_however_the_agent_presents_it():
    obj = {"verdict": "approved"}
    assert parsing.extract_json(json.dumps(obj)).value == obj
    assert parsing.extract_json(f"```json\n{json.dumps(obj)}\n```").value == obj
    assert parsing.extract_json(f"Here you go:\n{json.dumps(obj)}\nHope that helps").value == obj
    # structured_output from a --json-schema call wins outright.
    assert parsing.extract_json("nonsense", structured=obj).value == obj


def test_nested_objects_survive_extraction():
    payload = {"plan": {"tasks": [{"id": "T-1", "meta": {"x": 1}}]}}
    parsed = parsing.extract_json(f"Result:\n{json.dumps(payload)}\n")
    assert parsed.ok
    assert parsed.value == payload


def test_braces_inside_strings_do_not_break_extraction():
    payload = {"note": "use a f-string like f'{x}' here"}
    parsed = parsing.extract_json(f"see below\n{json.dumps(payload)}")
    assert parsed.ok
    assert parsed.value == payload


def test_prose_with_no_json_is_a_failure_not_a_default():
    parsed = parsing.extract_json("Looks good to me.")
    assert parsed.ok is False
    assert "no JSON object" in parsed.error


def test_a_verdict_outside_the_closed_set_is_rejected():
    """A reviewer whose reply cannot be read has not approved anything."""
    assert parsing.one_of({"v": "approved"}, "v", ("approved", "rework")) == "approved"
    assert parsing.one_of({"v": "APPROVED"}, "v", ("approved", "rework")) == "approved"
    assert parsing.one_of({"v": "looks fine"}, "v", ("approved", "rework")) is None
    assert parsing.one_of({}, "v", ("approved", "rework")) is None
