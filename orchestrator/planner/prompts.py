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

Return this JSON object:
{{
  "summary": "<2-4 sentences: the approach you are taking and why>",
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


def plan_prompt(
    *,
    context: str,
    user_choices: str,
    target_repo: str,
    supervisor_comments: str = "",
) -> str:
    replan = REPLAN_NOTE.format(comments=supervisor_comments) if supervisor_comments else ""
    return f"""{PLAN_BRIEF}

## Repository
{target_repo}

## Requirements (context.md)
{context}

## Explicit user choices (user_choices.md)

These came from the user directly. Where anything else conflicts with one of \
these, this wins.

{user_choices}
{replan}
"""
