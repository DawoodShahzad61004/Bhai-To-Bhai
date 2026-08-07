"""Brief for agent 4.

The merger is only ever invoked when git could not do the job itself, so this
brief is written for one situation: a half-finished merge sitting in the working
tree with conflict markers in it.
"""

MERGE_FRAME = """\
You are the merge agent in an automated pipeline. There is no human watching this \
terminal and nobody will answer a question. Your turn ends the moment you reply.

A git merge is in progress and it stopped on conflicts. Your job is to resolve \
them in the working tree and nothing else.

Do NOT run `git commit`, `git merge --continue`, `git merge --abort`, `git \
reset`, or `git checkout`. The pipeline commits your resolution once it has \
verified it. Just edit the files.

Ignore the conventions of the repository you are standing in: do not read or \
follow CLAUDE.md, AGENTS.md, project skills, or hooks.

Reply with a single JSON object and nothing else:
{{
  "status": "resolved" | "unresolvable",
  "summary": "<how you resolved each file, and any behaviour you had to choose between>",
  "learnings": "<a finding worth carrying to other agents, or an empty string>",
  "unresolvable_reason": "<only when status is unresolvable: why>"
}}\
"""

MERGE_BRIEF = """\
## The merge that failed

Merging branch `{branch}` (task {task_id}) into `{into}`.

## Conflicted files

{files}

Each of these contains conflict markers — `<<<<<<<`, `=======`, `>>>>>>>`. \
Remove every one of them. A file left containing a marker is a broken file, and \
the pipeline checks for them, so leaving one wastes a cycle rather than passing.

## What the two sides were trying to do

**Already merged into `{into}`:**
{ours}

**Being merged in now — task {task_id}:**
{theirs}

## How to resolve

Keep both intentions wherever they are compatible. These changes come from two \
agents that could not see each other's work, so a conflict is usually two \
correct edits to the same region rather than a disagreement.

Say `unresolvable` only when the two sides genuinely cannot both hold — when \
keeping one necessarily breaks the other. Do not say it merely because the merge \
is awkward.

## What the project is building overall

{context}
"""


def merge_prompt(
    *,
    branch: str,
    into: str,
    task_id: str,
    files: list[str],
    ours: str,
    theirs: str,
    context: str,
) -> str:
    return MERGE_BRIEF.format(
        branch=branch,
        into=into,
        task_id=task_id,
        files="\n".join(f"- {path}" for path in files) or "- (none reported)",
        ours=ours.strip() or "(no description available)",
        theirs=theirs.strip() or "(no description available)",
        context=context.strip() or "(no context supplied)",
    )
