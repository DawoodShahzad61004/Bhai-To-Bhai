"""Runs one scenario end to end and returns the row the report is built from.

Every run goes through `compact_markdown_file` on a real temporary file named
as the manifest declares, because `compact_markdown_file` is where two
behaviours live that `compact_markdown` does not have: it reads utf-8-sig
(without which a BOM silently costs the file's first record) and it renumbers
tags for learnings.md / user_choices.md.
"""
from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

import compact_command as command
import config
from mem_manager.compact import compact_markdown_file
from mem_manager.importance import parse_episodic_md
from mem_manager.memory import build_durable_memories

import quality_keywords as qk
import quality_metrics as qm
from quality_embedders import resolve_embedder

# The frozen instant the whole suite scores against, matching conftest's `now`
# fixture. Recency, decay and therefore the prune ordering are all functions
# of it, so pinning it is what makes an "exact pruned records" assertion
# possible at all.
QUALITY_NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


@contextmanager
def config_overrides(**values) -> Iterator[None]:
    """Apply a scenario's config, restore afterwards.

    This mutates module globals on `config`, which the pipeline reads live.
    That is safe here and only here: this repository has no pytest-xdist and
    no threaded tests. If either is ever added, this needs to become a lock or
    a per-process fixture - writing the constraint down now, while it is still
    true, is cheaper than rediscovering it from a flaky failure.
    """
    previous = {key: getattr(config, key) for key in values}
    try:
        for key, value in values.items():
            setattr(config, key, value)
        yield
    finally:
        for key, value in previous.items():
            setattr(config, key, value)


def _source_tag_by_id(text: str) -> dict[str, str]:
    """Map each source event's durable id to the tag it was written under, so
    a dropped memory can be reported as the source records it represented."""
    records = parse_episodic_md(text)
    return {
        memory.id: record.tag
        for memory, record in zip(build_durable_memories(records, now=QUALITY_NOW), records)
    }


def _tags_behind(memories, source_tags: dict[str, str]) -> set[str]:
    """The DISTINCT source tags a set of memories came from - used to report
    which records a prune dropped."""
    return {
        source_tags[source_id]
        for memory in memories
        for source_id in memory.merged_from
        if source_id in source_tags
    }


def _group_of(memory, source_tags: dict[str, str]) -> list[str]:
    """One memory's source tags WITH MULTIPLICITY.

    Multiplicity is the whole point: three verbatim duplicates are written
    under one tag, so a set would report a group of three as size one and hide
    the merging this metric exists to observe.
    """
    return sorted(
        source_tags[source_id]
        for source_id in memory.merged_from
        if source_id in source_tags
    )


def _compact(text: str, as_filename: str, *, embedder, use_llm: bool, llm_calls):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / as_filename
        path.write_text(text, encoding="utf-8")
        merge_call, judge_call = llm_calls if llm_calls else (None, None)
        return compact_markdown_file(
            path,
            now=QUALITY_NOW,
            embedder=embedder,
            use_llm=use_llm,
            llm_call=merge_call,
            judge_call=judge_call,
        )


def run_scenario(
    scenario: dict,
    *,
    embedder_name: str,
    counter,
    llm_calls=None,
    use_llm: bool | None = None,
) -> dict:
    text = qk.read_corpus(scenario["file"])
    as_filename = scenario["as_filename"]
    overrides = dict(scenario["config"])
    resolved_use_llm = bool(llm_calls) if use_llm is None else use_llm

    source_tags = _source_tag_by_id(text)

    with config_overrides(**overrides):
        memories = _compact(
            text,
            as_filename,
            embedder=resolve_embedder(embedder_name),
            use_llm=resolved_use_llm,
            llm_calls=llm_calls,
        )
    # A second pass with pruning off, so "was anything pruned, and what"
    # is observed rather than inferred from the config. The gate is
    # sum(len(m.content)) >= MIN_PRUNE_BUDGET on POST-MERGE content
    # characters, which is the likeliest way a pruning scenario quietly
    # does nothing at all.
    with config_overrides(**{**overrides, "ENABLE_PRUNING": False}):
        unpruned = _compact(
            text,
            as_filename,
            embedder=resolve_embedder(embedder_name),
            use_llm=resolved_use_llm,
            llm_calls=llm_calls,
        )

    retained_ids = {memory.id for memory in memories}
    dropped = [memory for memory in unpruned if memory.id not in retained_ids]
    pruned_source_tags = sorted(_tags_behind(dropped, source_tags))

    output_text = command._memories_to_markdown(memories)
    output_content = "\n".join(memory.content for memory in memories)

    parsed_sentences = qk.sentences_for_markdown(text, denominator="parsed")
    raw_sentences = qk.sentences_for_markdown(text, denominator="raw")
    headline = scenario.get("denominator", "parsed")
    sentences = parsed_sentences if headline == "parsed" else raw_sentences

    keywords = qm.score_keywords(sentences, output_content, source_text=text)
    tokens = qm.score_tokens(text, output_text, output_content, counter)
    collateral, orphaned = qm.prune_collateral(sentences, pruned_source_tags)

    return {
        "id": scenario["id"],
        "file": scenario["file"],
        "hypothesis": scenario["hypothesis"],
        "embedder": embedder_name,
        "denominator": headline,
        "records_in": len(parse_episodic_md(text)),
        "records_out": len(memories),
        "records_after_merge": len(unpruned),
        "pruning_applied": len(dropped) > 0,
        "pruned_source_tags": pruned_source_tags,
        "prune_collateral": collateral,
        "orphaned_keywords": list(orphaned),
        "groups_formed": sorted(
            (_group_of(memory, source_tags) for memory in unpruned),
            key=lambda tags: (-len(tags), tags),
        ),
        "keywords": keywords,
        "tokens": tokens,
        "parsed_keyword_total": len(
            {keyword for s in parsed_sentences for keyword in s.keywords}
        ),
        "raw_keyword_total": len(
            {keyword for s in raw_sentences for keyword in s.keywords}
        ),
    }


