"""Adversarial integration contracts, including currently reproducible defects.

These are ordinary assertions: no skips or xfails conceal data loss. Contracts
come from Architecture principles 1/4/6, ADR-027/028, and the orchestrator's
existing append-only writer and peer-reader guarantees. They deliberately do
not require the unimplemented target recall/fidelity/interference APIs.
"""

import asyncio
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

import artifacts as art
import compact_command as command
import config
from mem_manager import compact_markdown, compact_markdown_file
from mem_manager.consolidate import refresh_decay, rerank_and_prune
from mem_manager.importance import parse_episodic_md
from mem_manager.memory import build_durable_memories
from mem_manager.services import dedup_merge as merge
from requirements.node import _user_choices_markdown


def test_explicit_user_choice_provenance_survives_actual_writer(store, embedder):
    text = _user_choices_markdown(run_id=store.run_id, goal="Always use tabs", stated=[], qa=[])
    art.append_user_choices(store, store.run_id, text)
    result = compact_markdown_file(store.user_choices, embedder=embedder, use_llm=False)
    assert result[0].provenance == "explicit", "User-stated choices were downgraded to inferred"


def test_unmerged_events_keep_separate_identities(block, now, embedder):
    # A no-op embedder promises that records stay separate when dependencies
    # are absent. Equal content does not make distinct source events identical.
    text = block("same", timestamp=now - timedelta(days=1)) + "\n" + block("same")
    result = compact_markdown(text, now=now, embedder=command.NoOpEmbedder(), use_llm=False)
    assert len(result) == 2
    assert result[0].id != result[1].id, "Separate events acquired the same durable ID"


def test_singleton_dedup_preserves_the_source_id(block, now, embedder):
    source = build_durable_memories(parse_episodic_md(block()), now=now)
    result = merge.dedupe_and_merge(source, embedder=embedder, use_llm=False)
    assert result[0].id == source[0].id, "An unmerged source record changed identity"


@pytest.mark.parametrize("field", ["last_accessed_at", "provenance", "session", "merged_from"])
def test_markdown_roundtrip_retains_durable_metadata(memory, now, field):
    original = memory("a durable lesson", created_at=now - timedelta(days=30),
                      last_accessed_at=now, provenance="explicit", session="claude:abc",
                      merged_from=["source-a", "source-b"])
    text = command._memories_to_markdown([original])
    reread = build_durable_memories(parse_episodic_md(text), now=now)[0]
    assert getattr(reread, field) == getattr(original, field), f"Markdown persistence discarded {field}"


def test_repeated_file_compaction_retains_merge_source_count(tmp_path, block, now, embedder):
    path = tmp_path / "learnings.md"
    path.write_text(block("same", timestamp=now - timedelta(days=1)) + "\n" + block("same"), encoding="utf-8")
    first = asyncio.run(command.compact_file_async(path, embedder=embedder, use_llm=False))
    second = asyncio.run(command.compact_file_async(path, embedder=embedder, use_llm=False))
    assert first["memories"][0]["merged_from_count"] == 2
    assert second["memories"][0]["merged_from_count"] == 2, "Recompaction erased original source lineage"


def test_compacted_learnings_are_visible_to_existing_peer_reader(store):
    art.append_learning(store, "T-peer", "Use a shared transaction.")
    before, _ = art.peer_entries_since(store, "T-reader", 0)
    assert "Use a shared transaction." in before
    asyncio.run(command.compact_file_async(store.learnings, use_llm=False))
    after, _ = art.peer_entries_since(store, "T-reader", 0)
    assert "Use a shared transaction." in after, "Compaction wrote headers the peer reader cannot recognize"


def test_compaction_updates_learnings_stamp(store):
    art.append_learning(store, "T-peer", "Same lesson")
    art.append_learning(store, "T-peer", "Same lesson")
    asyncio.run(command.compact_file_async(store.learnings, use_llm=False))
    assert art.read_learnings_stamp(store) == store.learnings.stat().st_size, "Shared change stamp is stale"


def test_compaction_invalidates_offsets_into_rewritten_content(store):
    for i in range(4):
        art.append_learning(store, "T-peer", "Repeated lesson")
    old_end = store.learnings.stat().st_size
    art.write_learnings_cursor(store, "T-reader", old_end)
    asyncio.run(command.compact_file_async(store.learnings, use_llm=False))
    cursor = art.read_learnings_cursor(store, "T-reader")
    assert cursor is None or cursor <= store.learnings.stat().st_size, "Reader offset points beyond rewritten file"


def test_choice_ledger_remains_idempotent_after_compaction(store, block):
    text = block("Explicit decision", tag=store.run_id, choices=True)
    art.append_user_choices(store, store.run_id, text)
    asyncio.run(command.compact_file_async(store.user_choices, use_llm=False))
    compacted = store.user_choices.read_bytes()
    art.append_user_choices(store, store.run_id, text)
    assert store.user_choices.read_bytes() == compacted, "Compaction removed the replay-deduplication marker"


def test_choice_run_markers_are_not_attached_to_the_previous_memory(store, block):
    art.append_user_choices(store, "run-one", block("first decision", tag="run-one", choices=True))
    art.append_user_choices(store, "run-two", block("second decision", tag="run-two", choices=True))
    result = compact_markdown_file(store.user_choices, use_llm=False)
    first = next(m for m in result if m.tag == "run-one")
    assert "run-two" not in first.content, "Next run's control marker leaked into the previous decision"


