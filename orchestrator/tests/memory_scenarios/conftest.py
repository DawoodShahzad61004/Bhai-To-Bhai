"""Offline services and isolated, real on-disk memory artifacts."""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

import artifacts as art
import compact_command as command
import config
from mem_manager.memory import DurableMemory, content_id
from mem_manager.services import dedup_merge


class ExactEmbedder:
    """Controlled similarity: identical text=1, different text=0.

    This tests pipeline decisions, not the quality of a semantic model.
    """

    def __init__(self):
        self.batches = []

    def generate_embedding(self, texts):
        self.batches.append(list(texts))
        return list(texts)

    def cosine_similarity(self, left, right):
        return float(left == right)


@pytest.fixture
def now():
    return datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


@pytest.fixture
def block(now):
    def make(content="Use pytest fixtures.", tag="T-001", timestamp=None, choices=False):
        stamp = (timestamp or now).strftime("%Y-%m-%d %H:%M:%SZ")
        header = f"## Run `{tag}` — {stamp}" if choices else f"## {stamp} — {tag}"
        return f"{header}\n\n{content}\n"

    return make


@pytest.fixture
def memory(now):
    def make(content="Use pytest fixtures.", **kwargs):
        identifier = content_id(content)
        fields = dict(id=identifier, content=content, tag="T-001", created_at=now,
                      last_accessed_at=now, importance=0.5, provenance="inferred",
                      merged_from=[identifier])
        fields.update(kwargs)
        return DurableMemory(**fields)

    return make


@pytest.fixture
def embedder():
    return ExactEmbedder()


@pytest.fixture(autouse=True)
def offline(monkeypatch, embedder):
    monkeypatch.setattr(dedup_merge, "_default_embedder", lambda: embedder)
    monkeypatch.setattr(dedup_merge, "_default_llm_calls", Mock(
        side_effect=AssertionError("Test attempted to construct a live LLM client")))
    monkeypatch.setattr(command, "setup_logging", lambda: None)
    monkeypatch.setattr(config, "ENABLE_PRUNING", False)
    monkeypatch.setattr(config, "MIN_PRUNE_BUDGET", 2000)
    monkeypatch.setattr(config, "PRUNE_BOTTOM_PERCENT", 0.2)
    monkeypatch.setattr(config, "MERGE_SIMILARITY_THRESHOLD", 0.6)
    monkeypatch.setattr(config, "MERGE_LLM_ENABLED", True)
    monkeypatch.setattr(config, "MERGE_VALIDATION_ENABLED", True)


@pytest.fixture
def store(tmp_path):
    # Construct directly: never use config's real COMPACT_ARTIFACT_FILES or
    # prepare()'s sibling-directory placement for destructive scenarios.
    result = art.RunArtifacts(run_id="memory-test", root=tmp_path / "store")
    result.shared_dir.mkdir(parents=True)
    return result
