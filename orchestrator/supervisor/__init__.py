"""Agent 6 — supervisor.

Drawing box 6, "Supervisor", larger model. Runs once when every wave has merged,
outside the inner loop; may send the whole thing back to the planner.

Switched off with ENABLE_SUPERVISOR in config.py.
"""

from supervisor.node import supervisor_node

__all__ = ["supervisor_node"]
