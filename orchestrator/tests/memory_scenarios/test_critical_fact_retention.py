"""A critical, one-line instruction must not be pruned away just because it
sits inside a large file of otherwise-forgettable filler.

`corpus/test_learnings.md` is a synthetic learnings.md: 20 entries, at least
10_000 characters of body content, real config defaults
(MERGE_SIMILARITY_THRESHOLD=0.60, PRUNE_BOTTOM_PERCENT=0.20). Three entries
(Learning 1, 5, 19) each carry one short, load-bearing instruction buried in
an otherwise unremarkable paragraph:

  Learning 1  - torch/CUDA wheel compatibility constraint
  Learning 5  - a library's Python version floor
  Learning 19 - the partner API's max batch size

The other 17 are timestamped 50+ days before the frozen `now` this test uses
(RECENCY_HALF_LIFE_HOURS is 168, i.e. 7 days), so they rank far lower on
importance than the three recent, critical entries and are the ones
PRUNE_BOTTOM_PERCENT should remove instead. This is the scenario
`test_quality_information_loss.py`'s S5 exercises in the abstract (pruning
drops real content); this file pins the concrete case that actually matters -
a short, critical fact must outlive the bulk deletion around it.
"""
from datetime import datetime, timezone

import pytest

import config
import quality_keywords as qk
from mem_manager import compact_markdown
from mem_manager.importance import parse_episodic_md
from quality_embedders import LexicalEmbedder

CORPUS = "test_learnings.md"

CRITICAL_LINES = {
    "Learning 1": "All new dependencies should be selected based on their compatibility to torch 2.14.0+cu130",
    "Learning 5": "library-X==2.4.1 requires Python >=3.11; agents must not attempt installation under Python 3.10.",
    "Learning 19": "The API accepts batches of at most 100 items; all subsequent agents must split larger inputs into chunks of <=100.",
}

FROZEN_NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def source_text():
    return qk.read_corpus(CORPUS)


# --- the fixture matches its own stated shape --------------------------------


def test_the_corpus_has_at_least_20_entries_and_10000_characters(source_text):
    records = parse_episodic_md(source_text)
    assert len(records) >= 20
    assert sum(len(record.content) for record in records) >= 10_000


def test_the_corpus_carries_all_three_critical_lines_verbatim(source_text):
    records = {record.tag: record for record in parse_episodic_md(source_text)}
    for tag, line in CRITICAL_LINES.items():
        assert line in records[tag].content


# --- compaction under real defaults must not cost them -----------------------


@pytest.fixture
def compacted(source_text, monkeypatch):
    # Real production defaults, not the offline suite's relaxed overrides:
    # pruning is ON, and the budget is comfortably below this corpus's size,
    # so PRUNE actually fires rather than being skipped as a no-op.
    monkeypatch.setattr(config, "ENABLE_PRUNING", True)
    monkeypatch.setattr(config, "PRUNE_BOTTOM_PERCENT", 0.20)
    monkeypatch.setattr(config, "MIN_PRUNE_BUDGET", 1_000)
    return compact_markdown(
        source_text,
        now=FROZEN_NOW,
        embedder=LexicalEmbedder(),
        use_llm=False,
    )


def test_pruning_actually_removes_something(compacted):
    # If nothing were dropped, the assertions below would prove nothing about
    # survival under pruning - they'd pass whether pruning worked or not.
    assert 0 < len(compacted) < 20


def test_the_three_critical_lines_survive_compaction(compacted):
    surviving_text = "\n".join(memory.content for memory in compacted)
    for tag, line in CRITICAL_LINES.items():
        assert line in surviving_text, (
            f"the critical instruction from {tag} did not survive compaction - "
            "a short, load-bearing fact was lost in the bulk of a much larger file"
        )


def test_the_three_critical_entries_keep_their_own_tag(compacted):
    # None of the 20 topics is a near-duplicate of another (that's the point
    # of the corpus), so nothing here merges and every surviving memory keeps
    # its source tag unchanged - a stronger, more direct check than the
    # substring search above, on top of it rather than instead of it.
    surviving_tags = {memory.tag for memory in compacted}
    assert set(CRITICAL_LINES) <= surviving_tags