def test_concurrent_append_during_compaction_is_not_lost(store, monkeypatch):
    art.append_learning(store, "T-first", "Original lesson")
    real_compact = command.compact_markdown_file

    def append_after_snapshot(path, **kwargs):
        snapshot = real_compact(path, **kwargs)
        # Deterministically interleave a real writer after snapshot extraction.
        # No sleeps or timing-sensitive thread races are needed to reproduce it.
        art.append_learning(store, "T-concurrent", "Concurrent evidence must survive")
        return snapshot

    monkeypatch.setattr(command, "compact_markdown_file", append_after_snapshot)
    asyncio.run(command.compact_file_async(store.learnings, use_llm=False))
    assert "Concurrent evidence must survive" in store.learnings.read_text(encoding="utf-8")


def test_partial_write_failure_does_not_destroy_original(tmp_path, block, monkeypatch):
    path = tmp_path / "learnings.md"
    path.write_text(block("Important original evidence"), encoding="utf-8")
    original = path.read_bytes()
    real_write_text = Path.write_text

    def partial_write(self, text, *args, **kwargs):
        if self == path:
            with self.open("wb") as handle:
                handle.write(b"partial")
            raise OSError("simulated disk full")
        return real_write_text(self, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", partial_write)
    result = asyncio.run(command.compact_file_async(path, use_llm=False))
    assert result["status"] == "error"
    assert path.read_bytes() == original, "Failed in-place write destroyed the source"


@pytest.mark.parametrize("content", ["# Imported memory\nUnstructured but valuable evidence", "## malformed\nKeep this evidence"])
def test_unparseable_nonempty_file_is_not_silently_erased(tmp_path, content):
    path = tmp_path / "learnings.md"
    path.write_text(content, encoding="utf-8")
    original = path.read_bytes()
    result = asyncio.run(command.compact_file_async(path, use_llm=False))
    assert path.read_bytes() == original, f"{result['status']}: unrecognized input was erased"


@pytest.mark.parametrize("body", ["before\n\n## Supporting details\n\nafter",
                                  "before\n```markdown\n## Example header\nexample\n```\nafter"])
def test_nested_markdown_does_not_truncate_memory_content(block, body):
    records = parse_episodic_md(block(body))
    assert len(records) == 1 and records[0].content == body, "Non-event heading truncated the memory body"


def test_utf8_bom_does_not_drop_the_first_record(tmp_path, block):
    path = tmp_path / "learnings.md"
    path.write_text(block("first evidence"), encoding="utf-8-sig")
    result = compact_markdown_file(path, use_llm=False)
    assert len(result) == 1 and result[0].content == "first evidence"


def test_serializer_converts_offset_timestamp_to_utc(memory, now):
    local = now.astimezone(timezone(timedelta(hours=5)))
    text = command._memories_to_markdown([memory(created_at=local)])
    assert parse_episodic_md(text)[0].timestamp == now, "A non-UTC clock was labeled Z without conversion"


def test_default_llm_initialization_failure_uses_deterministic_fallback(memory, embedder, monkeypatch):
    monkeypatch.setattr(merge, "_default_llm_calls", Mock(side_effect=RuntimeError("provider unavailable")))
    result = merge.dedupe_and_merge([memory("same"), memory("same")], embedder=embedder)
    assert result[0].content == "same"


@pytest.mark.parametrize("case", ["disabled", "singleton", "no-duplicates"])
def test_no_llm_setup_when_no_merge_call_is_needed(memory, embedder, monkeypatch, case):
    setup = Mock(side_effect=AssertionError("No LLM client should be needed"))
    monkeypatch.setattr(merge, "_default_llm_calls", setup)
    source = [memory("same"), memory("same")]
    if case == "disabled":
        monkeypatch.setattr(config, "MERGE_LLM_ENABLED", False)
    elif case == "singleton":
        source = source[:1]
    else:
        source[1] = memory("different")
    merge.dedupe_and_merge(source, embedder=embedder, use_llm=True)
    setup.assert_not_called()


@pytest.mark.parametrize("verdict", ["NOT FAITHFUL", "The word FAITHFUL is insufficient", "FAITHFULNESS"])
def test_judge_requires_an_unambiguous_faithful_verdict(memory, verdict):
    result = merge.merge_group([memory("one"), memory("two")],
                              llm_call=lambda _: "invented", judge_call=lambda _: verdict)
    assert result.content == "one\n---\ntwo", "Ambiguous judge output authorized a merge"


def test_conflicting_near_duplicates_keep_distinct_timestamped_records(block, now, embedder):
    # Semantically related opposites can have high cosine similarity. Control
    # the score to verify conflict safety independently of model calibration.
    embedder.cosine_similarity = lambda *_: .95
    old = "Always enable telemetry for this project."
    new = "Never enable telemetry for this project."
    text = block(old, timestamp=now - timedelta(days=1)) + "\n" + block(new)
    result = compact_markdown(text, now=now, embedder=embedder, use_llm=False)
    assert len(result) == 2, "Contradictory choices collapsed into one timestamped record"
    assert {m.content for m in result} == {old, new}


def test_same_time_maintenance_does_not_apply_decay_twice(memory, now):
    original = memory(created_at=now - timedelta(days=30), last_accessed_at=now - timedelta(days=30))
    once = refresh_decay([original], now=now)
    twice = refresh_decay(once, now=now)
    assert twice[0].importance == pytest.approx(once[0].importance), "Same elapsed time was charged twice"


@pytest.mark.parametrize("fraction", [-.1, 1.1, 2, float("nan"), float("inf")])
def test_invalid_prune_fraction_is_rejected_without_a_deletion(memory, monkeypatch, fraction):
    monkeypatch.setattr(config, "ENABLE_PRUNING", True)
    monkeypatch.setattr(config, "MIN_PRUNE_BUDGET", 0)
    source = [memory(str(i)) for i in range(10)]
    with pytest.raises(ValueError):
        rerank_and_prune(source, prune_fraction=fraction)
    assert len(source) == 10
