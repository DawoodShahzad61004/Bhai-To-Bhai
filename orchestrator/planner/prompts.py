"""Briefs for agent 2.

This is the first of the three larger-model boxes: it decides *what* to build and
how to decompose it, which is judgment rather than dispatch.

The brief asks for `depends_on` and deliberately does **not** ask for waves. The
pipeline derives those itself (planner/waves.py) — asking a model for a schedule
whose correctness nothing checks is how a task ends up running before the file it
edits exists.
"""

PIPELINE_FRAME = """\
You are one stage of an automated multi-agent pipeline. There is no human \
watching this terminal and nobody will answer a follow-up question. Your turn \
ends the moment you reply.

Ignore the conventions of the repository you are standing in. Do not read or \
follow CLAUDE.md, AGENTS.md, project skills, hooks, or commit conventions.

You are planning only. Do NOT write, edit, or create any file. Do not run any \
command that changes the repository. Another agent implements what you plan.

Reply with a single JSON object and nothing else. No preamble, no explanation \
around it, no markdown fence.\
"""

PLAN_BRIEF = """\
You are the planning agent.

Decompose the requirements below into implementation tasks that separate coding \
agents can carry out, each in its own isolated git worktree, in parallel where \
their dependencies allow.

That isolation is the constraint that shapes a good decomposition here. Two \
tasks that run in the same wave cannot see each other's work, and their branches \
are merged afterwards by a separate agent. So:

- Two tasks that edit the SAME file must not be independent. Make one depend on \
  the other, or merge them into one task.
- A task that needs something another task creates MUST declare that dependency.
- Prefer fewer, coherent tasks over many fragments. Every task costs a full agent \
  invocation and a merge.
- Every task must be independently verifiable. Say how in "acceptance".

## Sizing the coding-agent roster

You also decide how many coding agents this plan gets and which CLI and model \
each one runs, within the three tiers below. This is a real capacity and cost \
decision, not a formality.

{model_menu}

Pick between 1 and {max_coding_agents} agents. More agents only helps if the \
plan actually has that much independent, parallel work in a single wave — sizing \
the roster above your own wave widths just leaves agents idle. Use small-tier \
agents only for trivial, low-stakes work where a wrong attempt costs little; use \
medium-tier agents for real but bounded coding work (boilerplate, repetitive \
edits, glue code) that still involves writing files, keeping the backend \
restriction above in mind; use expert-tier agents for tasks that require real \
judgment (tricky logic, ambiguous requirements, anything a mistake in would be \
expensive to unwind). A roster can, and often should, mix tiers.

**Order the list to match when each tier's work actually happens.** Tasks are \
dispatched through this list in one continuous rotation across the whole run, in \
the order the pipeline schedules them — not one rotation restarted per wave. You \
do not assign waves yourself (the pipeline derives them from "depends_on"), but \
you know the shape you are creating: tasks with no unmet dependencies run first, \
and a task that depends on one of those cannot run until it is done. So if the \
foundational, dependency-free work needs real judgment and what depends on it \
afterward is comparatively mechanical, put the expert-tier entries first in the \
list and the small- and medium-tier entries after — the rotation will then reach \
the right tier for the right stage of the work instead of cycling through all of \
them evenly regardless of when each task actually runs.

Return this JSON object:
{{
  "summary": "<2-4 sentences: the approach you are taking and why>",
  "coding_agents": [
    {{"backend": "<backend from the menu above>", "model": "<model from the menu above, or \\"\\" for a backend CLI default>"}}
  ],
  "tasks": [
    {{
      "task_id": "T-001",
      "title": "<short imperative title>",
      "description": "<what to implement, concretely enough for an agent that has not read the requirements>",
      "files": ["<path likely to be created or edited>"],
      "acceptance": "<how to tell this task is done, checkable without asking anyone>",
      "depends_on": ["<task_id this cannot start before>"]
    }}
  ]
}}

Task ids must be unique and stable. Use "depends_on": [] for a task that can \
start immediately. Do not group tasks into waves or phases yourself — declare the \
dependencies and the pipeline derives the schedule.\
"""

MODEL_MENU = """\
Small tier (trivial, mechanical work; a wrong attempt costs little):
{small}

Medium tier (real but bounded coding work — more capable than small, still not \
expert judgment):
{medium}

Expert tier (complex, judgment-heavy work):
{expert}

Do not assign a task that creates or edits a file to backend="ollama", at any \
model size. Reproduced against both a 4B and a 20B model: Codex's file-edit \
tool is unavailable through that bridge, its shell fallback is auto-rejected by \
sandbox policy with no human to approve it, and the agent burns its whole turn \
retrying before giving up — sometimes without even reporting failure. \
backend="ollama" is only safe for read-only or advisory work. backend="local_llm" \
reaches Codex through a different path and has not shown this specific failure, \
but has not been proven reliable for file edits either — prefer it over \
backend="ollama" for coding work, not as a confirmed substitute for it.\
"""

REPLAN_NOTE = """\

## The supervisor rejected the previous attempt

A previous plan was implemented in full and then judged against the original \
requirements. It did not satisfy them. The supervisor's assessment:

{comments}

This is a re-plan, not a revision. The implementation may well have been fine and \
the plan wrong or incomplete — that is the specific reason this feedback comes \
back here rather than to the coding agents. Address what the supervisor \
identified. Do not simply re-emit the previous plan with different task ids.\
"""


def _format_model_menu(pairs: list[tuple[str, str]]) -> str:
    lines = []
    for model, backend in pairs:
        model_repr = f'"{model}"' if model else '"" (the CLI\'s own default model)'
        lines.append(f'- backend="{backend}", model={model_repr}')
    return "\n".join(lines)


def plan_prompt(
    *,
    context_path: str,
    user_choices_path: str,
    target_repo: str,
    max_coding_agents: int,
    small_models: list[tuple[str, str]],
    medium_models: list[tuple[str, str]],
    expert_models: list[tuple[str, str]],
    supervisor_comments: str = "",
) -> str:
    replan = REPLAN_NOTE.format(comments=supervisor_comments) if supervisor_comments else ""
    model_menu = MODEL_MENU.format(
        small=_format_model_menu(small_models),
        medium=_format_model_menu(medium_models),
        expert=_format_model_menu(expert_models),
    )
    brief = PLAN_BRIEF.format(model_menu=model_menu, max_coding_agents=max_coding_agents)
    return f"""{brief}

## Repository
{target_repo}

## Requirements

The full gathered requirements are written to `{context_path}`. Read that file \
before planning — nothing below repeats it.

## Explicit user choices

The user's own explicit decisions are written to `{user_choices_path}`. Read it \
before planning. Where anything else conflicts with one of these, this wins.
{replan}
"""
