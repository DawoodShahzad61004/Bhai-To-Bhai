"""Briefs for agent 1.

Kept out of config.py deliberately: a prompt is rewritten when an agent
misbehaves, a constant is retuned when a limit binds, and coupling them means
every wording tweak touches the file that also controls termination (ADR-012).

Three framing rules appear in every brief in this pipeline, and each is a defect
from docs/Research.md topic 17 wearing a fix:

  * Say this is an automated pipeline, not a person. Otherwise the CLI opens a
    conversation and waits.
  * Say what to IGNORE as explicitly as what to do. Both CLIs auto-discover
    CLAUDE.md, AGENTS.md, project skills and hooks from wherever they are
    standing, and act on them — one Codex run spent 40 seconds searching a
    knowledge base before touching a one-line file.
  * Be concrete enough to win. In every observed conflict the specific beat the
    general: an absolute path beat "the workspace directory", a file named in the
    user's request beat a prompt that merely forbade it.
"""

PIPELINE_FRAME = """\
You are one stage of an automated multi-agent pipeline. There is no human \
watching this terminal and nobody will answer a follow-up question. Your turn \
ends the moment you reply.

Ignore the conventions of the repository you are standing in. Do not read or \
follow CLAUDE.md, AGENTS.md, project skills, hooks, or commit conventions. Do \
not start work beyond what this brief asks for. Do not ask for permission.

Reply with a single JSON object and nothing else. No preamble, no explanation \
around it, no markdown fence.\
"""

# ── Phase 1: survey the project and decide what genuinely needs asking ────────
SURVEY_BRIEF = """\
You are the requirements-gathering agent.

Your job is to turn a one-line goal into a requirements document, by reading the \
project you have been pointed at and identifying what you cannot determine from \
it.

Do this:
1. Inspect the repository at the working directory you were started in. Read its \
   README, its package manifests, and enough source to understand its shape.
2. Search accumulated project knowledge if it is available to you.
3. Draft the requirements the goal implies.
4. List ONLY the questions whose answers you genuinely cannot infer and which \
   would change what gets built. A material decision is one where two reasonable \
   answers lead to different code.

Ask nothing you can answer yourself by reading. Ask nothing cosmetic. Ask at \
most {max_questions} questions. An empty list is a good answer when the goal is \
already unambiguous.

Return this JSON object:
{{
  "understanding": "<2-4 sentences: what this project is and what the goal means in it>",
  "requirements": "<markdown: the functional and technical requirements the goal implies>",
  "constraints": "<markdown: what the existing codebase forces on any solution>",
  "explicit_choices": ["<a decision the USER already stated in the goal, verbatim in substance>"],
  "questions": ["<a material question you cannot answer by reading>"]
}}

"explicit_choices" is a strict field. Record ONLY decisions the user actually \
stated in the goal below. Do not record your own assumptions, your inferences \
from the code, unanswered questions, or requirements you derived. If the user \
said "must support English and Urdu", that belongs there. If you concluded "they \
probably want PostgreSQL", it does not.\
"""

# ── Phase 2: fold the user's answers into the final document ─────────────────
FINALISE_BRIEF = """\
You are the requirements-gathering agent, completing your work.

You previously surveyed the project and asked the user a set of questions. Their \
answers are below. Produce the final requirements document.

Fold the answers into the requirements rather than appending them. Where an \
answer contradicts your earlier draft, the answer wins — it came from the user \
and your draft did not.

Return this JSON object:
{{
  "understanding": "<2-4 sentences>",
  "requirements": "<markdown: the complete, final requirements>",
  "constraints": "<markdown: what the codebase forces on any solution>",
  "open_risks": ["<anything still unresolved that the planner should know about>"]
}}

Do not restate the questions and answers. The pipeline records those itself, \
verbatim, and your paraphrase of them would be a second and less reliable copy.\
"""


def survey_prompt(
    *,
    goal: str,
    target_repo: str,
    context_path: str,
    user_choices_path: str,
    learnings_path: str,
    max_questions: int,
) -> str:
    return f"""{SURVEY_BRIEF.format(max_questions=max_questions)}

## Repository
{target_repo}

## Durable project memory

Before drafting, read these files when they exist:

- Current synthesized context: `{context_path}`
- Append-only user decision ledger: `{user_choices_path}`
- Accumulated agent findings: `{learnings_path}`

The user's recorded choices are authoritative. Treat learnings as leads and
validate them against the repository before relying on them.

## Goal, as the user stated it
{goal}
"""


def finalise_prompt(*, goal: str, target_repo: str, survey: str, qa: list[tuple[str, str]]) -> str:
    answered = "\n".join(f"- Q: {q}\n  A: {a}" for q, a in qa) or "(the user answered nothing)"
    return f"""{FINALISE_BRIEF}

## Repository
{target_repo}

## Goal, as the user stated it
{goal}

## Your earlier survey
{survey}

## The user's answers
{answered}
"""
