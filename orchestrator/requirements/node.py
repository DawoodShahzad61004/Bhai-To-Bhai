"""Agent 1 — requirements gathering.

    START -> [Req. gathering] -> Plan

Produces `context.md` and `user_choices.md`. Has no incoming feedback edge: once
the user's requirements are captured the pipeline never re-asks for them, it only
re-plans against them (docs/Research.md topic 23).

**Why this one box is two nodes.** LangGraph's `interrupt()` suspends the graph,
and on resume it re-executes the node **from its first line** — the interrupted
run's updates were never applied, because the node never returned. A single node
that surveyed the project and then paused would therefore pay for the survey
twice on every interactive run. Measured directly: one pause produced two survey
calls. So the expensive work finishes and is committed to state in `survey`, and
`clarify` opens with the pause, which makes its own re-execution free.

The seam is also honest about what the two stages are. `survey` reads the project
and decides what it cannot answer; `clarify` folds in what the user said. They
fail differently and cost differently.

**Who writes `user_choices.md`.** The orchestrator, not the agent. The notes
define that file as explicit user decisions only — no assumptions, no inferences,
nothing derived from code. The pipeline *knows* the questions it asked and the
answers it received, so it records those verbatim rather than asking a language
model to faithfully not invent things. The agent contributes only its extraction
of choices from the user's original goal, which is the one part it can see and
the orchestrator cannot.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from langgraph.types import interrupt

import artifacts as art
import config
import parsing
from adapters import run_agent
from logging_config import get_logger
from requirements.prompts import PIPELINE_FRAME, finalise_prompt, survey_prompt
from state import PipelineState, event

logger = get_logger(__name__)

AGENT = "requirements"

# Read-only. Agent 1 inspects the project and must not change it.
SURVEY_TOOLS = ("Read", "Glob", "Grep", "Bash")


# ── Document assembly ────────────────────────────────────────────────────────


def _context_markdown(*, goal: str, payload: dict, qa: list[tuple[str, str]]) -> str:
    """Assemble context.md from the agent's fields plus what the run knows."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    sections = [
        "# Context",
        "",
        f"_Gathered {stamp} by the requirements agent._",
        "",
        "## Goal",
        "",
        goal.strip(),
        "",
        "## Understanding",
        "",
        (parsing.require_str(payload, "understanding") or "_not provided_").strip(),
        "",
        "## Requirements",
        "",
        (parsing.require_str(payload, "requirements") or "_not provided_").strip(),
        "",
        "## Constraints from the existing codebase",
        "",
        (parsing.require_str(payload, "constraints") or "_none identified_").strip(),
    ]

    risks = parsing.string_list(payload, "open_risks")
    if risks:
        sections += ["", "## Open risks", ""]
        sections += [f"- {risk}" for risk in risks]

    if qa:
        sections += ["", "## Clarifications", ""]
        for question, answer in qa:
            sections += [f"**Q.** {question}", "", f"**A.** {answer}", ""]

    return "\n".join(sections).rstrip() + "\n"


