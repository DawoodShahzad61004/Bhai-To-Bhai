"""mem_manage - compacts raw episodic memory event records into durable
learnings: importance scoring, LLM-assisted dedup/merge, passive decay, and
bottom-N% pruning. See docs/Architecture.md for the design this implements.
"""
from .compact import compact_markdown, compact_markdown_file
from .importance import EpisodicRecord, composite_importance, passive_decay
from .memory import DurableMemory

__all__ = [
    "compact_markdown",
    "compact_markdown_file",
    "DurableMemory",
    "EpisodicRecord",
    "composite_importance",
    "passive_decay",
]
