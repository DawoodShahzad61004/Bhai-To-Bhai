"""Claude Code counterparts of the supervisors and workers in teams/.

Every node here is interchangeable with the one it replaces: same node name,
same state in, same Command out, same report format. The graphs in teams/ are
unchanged -- config.USE_CLAUDE_CODE_AGENTS picks which implementation gets
wired into them, and config.CLAUDE_CODE_SEND_CUSTOM_TOOLS picks whether Claude
Code works with this project's tools (over MCP) or its own.

The nodes hold no logic of their own. They assemble the prompt their LangChain
counterpart already runs on, hand it to the CLI, log what came back, and file it
as a report.
"""

from claude_agents.agents import CLAUDE_AGENTS, make_claude_worker_node
from claude_agents.router import make_claude_supervisor_node

__all__ = [
    "CLAUDE_AGENTS",
    "make_claude_worker_node",
    "make_claude_supervisor_node",
]
