"""Real parse -> score -> merge -> decay -> prune and file command integration."""

import asyncio
from datetime import timedelta
import json
from unittest.mock import Mock

import pytest

import artifacts as art
import compact_command as command
import config
import main
from mem_manager import compact_markdown, compact_markdown_file
from mem_manager.importance import parse_episodic_md
from requirements.node import _user_choices_markdown


@pytest.mark.parametrize("count", [1, 2, 5, 20, 80])
@pytest.mark.parametrize("prune", [False, True])
def test_bulk_duplicate_ingestion_merges_before_pruning(block, now, embedder, monkeypatch, count, prune):
    monkeypatch.setattr(config, "ENABLE_PRUNING", prune)
    monkeypatch.setattr(config, "MIN_PRUNE_BUDGET", 0)
    text = "\n".join(block("Same durable lesson", timestamp=now - timedelta(seconds=i)) for i in range(count))
    result = compact_markdown(text, now=now, embedder=embedder, use_llm=False)
    assert len(result) == 1  # floor(1 * .2) == 0 after consolidation
    assert result[0].content == "Same durable lesson"
    assert len(set(result[0].merged_from)) == count
    assert result[0].last_accessed_at == now


@pytest.mark.parametrize("count", [1, 5, 25, 100])
def test_bulk_distinct_ingestion_has_no_cross_record_data_loss(block, now, embedder, count):
    contents = [f"Independent fact {i:04}: value-{i * 37}" for i in range(count)]
    text = "\n".join(block(c, tag=f"T-{i}") for i, c in enumerate(contents))
    result = compact_markdown(text, now=now, embedder=embedder, use_llm=False)
    assert {m.content for m in result} == set(contents)
    assert len({m.id for m in result}) == count
    assert all(0 <= m.importance <= 1 for m in result)


def test_pipeline_order_is_visible_in_stage_logs(block, now, embedder, caplog):
    with caplog.at_level("INFO"):
        compact_markdown(block(), now=now, embedder=embedder, use_llm=False)
    stages = ["[PARSE] input", "[SCORE] input", "[DEDUP_MERGE] input", "[CONSOLIDATE] input", "[PRUNE] input"]
    positions = [caplog.text.index(stage) for stage in stages]
    assert positions == sorted(positions)


@pytest.mark.parametrize("filename", ["learnings.md", "user_choices.md", "日本語 with spaces.md"])
def test_file_command_commits_readable_results(tmp_path, block, embedder, filename):
    path = tmp_path / filename
    path.write_text(block("same") + "\n" + block("same"), encoding="utf-8")
    result = asyncio.run(command.compact_file_async(path, use_llm=False, embedder=embedder))
    assert result["status"] == "success" and result["memory_count"] == 1
    records = parse_episodic_md(path.read_text(encoding="utf-8"))
    assert [record.content for record in records] == ["same"]
    prefix = config.DURABLE_MEMORY_TAG_PREFIXES.get(filename.lower())
    expected_tag = f"{prefix} 1" if prefix else "T-001"
    assert records[0].tag == expected_tag
    assert result["memories"][0]["tag"] == expected_tag
    assert json.loads(json.dumps(result))["memories"][0]["content"] == "same"


@pytest.mark.parametrize(
    ("filename", "prefix"),
    [("learnings.md", "Learning"), ("user_choices.md", "User Choice")],
)
def test_durable_memories_use_file_specific_chronological_names(
    tmp_path, block, now, embedder, filename, prefix
):
    path = tmp_path / filename
    path.write_text(
        block("newer", tag="source-new", timestamp=now)
        + "\n"
        + block("older", tag="source-old", timestamp=now - timedelta(days=1)),
        encoding="utf-8",
    )

    result = compact_markdown_file(path, now=now, embedder=embedder, use_llm=False)

    assert {memory.content: memory.tag for memory in result} == {
        "older": f"{prefix} 1",
        "newer": f"{prefix} 2",
    }


def test_unconfigured_memory_file_retains_source_tags(tmp_path, block, embedder):
    path = tmp_path / "other.md"
    path.write_text(block("keep", tag="source-tag"), encoding="utf-8")

    result = compact_markdown_file(path, embedder=embedder, use_llm=False)

    assert result[0].tag == "source-tag"


