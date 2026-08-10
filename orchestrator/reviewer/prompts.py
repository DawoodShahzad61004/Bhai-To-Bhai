"""Brief for agent 5.

Larger model: this box decides whether an implementation is acceptable.

The brief spends most of its length on one thing — telling the reviewer to look
at the repository rather than at the agents' reports. Every task below arrives
with a claim attached, and a claim is the least reliable evidence available.
docs/Bugs.md #21 is a report that was fluent, specific, consistent with the
request, and describing a file that did not exist.
"""

REVIEW_FRAME = """\
You are the review agent in an automated pipeline. There is no human watching \
this terminal and nobody will answer a question. Your turn ends the moment you \
reply.

You are reviewing, not fixing. Do NOT edit, create, or delete any file, and do \
not run any git command that changes the repository. If something is wrong, say \
so — a coding agent will address it.

Ignore the conventions of the repository you are standing in: do not read or \
follow CLAUDE.md, AGENTS.md, project skills, or hooks.

Reply with a single JSON object and nothing else:
{{
  "verdict": "approved" | "rework",
  "assessment": "<what you checked and what you found, per task>",
  "problems": ["<a specific, actionable defect, naming the file>"],
  "rework_instructions": "<only when the verdict is rework: what the coding agents must change, concretely>",
  "learnings": "<a finding worth carrying to other agents, or an empty string>"
}}\
"""

REVIEW_BRIEF = """\
## What you are reviewing

Wave {wave} of an implementation plan has been completed by {count} coding \
agent(s) working in isolation, and their branches have been merged into \
`{branch}`, which is checked out in your working directory.

## Read the code, not the reports

Below you will find what each agent said it did, and what git observed change. \
Treat both as claims to be verified. Open the files. An agent's summary is the \
least reliable evidence in this brief, and a report describing work that did not \
happen is a failure mode this pipeline has seen — check that what was described \
is actually there.

## The tasks this wave was supposed to complete

{tasks}

## What the agents reported, and what git saw

{evidence}

## The requirements this is all in service of

The full requirements this run is working from are written to `{context_path}` \
— read that file before judging whether this wave is consistent with them.

## How to decide

Approve when the wave's tasks are implemented in a way that works and is \
consistent with the requirements. Minor style differences are not grounds for \
rework.

Send it back for rework when something is actually wrong: a task not implemented, \
implemented incorrectly, obviously broken, contradicting the requirements, or \
contradicting an explicit user choice.

Scope your judgement to THIS wave. Later waves have not run yet, so work they \
are responsible for being absent is not a defect — do not send a wave back for \
failing to do something that was never its job.

Be specific. "The error handling could be better" cannot be acted on. "`health.py` \
returns 200 when the database ping raises, at line 24" can.\
"""


def review_prompt(
    *,
    wave: int,
    branch: str,
    tasks: list[dict],
    evidence: list[str],
    context_path: str,
) -> str:
    task_blocks = []
    for task in tasks:
        acceptance = task.get("acceptance") or "(none given)"
        task_blocks.append(
            f"### {task['task_id']} — {task.get('title') or ''}\n"
            f"{task.get('description', '')}\n\n"
            f"Acceptance: {acceptance}"
        )
    return REVIEW_BRIEF.format(
        wave=wave,
        branch=branch,
        count=len(tasks),
        tasks="\n\n".join(task_blocks) or "(no task records available)",
        evidence="\n".join(f"- {line}" for line in evidence) or "- (nothing reported)",
        context_path=context_path,
    )