def run_generations(
    scenario: dict, *, embedder_name: str, counter, generations: int
) -> list[dict]:
    """Compact repeatedly, each generation's output feeding the next.

    Recall is measured against GENERATION ZERO every time, not against the
    previous generation: erosion that loses two percent per pass looks
    harmless compared with its predecessor and ruinous compared with the
    source, and the source is the comparison that matters.
    """
    original = qk.read_corpus(scenario["file"])
    source = qk.sentences_for_markdown(
        original, denominator=scenario.get("denominator", "parsed")
    )
    overrides = dict(scenario["config"])
    rows: list[dict] = []
    text = original

    for generation in range(generations):
        with config_overrides(**overrides):
            memories = _compact(
                text,
                scenario["as_filename"],
                embedder=resolve_embedder(embedder_name),
                use_llm=False,
                llm_calls=None,
            )
        output_text = command._memories_to_markdown(memories)
        output_content = "\n".join(memory.content for memory in memories)
        rows.append(
            {
                "generation": generation,
                "records_out": len(memories),
                "text": output_text,
                "keywords": qm.score_keywords(
                    source, output_content, source_text=original
                ),
                "tokens": qm.score_tokens(original, output_text, output_content, counter),
            }
        )
        text = output_text
    return rows


def expectation_failures(row: dict, scenario: dict) -> list[str]:
    """Check a row against the manifest's declared floors.

    Floors are declared on BOTH quality and savings for every scenario. That
    pairing is the anti-gaming rule made structural: doing nothing scores
    perfect recall, deleting everything scores perfect savings, and neither
    can satisfy both.
    """
    expect = dict(scenario["expect"])
    expect.update(scenario.get("expect_by_embedder", {}).get(row["embedder"], {}))
    keywords, tokens = row["keywords"], row["tokens"]
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    if "type_recall_min" in expect:
        check(
            keywords.type_recall >= expect["type_recall_min"],
            f"type_recall {keywords.type_recall:.4f} < {expect['type_recall_min']}",
        )
    if "rare_recall_min" in expect:
        check(
            keywords.rare_recall is not None
            and keywords.rare_recall >= expect["rare_recall_min"],
            f"rare_recall {keywords.rare_recall} < {expect['rare_recall_min']}",
        )
    if "injected_max" in expect:
        check(
            len(keywords.injected) <= expect["injected_max"],
            f"injected {list(keywords.injected)} exceeds {expect['injected_max']}",
        )
    if "records_out" in expect:
        check(
            row["records_out"] == expect["records_out"],
            f"records_out {row['records_out']} != {expect['records_out']}",
        )
    if "tokens_freed_pct_min" in expect:
        check(
            tokens.freed_pct >= expect["tokens_freed_pct_min"],
            f"tokens_freed_pct {tokens.freed_pct:.4f} < {expect['tokens_freed_pct_min']}",
        )
    if "tokens_freed_max" in expect:
        check(
            tokens.freed <= expect["tokens_freed_max"],
            f"tokens_freed {tokens.freed} > {expect['tokens_freed_max']}",
        )
    return failures


def merge_yield(with_merging: dict, without_merging: dict) -> dict:
    """What near-duplicate merging actually bought, isolated: the same corpus
    scored under an embedder that groups paraphrases and one that does not."""
    return {
        "tokens_freed_delta": with_merging["tokens"].freed - without_merging["tokens"].freed,
        "type_recall_delta": (
            with_merging["keywords"].type_recall - without_merging["keywords"].type_recall
        ),
        "records_out_delta": with_merging["records_out"] - without_merging["records_out"],
    }