def test_repeated_singleton_compaction_keeps_content_and_id(tmp_path, block, embedder):
    path = tmp_path / "learnings.md"
    path.write_text(block("keep this"), encoding="utf-8")
    results = [asyncio.run(command.compact_file_async(path, use_llm=False, embedder=embedder)) for _ in range(4)]
    assert {r["memory_count"] for r in results} == {1}
    assert len({r["memories"][0]["id"] for r in results}) == 1
    assert len(parse_episodic_md(path.read_text(encoding="utf-8"))) == 1


def test_missing_file_command_returns_error_without_creation(tmp_path):
    path = tmp_path / "absent.md"
    result = asyncio.run(command.compact_file_async(path, use_llm=False))
    assert result["status"] == "error" and result["error"] == "file not found"
    assert not path.exists()


@pytest.mark.parametrize("error", [RuntimeError("embedding failed"), PermissionError("read denied"),
                                    ValueError("invalid input"), ModuleNotFoundError("unrelated_package")])
def test_processing_errors_preserve_original_file(tmp_path, block, monkeypatch, error):
    path = tmp_path / "learnings.md"
    path.write_text(block("original evidence"), encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setattr(command, "compact_markdown_file", Mock(side_effect=error))
    result = asyncio.run(command.compact_file_async(path, use_llm=False))
    assert result["status"] == "error" and result["error"] == str(error)
    assert path.read_bytes() == original


@pytest.mark.parametrize("dependency", ["numpy", "sentence_transformers", "torch", "groq", "openai", "anthropic"])
def test_missing_optional_dependency_retries_once_with_real_offline_pipeline(tmp_path, block, monkeypatch, dependency):
    path = tmp_path / "learnings.md"
    path.write_text(block("a") + "\n" + block("b"), encoding="utf-8")
    calls = []

    def first_unavailable(path, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ModuleNotFoundError(f"No module named '{dependency}'")
        return compact_markdown_file(path, **kwargs)

    monkeypatch.setattr(command, "compact_markdown_file", first_unavailable)
    result = asyncio.run(command.compact_file_async(path, use_llm=True))
    assert result["status"] == "success" and result["memory_count"] == 2
    assert len(calls) == 2 and calls[1]["use_llm"] is False
    assert isinstance(calls[1]["embedder"], command.NoOpEmbedder)


def test_dependency_retry_is_bounded_and_retains_file(tmp_path, block, monkeypatch):
    path = tmp_path / "learnings.md"
    path.write_text(block(), encoding="utf-8")
    original = path.read_bytes()
    call = Mock(side_effect=ModuleNotFoundError("numpy"))
    monkeypatch.setattr(command, "compact_markdown_file", call)
    result = asyncio.run(command.compact_file_async(path))
    assert result["status"] == "error" and call.call_count == 2
    assert path.read_bytes() == original


def test_batch_handles_success_missing_and_empty_independently(tmp_path, block):
    good, missing, empty = [tmp_path / name for name in ("good.md", "missing.md", "empty.md")]
    good.write_text(block(), encoding="utf-8")
    empty.write_text("", encoding="utf-8")
    result = asyncio.run(command.compact_files_parallel([good, missing, empty], use_llm=False))
    assert (result["files_processed"], result["files_succeeded"], result["files_failed"], result["total_memories"]) == (3, 2, 1, 1)
    assert [r["file"] for r in result["results"]] == list(map(str, [good, missing, empty]))


def test_empty_batch_is_successful():
    result = asyncio.run(command.compact_files_parallel([], use_llm=False))
    assert result["files_processed"] == result["files_failed"] == result["total_memories"] == 0


def test_command_uses_configured_temp_files_and_prints_summary(tmp_path, block, monkeypatch, capsys):
    path = tmp_path / "learnings.md"
    path.write_text(block(), encoding="utf-8")
    monkeypatch.setattr(config, "COMPACT_ARTIFACT_FILES", [path])
    assert command.compact_command(use_llm=False) == 0
    assert "Files succeeded:  1" in capsys.readouterr().out


def test_command_preflight_missing_file_leaves_all_inputs_untouched(tmp_path, block, monkeypatch):
    good = tmp_path / "good.md"
    good.write_text(block(), encoding="utf-8")
    original = good.read_bytes()
    monkeypatch.setattr(config, "COMPACT_ARTIFACT_FILES", [good, tmp_path / "missing.md"])
    assert command.compact_command(use_llm=False) == 2
    assert good.read_bytes() == original


def test_command_reports_processing_failure(tmp_path, monkeypatch):
    path = tmp_path / "invalid.md"
    path.write_bytes(b"\xff\xfe\x00")
    monkeypatch.setattr(config, "COMPACT_ARTIFACT_FILES", [path])
    assert command.compact_command(use_llm=False) == 1
    assert path.read_bytes() == b"\xff\xfe\x00"


def test_main_routes_compact_before_normal_run_validation(monkeypatch):
    callback = Mock(return_value=7)
    monkeypatch.setattr(command, "compact_command", callback)
    assert main.main(["--compact"]) == 7
    callback.assert_called_once_with()


@pytest.mark.parametrize("agent", ["requirements", "planner", "T-001", "merger", "reviewer", "supervisor"])
@pytest.mark.parametrize("session", ["", "claude:abc"], ids=["no-session", "sessioned"])
def test_actual_agent_append_format_is_ingested(store, agent, session, now, embedder):
    art.append_learning(store, agent, "A durable engineering lesson.", session=session)
    result = compact_markdown_file(store.learnings, now=now, embedder=embedder, use_llm=False)
    assert len(result) == 1 and result[0].tag == "Learning 1"
    assert result[0].content == "A durable engineering lesson."
    # The marker is metadata, not content: it identifies the record without
    # becoming part of the text that gets embedded, merged, or shown back.
    assert result[0].session == session
    assert art.read_learnings_stamp(store) == store.learnings.stat().st_size


def test_requirements_choices_writer_to_memory_pipeline(store, embedder):
    text = _user_choices_markdown(run_id=store.run_id, goal="Use PostgreSQL", stated=["Keep Python"],
                                 qa=[("Test framework?", "pytest")], session="codex:xyz")
    art.append_user_choices(store, store.run_id, text)
    result = compact_markdown_file(store.user_choices, embedder=embedder, use_llm=False)
    assert len(result) == 1 and result[0].tag == "User Choice 1"
    assert all(value in result[0].content for value in ("PostgreSQL", "Keep Python", "pytest"))
    assert result[0].session == "codex:xyz"
    assert "<!-- session:" not in result[0].content


def test_session_identity_survives_repeated_file_compaction(store, embedder):
    art.append_learning(store, "reviewer", "Serialise the shared writes.", session="claude:s1")
    art.append_learning(store, "planner", "Split the wave.", session="codex:s2")
    first = asyncio.run(command.compact_file_async(store.learnings, embedder=embedder, use_llm=False))
    second = asyncio.run(command.compact_file_async(store.learnings, embedder=embedder, use_llm=False))
    assert {m["session"] for m in first["memories"]} == {"claude:s1", "codex:s2"}
    assert {m["session"] for m in second["memories"]} == {"claude:s1", "codex:s2"}
    # Re-emitting the marker has to be a fixed point, or every /compact would
    # rewrite the block and hand the same content a new id each time.
    assert [m["id"] for m in first["memories"]] == [m["id"] for m in second["memories"]]


def test_two_projects_compact_independently(tmp_path, block, embedder):
    paths = [tmp_path / "project-a.md", tmp_path / "project-b.md"]
    for path, content in zip(paths, ["private alpha", "private beta"]):
        path.write_text(block(content), encoding="utf-8")
    result = asyncio.run(command.compact_files_parallel(paths, use_llm=False))
    assert [r["memories"][0]["content"] for r in result["results"]] == ["private alpha", "private beta"]


def test_append_compact_append_compact_does_not_resurrect_pruned_content(store, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PRUNING", True)
    monkeypatch.setattr(config, "MIN_PRUNE_BUDGET", 0)
    for i in range(10):
        art.append_learning(store, f"T-{i}", f"Distinct fact {i}")
    first = asyncio.run(command.compact_file_async(store.learnings, use_llm=False))
    assert first["memory_count"] == 8
    removed = {f"Distinct fact {i}" for i in range(10)} - {m["content"] for m in first["memories"]}
    art.append_learning(store, "T-new", "New independent evidence")
    second = asyncio.run(command.compact_file_async(store.learnings, use_llm=False))
    assert second["memory_count"] == 8  # nine memories minus floor(9 * .2)
    assert removed.isdisjoint({m["content"] for m in second["memories"]})
