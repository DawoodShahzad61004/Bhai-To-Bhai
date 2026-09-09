"""DurableMemory - the record shape a raw EpisodicRecord compacts into.

One durable memory can be traced back to one or more source episodic
records (see `merged_from`) once dedup/merge folds near-duplicates
together. Everything here is plain data plus the one conversion function;
scoring itself stays in importance.py so this module doesn't duplicate it.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from .importance import EpisodicRecord, build_session_index, composite_importance


@dataclass(frozen=True)
class DurableMemory:
    id: str
    content: str
    tag: str
    created_at: datetime
    last_accessed_at: datetime
    importance: float
    provenance: str
    # The session that wrote the source record, carried through so a /compact
    # rewrite can re-emit it. Without this field the round trip through
    # compact_command._memories_to_markdown destroys every session identity in
    # the file - that function rebuilds the markdown from these fields alone.
    # After a merge this names the keeper's session only, so it and
    # `merged_from` legitimately disagree for LLM-merged content.
    session: str = ""
    # ids of every source EpisodicRecord folded into this memory. A single,
    # never-merged record still carries its own id here - this is what lets
    # a conflicting-but-distinct memory be told apart from a genuine merge.
    merged_from: list[str] = field(default_factory=list)


def content_id(text: str) -> str:
    """Stable, content-derived id (not random) so the same input always
    produces the same id - merge grouping, re-runs, and tests can rely on
    it."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _record_id(record: EpisodicRecord) -> str:
    # Keyed off the full original block (header + body), not just the body,
    # so two records with identical text but different timestamps still get
    # distinct ids.
    basis = record.raw or f"{record.timestamp.isoformat()}|{record.tag}|{record.content}"
    return content_id(basis)


def build_durable_memories(
    records: Sequence[EpisodicRecord], *, now: datetime | None = None
) -> list[DurableMemory]:
    """One DurableMemory per EpisodicRecord, each scored by composite_importance
    over the *whole* input corpus (so frequency/surprise/session-salience see
    every record, not just the ones processed so far)."""
    session_index = build_session_index(records)
    memories: list[DurableMemory] = []
    for record in records:
        importance = composite_importance(record, records, session_index, now=now)
        record_id = _record_id(record)
        memories.append(
            DurableMemory(
                id=record_id,
                content=record.content,
                tag=record.tag,
                created_at=record.timestamp,
                last_accessed_at=record.timestamp,
                importance=importance,
                provenance=record.provenance,
                session=record.session,
                merged_from=[record_id],
            )
        )
    return memories
