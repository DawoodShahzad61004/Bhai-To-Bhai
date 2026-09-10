"""Auto-compact: config-gated triggering, writer-exclusion locking, lockless reads.

Covers the three contracts auto-compact adds on top of the existing manual
`/compact` pipeline: (1) `config.ENABLE_PRUNING`/`MIN_PRUNE_BUDGET` gate
whether it fires at all, (2) a file being compacted excludes other writers
through the same sidecar lock `artifacts.py` already uses, and (3) readers
are never locked out - they either see the file whole before compaction or
whole after, via the atomic swap in `compact_command._compact_and_swap`.
"""

from pathlib import Path

import artifacts as art
import compact_command as command
import config


def test_should_auto_compact_off_when_enable_pruning_is_false(tmp_path, monkeypatch):
    path = tmp_path / "learnings.md"
    path.write_text("x" * 5000, encoding="utf-8")
    monkeypatch.setattr(config, "ENABLE_PRUNING", False)
    monkeypatch.setattr(config, "MIN_PRUNE_BUDGET", 10)
    assert command.should_auto_compact(path) is False


def test_should_auto_compact_off_below_min_prune_budget(tmp_path, monkeypatch):
    path = tmp_path / "learnings.md"
    path.write_text("x" * 5, encoding="utf-8")
    monkeypatch.setattr(config, "ENABLE_PRUNING", True)
    monkeypatch.setattr(config, "MIN_PRUNE_BUDGET", 10_000)
    assert command.should_auto_compact(path) is False


def test_should_auto_compact_on_once_both_gates_pass(tmp_path, monkeypatch):
    path = tmp_path / "learnings.md"
    path.write_text("x" * 5000, encoding="utf-8")
    monkeypatch.setattr(config, "ENABLE_PRUNING", True)
    monkeypatch.setattr(config, "MIN_PRUNE_BUDGET", 10)
    assert command.should_auto_compact(path) is True


def test_should_auto_compact_missing_file_is_false(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PRUNING", True)
    monkeypatch.setattr(config, "MIN_PRUNE_BUDGET", 0)
    assert command.should_auto_compact(tmp_path / "absent.md") is False


def test_maybe_auto_compact_is_a_noop_below_budget(tmp_path, block, monkeypatch):
    path = tmp_path / "learnings.md"
    path.write_text(block("keep"), encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setattr(config, "ENABLE_PRUNING", True)
    monkeypatch.setattr(config, "MIN_PRUNE_BUDGET", 1_000_000)
    assert command.maybe_auto_compact(path, use_llm=False) is None
    assert path.read_bytes() == original


def test_maybe_auto_compact_runs_once_over_budget(tmp_path, block, embedder, monkeypatch):
    path = tmp_path / "learnings.md"
    path.write_text(block("keep this"), encoding="utf-8")
    monkeypatch.setattr(config, "ENABLE_PRUNING", True)
    monkeypatch.setattr(config, "MIN_PRUNE_BUDGET", 0)
    result = command.maybe_auto_compact(path, use_llm=False)
    assert result["status"] == "success" and result["memory_count"] == 1
    assert "Learning 1" in path.read_text(encoding="utf-8")


def test_lock_target_for_user_choices_is_the_learnings_lock(tmp_path):
    shared = tmp_path
    user_choices = shared / art.USER_CHOICES_FILE
    learnings = shared / art.LEARNINGS_FILE
    assert command._lock_target_for(user_choices) == learnings
    assert command._lock_target_for(learnings) == learnings


def test_reader_is_never_blocked_by_the_writer_lock(store, block):
    store.learnings.write_text(block("existing"), encoding="utf-8")
    # A reader never opens the sidecar `.lock` file at all, so it succeeds
    # immediately even while the exclusive lock - the one compaction and
    # every writer share - is held.
    with art._exclusive_lock(store.learnings):
        assert "existing" in art.read_text(store.learnings)


def test_append_learning_triggers_auto_compact_for_a_configured_target(store, monkeypatch):
    monkeypatch.setattr(config, "COMPACT_ARTIFACT_FILES", [store.learnings])
    monkeypatch.setattr(config, "ENABLE_PRUNING", True)
    seen = []
    monkeypatch.setattr(command, "maybe_auto_compact", lambda path, **kw: seen.append(path))

    art.append_learning(store, "T-1", "A finding worth compacting")

    assert seen == [store.learnings]


def test_append_learning_skips_auto_compact_for_an_unconfigured_file(store, monkeypatch):
    monkeypatch.setattr(config, "COMPACT_ARTIFACT_FILES", [Path("/unrelated/other.md")])
    monkeypatch.setattr(config, "ENABLE_PRUNING", True)
    seen = []
    monkeypatch.setattr(command, "maybe_auto_compact", lambda path, **kw: seen.append(path))

    art.append_learning(store, "T-1", "A finding")

    assert seen == []


def test_append_learning_skips_auto_compact_when_pruning_disabled(store, monkeypatch):
    monkeypatch.setattr(config, "COMPACT_ARTIFACT_FILES", [store.learnings])
    monkeypatch.setattr(config, "ENABLE_PRUNING", False)
    seen = []
    monkeypatch.setattr(command, "maybe_auto_compact", lambda path, **kw: seen.append(path))

    art.append_learning(store, "T-1", "A finding")

    assert seen == []


def test_append_user_choices_triggers_auto_compact_through_its_own_path(store, monkeypatch):
    monkeypatch.setattr(config, "COMPACT_ARTIFACT_FILES", [store.user_choices])
    monkeypatch.setattr(config, "ENABLE_PRUNING", True)
    seen = []
    monkeypatch.setattr(command, "maybe_auto_compact", lambda path, **kw: seen.append(path))

    art.append_user_choices(store, store.run_id, "Use PostgreSQL")

    assert seen == [store.user_choices]


def test_a_failed_auto_compact_does_not_fail_the_triggering_append(store, monkeypatch):
    monkeypatch.setattr(config, "COMPACT_ARTIFACT_FILES", [store.learnings])
    monkeypatch.setattr(config, "ENABLE_PRUNING", True)
    monkeypatch.setattr(
        command, "maybe_auto_compact",
        lambda path, **kw: {"file": str(path), "status": "error", "error": "boom", "memories": []},
    )

    art.append_learning(store, "T-1", "A finding")

    assert "A finding" in store.learnings.read_text(encoding="utf-8")
