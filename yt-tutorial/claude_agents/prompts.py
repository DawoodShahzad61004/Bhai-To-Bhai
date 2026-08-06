"""What the Claude Code CLI is actually asked.

No role is rewritten here. Each agent hands Claude Code the same brief its
LangChain counterpart runs on, imported verbatim from the project's prompts.py,
so both implementations are answering the same question and a difference in
output is a difference in the model, not in the ask.

This module only supplies the framing around those briefs -- who is asking, what
a reply gets used for, and which tools the brief's tool names refer to in this
run -- plus the transcript rendering, because the CLI takes one prompt string
where the LangChain path takes a message list.
"""

from __future__ import annotations

# ── Framing ───────────────────────────────────────────────────────────────────
# Claude Code's default posture is a coding assistant talking to a person in a
# terminal: it opens with a plan, offers to continue, and asks when unsure. All
# three are wrong here -- the reply is consumed by a router, not read by anyone,
# and the turn ends when it is written.

_PIPELINE_FRAME = """You are being invoked by an automated multi-agent pipeline, not by a person at a terminal. Nobody will read your reply directly and nobody can answer a follow-up question: this turn ends the moment you reply, and your reply is the only thing that survives it.

Ignore the repository conventions you would normally pick up here -- CLAUDE.md, project skills, commit habits, documentation rules. They belong to the repository you happen to be standing in, not to this task."""

WORKER_FRAME = (
    _PIPELINE_FRAME
    + """

Take on the role set out under ROLE BRIEF below and do that job, once.

Your reply is handed verbatim to a routing supervisor as this worker's report,
so it must be the report and nothing else: no preamble, no plan, no summary of
what you are about to do, no offer to continue, no questions. Answer in the
report format the brief asks for.

Claim only what you actually did. The supervisor routes on your report, and a
worker that reports work it did not do stops the run with nothing to show.

{workspace_note}

{tools_note}

ROLE BRIEF
=================================================================
{role}
================================================================="""
)

ROUTER_FRAME = (
    _PIPELINE_FRAME
    + """

Take on the role set out under ROLE BRIEF below. You are routing, not working:
you do not answer the request, you do not use tools, and you do not have any.

Reply with the routing decision in the required JSON shape and nothing else.

ROLE BRIEF
=================================================================
{role}
================================================================="""
)

# ── Workspace ─────────────────────────────────────────────────────────────────
# The briefs say "the workspace directory" and leave it at that, because under
# LangChain the document tools resolve it and a worker never sees a path. Claude
# Code does see paths, and its own instructions tell it to put working files in a
# scratchpad directory -- which is where the first chart this pipeline produced
# went. Naming the directory, and saying plainly that it is the only one that
# counts, is what settles that disagreement.

WORKSPACE_NOTE = """WORKSPACE: {workspace}

Every file this run produces belongs in that directory, under the bare name the request asked for. It is where the pipeline looks afterwards, so a file written anywhere else does not exist as far as this run is concerned. Ignore any standing instruction to put working files in a scratchpad or temporary directory -- it does not apply to the deliverables here."""

# ── Tool notes ────────────────────────────────────────────────────────────────
# A brief names the tools its LangChain agent was given. Under
# CLAUDE_CODE_SEND_CUSTOM_TOOLS those same tools are attached over MCP, so the
# names still resolve -- only the prefix changes. Without it Claude Code has its
# own tools instead, and the brief's names refer to nothing, so each agent has
# to be told what stands in for them (agents.py owns that mapping).

CUSTOM_TOOLS_NOTE = """TOOLS: this project's own tools are attached over MCP and are the only tools you have. They appear as {tool_list} -- the same tools the brief names, so everything it says about them holds, including where they write and what they refuse. Claude Code's own tools are switched off; do not plan around Read, Write, Edit, Bash or WebSearch."""

BUILTIN_TOOLS_NOTE = """TOOLS: the tools the brief names do not exist in this run. You have Claude Code's own tools instead: {tool_list}. Your working directory is the shared workspace the brief calls its workspace, so refer to files there by bare name and write them there.

{mapping}"""

# ── User prompt ───────────────────────────────────────────────────────────────

WORKER_REQUEST = """{transcript}

=================================================================
You are the '{name}' worker. Do your job for this turn and reply with your report."""

ROUTER_REQUEST = """{transcript}

=================================================================
Name the one worker that should act next, or FINISH."""


# ── Transcript rendering ──────────────────────────────────────────────────────


def _text(message) -> str:
    """The text of a message, whatever shape LangChain handed it over in."""
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
    if isinstance(content, list):
        return "".join(
            str(block.get("text", block)) if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content or "")


def _label(message) -> str:
    """Who said it. Worker reports carry a name; the original request does not."""
    name = message.get("name") if isinstance(message, dict) else getattr(message, "name", None)
    if name:
        return name
    role = message.get("role") if isinstance(message, dict) else getattr(message, "type", "")
    return "user" if role in ("user", "human") else (role or "message")


def transcript(messages: list) -> str:
    """The conversation so far, as one block of text.

    The whole history goes in, not just the last message: a supervisor is asked
    to check which deliverables are already done, and a worker is asked to work
    from findings an earlier team reported. Both questions are unanswerable from
    the last message alone.
    """
    parts = []
    for message in messages:
        text = _text(message).strip()
        if text:
            parts.append(f"[{_label(message)}]\n{text}")
    return "\n\n".join(parts) or "[user]\n(no conversation yet)"