def _user_choices_markdown(
    *,
    run_id: str,
    goal: str,
    stated: list[str],
    qa: list[tuple[str, str]],
) -> str:
    """Assemble one append-only user-choice ledger entry.

    Every line here traces to something the user actually said — either in the
    goal or in an answer. The file states its own rule, because it is meant to be
    readable by a person deciding whether to trust it.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [
        f"## Run `{run_id}` — {stamp}",
        "",
        "### Stated in the original request",
        "",
        goal.strip(),
        "",
    ]

    if stated:
        lines += ["### Decisions extracted from that request", ""]
        lines += [f"- {choice}" for choice in stated]
        lines += [""]

    lines += ["### Answered during clarification", ""]
    if qa:
        for question, answer in qa:
            lines += [f"- **{question}**", f"  - {answer}", ""]
    else:
        lines += ["_No clarifying questions were asked._", ""]

    return "\n".join(lines).rstrip() + "\n"


def _survey_digest(payload: dict) -> str:
    """The survey, flattened for the clarify prompt."""
    parts = []
    for key in ("understanding", "requirements", "constraints"):
        value = parsing.require_str(payload, key)
        if value:
            parts.append(f"### {key}\n{value}")
    return "\n\n".join(parts) or "(the survey produced nothing)"


def _finish(
    state: PipelineState,
    *,
    payload: dict,
    qa: list[tuple[str, str]],
    stated: list[str],
    unasked: list[str],
    cost: float,
) -> dict:
    """Write both artifacts and return the state update. Shared by both nodes."""
    artifacts = art.prepare(state["run_dir"], state["target_repo"])
    goal = state["goal"]

    context_md = _context_markdown(goal=goal, payload=payload, qa=qa)
    choices_md = _user_choices_markdown(
        run_id=state["run_id"], goal=goal, stated=stated, qa=qa
    )
    art.write_text(artifacts.context, context_md)
    art.append_user_choices(artifacts, state["run_id"], choices_md)

    if unasked:
        art.append_learning(
            artifacts,
            AGENT,
            "Ran non-interactively with open questions; the planner is working from "
            "assumptions on these points:\n"
            + "\n".join(f"- {question}" for question in unasked),
        )

    entry = event(
        "requirements_ready",
        agent=AGENT,
        questions_asked=len(qa),
        questions_unasked=len(unasked),
        context_chars=len(context_md),
        cost_usd=round(cost, 4),
    )
    art.append_event(artifacts, entry)
    logger.info(
        "[%s] context.md written | %d chars | %d question(s) answered",
        AGENT,
        len(context_md),
        len(qa),
    )

    return {
        "context_path": str(artifacts.context),
        "user_choices_path": str(artifacts.user_choices),
        "context": context_md,
        "total_cost_usd": state.get("total_cost_usd", 0.0) + cost,
        "events": [entry],
    }


def _failure(message: str, *, kind: str) -> dict:
    """Stop the run with a stated reason rather than a plausible empty result."""
    logger.error("[%s] %s", AGENT, message)
    return {
        "status": "failed",
        "stop_reason": message,
        "events": [event("requirements_failed", agent=AGENT, error_kind=kind, detail=message)],
    }


# ── Node 1: survey ───────────────────────────────────────────────────────────


def requirements_survey_node(state: PipelineState) -> dict:
    """Read the project, draft the requirements, decide what genuinely needs asking.

    Returns without writing anything when there are questions to ask: the
    documents are written once, by whichever node reaches the end.
    """
    target = state["target_repo"]
    artifacts = art.prepare(state["run_dir"], target)
    logger.info("[%s] surveying %s", AGENT, target)

    survey = run_agent(
        survey_prompt(
            goal=state["goal"],
            target_repo=target,
            context_path=str(artifacts.context),
            user_choices_path=str(artifacts.user_choices),
            learnings_path=str(artifacts.learnings),
            max_questions=config.MAX_CLARIFYING_QUESTIONS,
        ),
        spec=config.AGENTS[AGENT],
        system_prompt=PIPELINE_FRAME,
        cwd=target,
        tag=AGENT,
        tools=SURVEY_TOOLS,
    )
    if not survey.ok:
        return _failure(
            f"The requirements agent failed to survey the project. {survey.error_message}",
            kind=survey.error_kind,
        )

    parsed = parsing.extract_json(survey.text, survey.structured)
    if not parsed.ok:
        return _failure(
            f"The requirements agent's survey could not be read: {parsed.error}",
            kind="unparseable",
        )
    payload = parsed.value or {}

    questions = parsing.string_list(payload, "questions")[: config.MAX_CLARIFYING_QUESTIONS]
    stated = parsing.string_list(payload, "explicit_choices")

    if questions and config.INTERACTIVE_REQUIREMENTS:
        # Hand off to clarify. Everything expensive is committed to state here,
        # so the pause costs nothing to re-enter.
        logger.info("[%s] %d question(s) for the user", AGENT, len(questions))
        return {
            "survey_payload": payload,
            "survey_questions": questions,
            "survey_choices": stated,
            "survey_session": survey.session_id,
            "total_cost_usd": state.get("total_cost_usd", 0.0) + survey.cost_usd,
            "events": [
                event(
                    "requirements_questions",
                    agent=AGENT,
                    count=len(questions),
                    cost_usd=round(survey.cost_usd, 4),
                )
            ],
        }

    if questions:
        logger.info(
            "[%s] %d question(s) left unasked: INTERACTIVE_REQUIREMENTS is off",
            AGENT,
            len(questions),
        )

    return _finish(
        state,
        payload=payload,
        qa=[],
        stated=stated,
        unasked=questions,
        cost=survey.cost_usd,
    )


# ── Node 2: clarify ──────────────────────────────────────────────────────────


def requirements_clarify_node(state: PipelineState) -> dict:
    """Pause for the user's answers, then fold them into the requirements.

    `interrupt()` is the first thing this node does, deliberately. On resume the
    node re-runs from here, and everything above the pause would be paid for
    again — which is why the survey is not above it.
    """
    questions = state.get("survey_questions") or []
    payload: dict[str, Any] = dict(state.get("survey_payload") or {})
    stated = list(state.get("survey_choices") or [])

    answers = interrupt(
        {
            "agent": AGENT,
            "goal": state["goal"],
            "understanding": parsing.require_str(payload, "understanding") or "",
            "questions": questions,
        }
    )
    qa = _pair_answers(questions, answers)
    logger.info("[%s] resumed with %d of %d answered", AGENT, len(qa), len(questions))

    target = state["target_repo"]
    final = run_agent(
        finalise_prompt(
            goal=state["goal"],
            target_repo=target,
            survey=_survey_digest(payload),
            qa=qa,
        ),
        spec=config.AGENTS[AGENT],
        system_prompt=PIPELINE_FRAME,
        cwd=target,
        tag=f"{AGENT}-finalise",
        tools=("Read", "Glob", "Grep"),
        # Same session: the agent already read the project once, and paying for
        # that twice is the switching cost this pipeline exists to price.
        resume_session=state.get("survey_session", ""),
    )

    if final.ok:
        refined = parsing.extract_json(final.text, final.structured)
        if refined.ok and refined.value:
            payload = refined.value
        else:
            # Keep the survey rather than fail: the answers are recorded verbatim
            # below regardless, so nothing the user said is lost.
            logger.warning(
                "[%s] finalise reply was unreadable (%s); keeping the survey draft",
                AGENT,
                refined.error,
            )
    else:
        logger.warning(
            "[%s] finalise call failed (%s); keeping the survey draft",
            AGENT,
            final.error_message[:200],
        )

    return _finish(
        state,
        payload=payload,
        qa=qa,
        stated=stated,
        unasked=[],
        cost=final.cost_usd,
    )


def route_after_survey(state: PipelineState) -> str:
    """Whether the survey left anything for the user to answer.

    Returns a semantic key, not a node name. graph.py maps it onto whichever
    node actually exists, which is what lets stages be switched off in config.
    """
    if state.get("status") == "failed":
        return "failed"
    return "clarify" if state.get("survey_questions") else "done"


def _pair_answers(questions: list[str], answers: object) -> list[tuple[str, str]]:
    """Line the user's answers up with the questions, whatever shape they arrive in.

    main.py sends a list; a caller resuming by hand might send a dict keyed by
    question, or one string. Unanswered questions are dropped rather than paired
    with an empty answer, because `user_choices.md` records only what the user
    actually said.
    """
    paired: list[tuple[str, str]] = []

    if isinstance(answers, dict):
        for question in questions:
            answer = answers.get(question)
            if isinstance(answer, str) and answer.strip():
                paired.append((question, answer.strip()))
        return paired

    if isinstance(answers, str):
        answers = [answers]

    if isinstance(answers, (list, tuple)):
        for question, answer in zip(questions, answers):
            if isinstance(answer, str) and answer.strip():
                paired.append((question, answer.strip()))
    return paired
