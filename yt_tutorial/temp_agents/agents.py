"""The two demo agents.

Each one has a name, a backend, a prompt and a file it is supposed to produce.
Which CLI runs it is config, not code, so the same agent can be Claude Code on
one run and Codex on the next.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import TEMP_AGENT_A_BACKEND, TEMP_AGENT_B_BACKEND, TEMP_AGENTS_DIR
from logging_config import get_logger
from temp_agents import prompts
from temp_agents.runners import Result, run

logger = get_logger(__name__)

FILE_A = TEMP_AGENTS_DIR / "file_A.txt"
FILE_B = TEMP_AGENTS_DIR / "file_B.txt"


@dataclass(frozen=True)
class TempAgent:
    name: str
    backend: str
    prompt: str
    target: Path


AGENT_A = TempAgent("agent_A", TEMP_AGENT_A_BACKEND, prompts.AGENT_A, FILE_A)
AGENT_B = TempAgent("agent_B", TEMP_AGENT_B_BACKEND, prompts.AGENT_B, FILE_B)


def run_agent(agent: TempAgent) -> Result:
    """Run one agent to completion. Never raises -- the supervisor waits on this."""
    logger.info("--> %s starting on %s", agent.name, agent.backend)
    result = run(
        agent.backend,
        agent.prompt.format(path=agent.target),
        # Claude Code needs the write tool named; Codex ignores this and works
        # from its sandbox mode instead.
        tools=("Write",),
        tag=agent.name,
    )
    if not result.ok:
        logger.error("<-- %s failed: %s", agent.name, result.error[:200])
    else:
        logger.info("<-- %s reported: %s", agent.name, result.text[:200])
    return result
