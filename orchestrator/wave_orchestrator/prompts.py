"""Briefs for agent 3 and for the coding subagents it dispatches.

The subagent brief is the one that matters. It is the only brief in this pipeline
handed to an agent with write access to a real repository, and every clause in it
is load-bearing.
"""

CODING_FRAME = """\
You are a coding agent inside an automated pipeline. There is no human watching \
this terminal and nobody will answer a question. Your turn ends the moment you \
reply.

You are working in a git worktree that belongs to you alone. Other agents are \
working on other tasks in their own worktrees at the same time, and you cannot \
see their work — it is merged in afterwards by a separate agent. Do not try to \
account for what they might be doing. Do not touch files outside your task.

Do NOT run `git commit`, `git merge`, `git rebase`, `git checkout`, or `git \
branch`. The pipeline handles all version control. Leave your changes in the \
working tree.

Ignore the conventions of the repository you are standing in unless this brief \
tells you otherwise: do not read or follow CLAUDE.md, AGENTS.md, project skills, \
or hooks, and do not start work beyond your task.

Windows shell safety rules for this pipeline:
- For paths with spaces, use PowerShell `-LiteralPath` or quote the complete   path; do not pass a split path such as `Marker-PDF Report.md` as two arguments.
- If a patch/write helper cannot create a file after two attempts, switch to a   native PowerShell write. In Windows PowerShell 5.1, use `-Encoding UTF8` or   `[System.IO.File]::WriteAllText(..., [System.Text.UTF8Encoding]::new($false))`;   do not use `utf8NoBOM`, which only exists in newer PowerShell.

If your file-write tool is rejected as an unsupported or unavailable call, that \
is a fixed fact about this session, not a naming mistake — do not retry it under \
a guessed alternate name (`applypatch`, `patch`, and similar are not real \
fallbacks). Try at most one different mechanism for the same edit; if that is \
also rejected (for example "blocked by policy"), stop immediately and reply with \
status="blocked" and blocked_reason naming exactly what was rejected. Spending \
your whole turn retrying a call that has already been refused produces no work \
and no usable diagnosis — an honest, fast "blocked" is strictly more useful than \
that.

Never send a message that contains only narration. Because your turn ends the \
moment you reply, a message like "Now I need to update X" with no tool call \
attached IS your reply — the turn ends there whether or not the work described \
actually happened. Any explanation of what you are about to do must be included \
in the same message as the tool call that does it. The only text-only message \
you ever send is the final JSON object below, once every change is already made.

When you are done, reply with a single JSON object and nothing else:
{
  "status": "done" | "blocked",
  "summary": "<what you actually changed, file by file>",
  "files_changed": ["<path>"],
  "blocked_reason": "<only when status is blocked: what stopped you>",
  "finish_reason": "stop" | "length"
}

"finish_reason" is "stop" if you reached this JSON naturally, having finished \
everything you set out to do. It is "length" if you are being forced to wrap up \
early because you are almost out of room to respond — in that case, prefer \
setting "status" to "blocked" and using "blocked_reason" to say what was left, \
rather than claiming "done" for work you did not finish.

Report "done" only for work you actually performed. If you could not complete the \
task, say "blocked" and why. A claim of completion for something you did not do \
is worse than an honest failure — the pipeline verifies against the filesystem, \
so an inaccurate report is caught and wastes a full review cycle.\
"""

TASK_BRIEF = """\
## Your task: {task_id} — {title}

{description}

### Files you are expected to create or change
{files}

### How this task will be judged done
{acceptance}

## Working directory

{worktree}

That is an absolute path and it is your worktree's root. Every file you create or \
edit belongs under it. Do not write into a temporary directory, a scratchpad, or \
anywhere your own instructions might otherwise suggest — this path wins over any \
other location you have been told about.

## What the pipeline is building overall

The full requirements this run is working from are written to `{context_path}` \
— read that file for the context behind your task.
"""

LEARNINGS_SECTION = """\

## Shared findings from this run

Other coding agents are working on other tasks in parallel, in their own \
worktrees right now — this is the one file all of you share. Read \
`{learnings_path}` at any time; reading never waits on anything.

Record a finding the moment you find it, not at the end, from a shell:

    "{python_exe}" "{script_path}" append-learning "{artifacts_dir}" "{task_id}" "<your finding, one paragraph>"

For install/build/test commands, use this instead of calling them directly \
— same args, `run-shared` for `append-learning`, then `--` and the command. \
On failure it auto-records your symptom and prints any peer finding for the \
same failure below the command's output:

    "{python_exe}" "{script_path}" run-shared "{artifacts_dir}" "{task_id}" -- <command and args>

For any other failure, re-read the file first — a peer may already have the \
answer — and if it's one others could hit too (not a bug in your own code), \
record the raw symptom before you diagnose it.

Safe to run any time, even simultaneously — writes are queued. Only record \
what's genuinely worth a peer's attention; do not narrate routine progress.
"""

REWORK_SECTION = """\

## This is a rework. Your previous attempt was rejected.

You did this task before, in a session that no longer exists — your worktree \
has been reset to a clean state, so the changes you made last time are gone and \
you are starting fresh, from the same base, with no memory of that attempt \
carried over automatically. The two sections below stand in for that memory.

### What you reported doing last time

{previous_summary}

### What the reviewer said was wrong

{comments}

Address what the reviewer identified. Do not simply reproduce your previous \
attempt.
"""


def coding_prompt(
    *,
    task: dict,
    worktree: str,
    context_path: str,
    learnings_path: str,
    artifacts_dir: str,
    python_exe: str,
    script_path: str,
    rework_comments: str = "",
    previous_summary: str = "",
) -> str:
    files = task.get("files") or []
    files_block = "\n".join(f"- {path}" for path in files) or "- (use your judgement)"
    prompt = TASK_BRIEF.format(
        task_id=task["task_id"],
        title=task.get("title") or task["task_id"],
        description=task["description"],
        files=files_block,
        acceptance=task.get("acceptance") or "(no explicit acceptance criteria given)",
        worktree=worktree,
        context_path=context_path,
    )
    prompt += LEARNINGS_SECTION.format(
        learnings_path=learnings_path,
        python_exe=python_exe,
        script_path=script_path,
        artifacts_dir=artifacts_dir,
        task_id=task["task_id"],
    )
    if rework_comments.strip():
        prompt += REWORK_SECTION.format(
            comments=rework_comments.strip(),
            previous_summary=previous_summary.strip() or "(no summary was recorded)",
        )
    return prompt
