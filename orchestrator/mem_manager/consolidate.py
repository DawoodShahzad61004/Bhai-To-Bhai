"""Maintenance stage: passive-decay refresh, then rerank-and-prune.

Two independent operations, deliberately kept separate: refresh_decay()
updates each memory's own importance from its own clock; rerank_and_prune()
only ever compares importances that are already current. Running decay
first is what makes the comparison meaningful.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from math import floor
from typing import Sequence

import config
from .importance import passive_decay
from .memory import DurableMemory

logger = logging.getLogger(__name__)


def refresh_decay(
    memories: Sequence[DurableMemory], *, now: datetime | None = None
) -> list[DurableMemory]:
    """Apply passive_decay to every memory's importance, each from its own
    created_at/last_accessed_at - a memory reinforced by a recent merge
    decays less than one that was never touched again."""
    return [
        replace(
            memory,
            importance=passive_decay(
                memory.importance,
                memory.created_at,
                now=now,
                last_accessed_at=memory.last_accessed_at,
            ),
        )
        for memory in memories
    ]


def rerank_and_prune(
    memories: Sequence[DurableMemory],
    *,
    prune_fraction: float | None = None,
) -> list[DurableMemory]:
    """Sort by importance descending (stable - ties keep their input order)
    and drop the bottom `prune_fraction` of the list (config.PRUNE_BOTTOM_PERCENT
    if not given - read live, not captured as a def-time default, so a
    config change takes effect without reloading this module).

    Pruning itself is gated and always skipped (memories only reranked,
    never dropped) when either check fails - both read live, same as
    prune_fraction above:
    - config.ENABLE_PRUNING is False.
    - the corpus's total content is under config.MIN_PRUNE_BUDGET characters:
      too small a corpus for "the bottom prune_fraction" to be a meaningful
      signal yet.
    """
    indexed = sorted(
        enumerate(memories), key=lambda pair: pair[1].importance, reverse=True
    )

    if not config.ENABLE_PRUNING:
        logger.info("[PRUNE] skipped: ENABLE_PRUNING is False")
        result = [memory for _, memory in indexed]
        _log_retained(result)
        return result

    total_chars = sum(len(memory.content) for memory in memories)
    if total_chars < config.MIN_PRUNE_BUDGET:
        logger.info(
            "[PRUNE] skipped: corpus is %d character(s), below MIN_PRUNE_BUDGET=%d",
            total_chars,
            config.MIN_PRUNE_BUDGET,
        )
        result = [memory for _, memory in indexed]
        _log_retained(result)
        return result

    if prune_fraction is None:
        prune_fraction = config.PRUNE_BOTTOM_PERCENT
    prune_count = floor(len(indexed) * prune_fraction)
    if prune_count <= 0:
        _log_retained([memory for _, memory in indexed])
        return [memory for _, memory in indexed]

    split = len(indexed) - prune_count
    retained_pairs = indexed[:split]
    pruned_pairs = indexed[split:]

    logger.info(
        "[PRUNE] pruning %d of %d memorie(s); original indices pruned: %s",
        prune_count,
        len(indexed),
        [original_index for original_index, _ in pruned_pairs],
    )
    for original_index, memory in pruned_pairs:
        logger.debug(
            "[PRUNE] pruned index=%d id=%s tag=%s importance=%.3f content=%r",
            original_index,
            memory.id,
            memory.tag,
            memory.importance,
            memory.content,
        )

    retained = [memory for _, memory in retained_pairs]
    _log_retained(retained)
    return retained


def _log_retained(memories: Sequence[DurableMemory]) -> None:
    for memory in memories:
        logger.debug(
            "[CONSOLIDATE] retained memory id=%s tag=%s importance=%.3f "
            "created_at=%s last_accessed_at=%s provenance=%s merged_from=%s content=%r",
            memory.id,
            memory.tag,
            memory.importance,
            memory.created_at.isoformat(),
            memory.last_accessed_at.isoformat(),
            memory.provenance,
            memory.merged_from,
            memory.content,
        )
