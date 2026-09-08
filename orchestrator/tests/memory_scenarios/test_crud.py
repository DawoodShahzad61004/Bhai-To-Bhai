"""Creation, reading, immutable updates, scoring, and deletion boundaries.

Contracts: attached Architecture principles 1/4/6; ADR-028/029/031.
Public CRUD here is Markdown ingestion and compaction, not a database API.
"""

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
import math

import pytest

import config
from mem_manager import compact_markdown, compact_markdown_file
from mem_manager.consolidate import refresh_decay, rerank_and_prune
from mem_manager.importance import (
    EpisodicRecord, build_entity_index, composite_importance, extract_entities,
    f_entity_salience, f_frequency, f_outcome, f_recency, f_surprise,
    parse_episodic_md, passive_decay,
)
from mem_manager.memory import build_durable_memories, content_id


@pytest.mark.parametrize("choices", [False, True], ids=["learning", "user-choice"])
@pytest.mark.parametrize("content", ["plain text", "اردو — 日本語 🧠", "`foo.py` and T-042",
                                     "first\n\n### Detail\n\n- second", "", "a" * 8192])
def test_create_read_record_content_and_utc(block, now, choices, content):
    records = parse_episodic_md(block(content, tag="run-42", choices=choices))
    assert len(records) == 1
    assert (records[0].content, records[0].tag, records[0].timestamp) == (content, "run-42", now)
    assert records[0].raw.startswith("## ")


@pytest.mark.parametrize("separator", ["-", "—", "–", "|", "â€”"])
def test_legacy_learning_separators(separator):
    records = parse_episodic_md(f"## 2026-09-08 12:00:00Z {separator} multiple word tag\nbody")
    assert len(records) == 1
    assert records[0].tag == "multiple word tag"


@pytest.mark.parametrize("text", ["", " \n ", "# Title\nbody", "## malformed\nbody",
    "## 2026-02-30 12:00:00Z - tag\nbody", "## 2026-09-08 99:00:00Z - tag\nbody",
    "## Run `run` — 2026-13-01 12:00:00Z\nbody", "## 2026-09-08 12:00:00Z\nbody"])
def test_read_empty_or_malformed_input_does_not_invent_records(text, now, embedder):
    assert compact_markdown(text, now=now, embedder=embedder, use_llm=False) == []
    assert embedder.batches == []


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_read_mixed_valid_and_invalid_records(block, newline):
    text = "# Log\n\n" + block("one") + "\n## nonsense\nignored\n" + block("two")
    assert [r.content for r in parse_episodic_md(text.replace("\n", newline))] == ["one", "two"]


def test_build_preserves_explicit_provenance_and_source_identity(now):
    source = EpisodicRecord(now, "choice", "Use tabs", provenance="explicit")
    result = build_durable_memories([source], now=now)[0]
    assert result.provenance == "explicit"
    assert result.last_accessed_at == result.created_at == now
    assert result.merged_from == [result.id]
    assert build_durable_memories([source], now=now)[0] == result


@pytest.mark.parametrize("changed", ["timestamp", "tag", "content"])
def test_source_ids_distinguish_timestamped_events(now, changed):
    source = EpisodicRecord(now, "T-1", "Use pytest")
    update = {"timestamp": now + timedelta(seconds=1), "tag": "T-2", "content": "Use unittest"}
    other = replace(source, **{changed: update[changed]})
    results = build_durable_memories([source, other], now=now)
    assert results[0].id != results[1].id


def test_updates_use_replacement_without_overwriting_original(memory):
    original = memory()
    with pytest.raises(FrozenInstanceError):
        original.content = "changed"
    updated = replace(original, content="newer observation", id=content_id("newer observation"))
    assert original.content == "Use pytest fixtures."
    assert updated.id != original.id


@pytest.mark.parametrize("hours,expected", [(-168, 1), (0, 1), (168, .5), (336, .25), (672, .0625)])
def test_recency_half_lives(now, hours, expected):
    record = EpisodicRecord(now - timedelta(hours=hours), "tag", "text")
    assert f_recency(record, now=now, half_life_hours=168) == pytest.approx(expected)


@pytest.mark.parametrize("hours", [-10, 0, 1, 24, 693.14718056, 10000])
@pytest.mark.parametrize("importance", [0, .2, 1])
def test_decay_matches_exponential_clock(now, hours, importance):
    created = now - timedelta(hours=hours)
    expected = importance * math.exp(-.001 * max(0, hours))
    assert passive_decay(importance, created, now=now, lambda_=.001) == pytest.approx(expected)


