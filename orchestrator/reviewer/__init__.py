"""Agent 5 — reviewer.

Drawing box 5, "Review", larger model. Runs once per wave against the merged
result; may send the wave back to the orchestrator for rework.

Switched off with ENABLE_REVIEWER in config.py.
"""

from reviewer.node import reviewer_node

__all__ = ["reviewer_node"]
