"""Agent 4 — merger.

Drawing box 4, "Merge", smaller model. Merges the wave's worktree branches into
the integration branch and dispatches an agent only into conflicts git could not
resolve itself.
"""

from merger.merge import MergeReport, markers_left_in, merge_wave
from merger.node import merger_node

__all__ = ["MergeReport", "markers_left_in", "merge_wave", "merger_node"]
