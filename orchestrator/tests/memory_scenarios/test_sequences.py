"""Reproducible multi-operation CRUD sequences and concurrent writers.

The independent oracle is a set of complete content strings. These scenarios
do not model activation, scoring, or semantic similarity with the same code
as the implementation under test.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import random

import pytest

import artifacts as art
import compact_command as command
import config
from mem_manager.importance import parse_episodic_md


@pytest.mark.parametrize("seed", range(12))
def test_seeded_create_read_update_compact_delete_restart_sequence(store, monkeypatch, seed):
    rng = random.Random(seed)
    expected = set()
    store.learnings.touch()
    # Every seed visits every operation, then varies their interleaving.
    operations = ["create", "read", "reinforce", "update", "compact", "restart", "delete"]
    operations += rng.choices(operations, k=53)
    for step, operation in enumerate(operations):
        if operation in {"create", "update"}:
            content = f"Seed {seed} observation {step}: {'new rule' if operation == 'update' else 'lesson'}"
            art.append_learning(store, f"T-{step}", content)
            expected.add(content)
        elif operation == "reinforce" and expected:
            content = rng.choice(sorted(expected))
            art.append_learning(store, f"T-{step}", content)
        elif operation in {"compact", "delete"}:
            deleting = operation == "delete"
            monkeypatch.setattr(config, "ENABLE_PRUNING", deleting)
            monkeypatch.setattr(config, "MIN_PRUNE_BUDGET", 0)
            monkeypatch.setattr(config, "PRUNE_BOTTOM_PERCENT", 1 if deleting else .2)
            result = asyncio.run(command.compact_file_async(store.learnings, use_llm=False))
            assert result["status"] == "success", (seed, step, result)
            if deleting:
                expected.clear()
            assert result["memory_count"] == len(expected), (seed, step, operation)
        elif operation == "restart":
            # A new handle has no process-local memory to repair the disk view.
            store = art.RunArtifacts(run_id=f"restart-{step}", root=store.root)

        actual = {r.content for r in parse_episodic_md(store.learnings.read_text(encoding="utf-8"))}
        assert actual == expected, (seed, step, operation, actual, expected)


@pytest.mark.parametrize("count", [8, 32])
def test_concurrent_agent_writes_then_compaction_preserves_every_entry(store, count):
    contents = [f"Writer {i} discovered independent evidence 語-{i}" for i in range(count)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(art.append_learning, store, f"T-{i}", text) for i, text in enumerate(contents)]
        for future in futures:
            future.result(timeout=30)
    records = parse_episodic_md(store.learnings.read_text(encoding="utf-8"))
    assert len(records) == count
    assert {r.content for r in records} == set(contents)
    assert art.read_learnings_stamp(store) == store.learnings.stat().st_size
    result = asyncio.run(command.compact_file_async(store.learnings, use_llm=False))
    assert result["status"] == "success" and result["memory_count"] == count
    assert {m["content"] for m in result["memories"]} == set(contents)