def test_recent_access_resets_decay_clock_without_mutating_source(memory, now):
    old = memory(created_at=now - timedelta(days=60), last_accessed_at=now - timedelta(hours=1))
    result = refresh_decay([old], now=now)[0]
    assert result.importance == pytest.approx(.5 * math.exp(-config.DECAY_LAMBDA_PER_HOUR))
    assert old.importance == .5
    assert result.id == old.id and result.merged_from == old.merged_from


@pytest.mark.parametrize("text,expected", [("passed", 1), ("FAILED", 0), ("fixed error", .4),
    ("plain observation", .5), ("Traceback", 0), ("works", 1), ("success then exit 1", .4)])
def test_outcome_signals(now, text, expected):
    assert f_outcome(EpisodicRecord(now, "tag", text)) == expected


@pytest.mark.parametrize("count", [0, 1, 2, 9])
def test_repeated_category_frequency(now, count):
    record = EpisodicRecord(now, "task", "alpha")
    corpus = [record] + [EpisodicRecord(now, "task", f"different {i}") for i in range(count)]
    assert f_frequency(record, corpus) == pytest.approx(1 / (1 + count))


def test_surprise_excludes_future_and_simultaneous_records(now):
    record = EpisodicRecord(now, "a", "same")
    future = replace(record, timestamp=now + timedelta(seconds=1))
    simultaneous = replace(record, tag="b")
    assert f_surprise(record, [record, future, simultaneous]) == 1
    assert f_surprise(future, [record, future]) == 0


def test_entity_salience_counts_records_not_repeated_mentions(now):
    records = [EpisodicRecord(now, "a", "`cache.py` `cache.py` `rare.py`"),
               EpisodicRecord(now, "b", "`cache.py`")]
    index = build_entity_index(records)
    assert index["cache.py"] == 1 and index["rare.py"] == .5
    assert f_entity_salience(records[0], index) == 1
    assert "T-042" in extract_entities("Fix T-042")


def test_explicit_choices_get_the_configured_initial_boost(now):
    inferred = EpisodicRecord(now, "choice", "prefer tabs")
    explicit = replace(inferred, provenance="explicit")
    score = composite_importance(inferred, [inferred], {}, now=now)
    assert composite_importance(explicit, [explicit], {}, now=now) == pytest.approx(
        min(1, score * (1 + config.EXPLICIT_PROVENANCE_BOOST)))


@pytest.mark.parametrize("count", [0, 1, 4, 5, 6, 10, 25])
@pytest.mark.parametrize("fraction", [0, .2, .5, 1])
@pytest.mark.parametrize("enabled", [False, True])
def test_delete_fraction_matrix_preserves_highest_scores(memory, monkeypatch, count, fraction, enabled):
    monkeypatch.setattr(config, "ENABLE_PRUNING", enabled)
    monkeypatch.setattr(config, "MIN_PRUNE_BUDGET", 0)
    source = [memory(str(i), importance=i / max(1, count)) for i in range(count)]
    result = rerank_and_prune(source, prune_fraction=fraction)
    retained = count - math.floor(count * fraction) if enabled else count
    assert [m.content for m in result] == [str(i) for i in reversed(range(count))][:retained]
    assert len(source) == count


@pytest.mark.parametrize("total_chars,expected", [(1999, 5), (2000, 4), (2001, 4)])
def test_delete_budget_boundary_is_characters(memory, monkeypatch, total_chars, expected):
    monkeypatch.setattr(config, "ENABLE_PRUNING", True)
    source = [memory("語" * 400) for _ in range(4)] + [memory("語" * (total_chars - 1600))]
    assert len(rerank_and_prune(source)) == expected


def test_tied_scores_keep_input_order_and_live_fraction_changes(memory, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PRUNING", True)
    monkeypatch.setattr(config, "MIN_PRUNE_BUDGET", 0)
    source = [memory(str(i)) for i in range(10)]
    assert rerank_and_prune(source) == source[:8]
    monkeypatch.setattr(config, "PRUNE_BOTTOM_PERCENT", .5)
    assert rerank_and_prune(source) == source[:5]


@pytest.mark.parametrize("name", ["learnings.md", "choices with spaces.md", "記憶.md"])
def test_public_file_read_is_non_destructive(tmp_path, block, embedder, now, name):
    path = tmp_path / name
    path.write_text(block("durable content"), encoding="utf-8")
    before = path.read_bytes()
    result = compact_markdown_file(path, now=now, embedder=embedder, use_llm=False)
    assert [m.content for m in result] == ["durable content"]
    assert path.read_bytes() == before


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        compact_markdown_file(tmp_path / "absent.md", use_llm=False)
