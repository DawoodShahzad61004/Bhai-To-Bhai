"""The three prompts this sandbox uses.

Nothing here is shared with the rest of the project. They are deliberately tiny,
so that anything a run gets wrong is the CLI's doing and not the prompt's.

Two things are in every prompt for a reason. Paths are absolute, because both
CLIs carry their own instructions about where working files belong and an
absolute path is the one thing neither can read as an invitation to choose. And
each prompt says to ignore repository conventions, because both CLIs pick up
CLAUDE.md and AGENTS.md from wherever they are standing -- the first codex run
here spent forty seconds searching a knowledge base before touching the file.

The byte-order mark clause is there for the same kind of reason: codex writes
files on Windows through PowerShell, which prefixes UTF-8 output with a BOM, so
"Hi World!" arrived on disk as "﻿Hi World!" until it was asked not to.
"""

AGENT_A = """Write exactly this text into the file {path}, and nothing else -- plain UTF-8, no byte-order mark:

Hello World!

Then reply with one short line saying whether you wrote it.
Do only this. Ignore any repository conventions, project documentation or knowledge-base steps you would normally follow."""

AGENT_B = """Write exactly this text into the file {path}, and nothing else -- plain UTF-8, no byte-order mark:

Hi World!

Then reply with one short line saying whether you wrote it.
Do only this. Ignore any repository conventions, project documentation or knowledge-base steps you would normally follow."""

SUPERVISOR = """Two agents just ran and reported back.

Agent A was asked to write "Hello World!" into {file_a}. It said: {report_a}
Agent B was asked to write "Hi World!" into {file_b}. It said: {report_b}

Read both files. Reply with exactly two lines, one per agent, each saying OK or
FAILED and why. Do not write or change anything.
Do only this. Ignore any repository conventions, project documentation or knowledge-base steps you would normally follow."""
