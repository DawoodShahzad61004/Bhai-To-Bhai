"""Agent 3 — wave orchestrator.

Drawing box 3, "Orchestrate", smaller model. Runs one wave of coding subagents
per invocation, each in its own git worktree. Re-entered by the reviewer's
rework edge.

Named `wave_orchestrator` rather than `orchestrator` because the package it lives
in is already called that, and two things called "the orchestrator" in one
codebase is a reliable way to be misread.
"""

from wave_orchestrator.dispatch import TaskOutcome, run_task, run_wave
from wave_orchestrator.node import wave_orchestrator_node

__all__ = ["TaskOutcome", "run_task", "run_wave", "wave_orchestrator_node"]
