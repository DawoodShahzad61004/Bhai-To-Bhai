"""Public entry point: compact a markdown file of raw episodic memory event
records into a pruned, deduped set of durable memories.

Pipeline: parse -> score -> dedupe/merge (LLM-assisted) -> passive decay ->
rerank + prune bottom N%. Dedup/merge runs before decay so a reinforced
memory's last_accessed_at reflects its most recent contributing entry;
pruning runs last, over the already-consolidated memories rather than raw
near-duplicate fragments. See docs/Architecture.md for the full rationale.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import config

from .consolidate import refresh_decay, rerank_and_prune
from .importance import parse_episodic_md
from .memory import DurableMemory, build_durable_memories
from .services.dedup_merge import LLMCall, dedupe_and_merge
from logging_config import setup_logging

logger = logging.getLogger(__name__)


def compact_markdown(
    text: str,
    *,
    now: datetime | None = None,
    embedder=None,
    llm_call: LLMCall | None = None,
    judge_call: LLMCall | None = None,
    use_llm: bool = True,
) -> list[DurableMemory]:
    logger.info("[PARSE] input: %d char(s) of raw markdown", len(text))
    records = parse_episodic_md(text)
    logger.info("[PARSE] output: %d episodic record(s)", len(records))

    logger.info("[SCORE] input: %d episodic record(s)", len(records))
    memories = build_durable_memories(records, now=now)
    logger.info(
        "[SCORE] output: %d durable memory(ies), importance range [%.3f, %.3f]",
        len(memories),
        min((m.importance for m in memories), default=0.0),
        max((m.importance for m in memories), default=0.0),
    )

    logger.info(
        "[DEDUP_MERGE] input: %d durable memory(ies), use_llm=%s", len(memories), use_llm
    )
    memories = dedupe_and_merge(
        memories,
        embedder=embedder,
        llm_call=llm_call,
        judge_call=judge_call,
        use_llm=use_llm,
    )
    logger.info("[DEDUP_MERGE] output: %d durable memory(ies)", len(memories))

    logger.info("[CONSOLIDATE] input: %d durable memory(ies)", len(memories))
    memories = refresh_decay(memories, now=now)
    logger.info("[CONSOLIDATE] output: %d durable memory(ies) after passive decay", len(memories))

    logger.info("[PRUNE] input: %d durable memory(ies)", len(memories))
    memories = rerank_and_prune(memories)
    logger.info("[PRUNE] output: %d durable memory(ies) retained", len(memories))

    return memories


def compact_markdown_file(path: str | Path, **kwargs) -> list[DurableMemory]:
    path = Path(path)
    # utf-8-sig strips a leading BOM when present and reads plain UTF-8
    # identically when it's not - a BOM left in by plain "utf-8" attaches to
    # the first header line and fails header validation, silently dropping
    # the file's first record.
    text = path.read_text(encoding="utf-8-sig")
    memories = compact_markdown(text, **kwargs)
    prefix = config.DURABLE_MEMORY_TAG_PREFIXES.get(path.name.lower())
    if not prefix:
        return memories

    chronological = sorted(
        enumerate(memories), key=lambda item: (item[1].created_at, item[0])
    )
    tags = {
        original_index: f"{prefix} {number}"
        for number, (original_index, _) in enumerate(chronological, start=1)
    }
    return [replace(memory, tag=tags[index]) for index, memory in enumerate(memories)]


def _main(argv: list[str]) -> int:
    setup_logging()
    if len(argv) != 2:
        print("usage: python -m mem_manage.compact <episodic-log.md>", file=sys.stderr)
        return 2
    text = Path(argv[1]).read_text(encoding="utf-8")
    record_count = len(parse_episodic_md(text))
    memories = compact_markdown(text)
    logger.debug("%d episodic record(s) -> %d durable memory(ies)", record_count, len(memories))
    print(f"{record_count} episodic record(s) -> {len(memories)} durable memory(ies)")
    logger.info("[OUTPUT] input: %d durable memory(ies), ranked by importance", len(memories))
    for memory in memories:
        merged_note = (
            f" (merged from {len(memory.merged_from)})" if len(memory.merged_from) > 1 else ""
        )
        logger.debug(
            "[%.3f] %s%s id=%s created_at=%s last_accessed_at=%s provenance=%s "
            "merged_from=%s content=%r",
            memory.importance,
            memory.tag,
            merged_note,
            memory.id,
            memory.created_at.isoformat(),
            memory.last_accessed_at.isoformat(),
            memory.provenance,
            memory.merged_from,
            memory.content,
        )
        print(f"  [{memory.importance:.3f}] {memory.tag}{merged_note}: {memory.content[:80]!r}")
    logger.info("[OUTPUT] output: %d entries formatted as markdown", len(memories))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
