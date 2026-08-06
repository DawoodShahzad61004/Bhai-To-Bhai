"""The supervisor: start both agents at once, wait for both, then check.

Both agents are submitted before either result is collected, which is what makes
them run at the same time -- a submit that were followed by its own result()
would serialise them, and two CLI turns back to back take twice as long for no
reason.

Threads rather than processes because each agent is a subprocess.run that spends
its whole life waiting on a pipe, which releases the GIL.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from logging_config import get_logger
from temp_agents import prompts
from temp_agents.agents import AGENT_A, AGENT_B, FILE_A, FILE_B, run_agent
from temp_agents.runners import Result, run_claude

logger = get_logger(__name__)


def _describe(result: Result) -> str:
    """What an agent said, or why it never got to say anything."""
    return result.text if result.ok else f"(failed: {result.error})"


def temp_supervisor() -> Result:
    """Run both agents concurrently, then report on what they actually wrote."""
    agents = (AGENT_A, AGENT_B)
    logger.info(
        "temp_supervisor starting %s",
        " and ".join(f"{a.name} on {a.backend}" for a in agents),
    )

    with ThreadPoolExecutor(max_workers=len(agents), thread_name_prefix="temp-agent") as pool:
        started = {agent.name: pool.submit(run_agent, agent) for agent in agents}
        results = {name: future.result() for name, future in started.items()}

    logger.info("temp_supervisor: both agents finished, checking the files")
    return run_claude(
        prompts.SUPERVISOR.format(
            file_a=FILE_A,
            file_b=FILE_B,
            report_a=_describe(results[AGENT_A.name]),
            report_b=_describe(results[AGENT_B.name]),
        ),
        # Read only: the agents own the files, the supervisor only confirms them.
        tools=("Read",),
        tag="temp_supervisor",
    )
