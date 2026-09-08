"""Controlled embedding and LLM situations; ADR-027/032 and BUG-011.

Similarity values are explicit test inputs, not claims about MiniLM quality.
"""

from datetime import timedelta
from itertools import permutations
from unittest.mock import Mock

import pytest

import config
from mem_manager.services import dedup_merge as merge


@pytest.mark.parametrize("similarity,expected", [(.599999, 2), (.6, 1), (.600001, 1), (0, 2), (1, 1)])
def test_similarity_cutoff_is_inclusive(memory, embedder, similarity, expected):
    embedder.cosine_similarity = lambda *_: similarity
    groups = merge.find_near_duplicate_groups([memory("a"), memory("b")], embedder, .6)
    assert len(groups) == expected


@pytest.mark.parametrize("order", list(permutations("abc")))
def test_complete_linkage_never_combines_incompatible_pair(memory, embedder, order):
    scores = {frozenset("ab"): .8, frozenset("ac"): .8, frozenset("bc"): .4}
    embedder.cosine_similarity = lambda a, b: scores[frozenset((a, b))]
    groups = merge.find_near_duplicate_groups([memory(c) for c in order], embedder, .6)
    assert sorted(i for group in groups for i in group) == [0, 1, 2]
    for group in groups:
        assert not {"b", "c"} <= {order[i] for i in group}


@pytest.mark.parametrize("content,expected", [
    ("[auto] `pytest` failed (exit 1): auth error", "auth error"),
    ("[auto] `python -m pytest` failed (exit -9): terminated", "terminated"),
    ("manual error", "manual error"),
    ("[auto] `pytest` failed (exit 1): ", "[auto] `pytest` failed (exit 1): "),
    ("prefix [auto] `pytest` failed (exit 1): bad", "prefix [auto] `pytest` failed (exit 1): bad"),
])
def test_boilerplate_is_removed_only_for_embedding(memory, embedder, content, expected):
    original = memory(content)
    merge.find_near_duplicate_groups([original], embedder, .6)
    assert embedder.batches == [[expected]]
    assert original.content == content


def test_different_commands_same_symptom_merge_without_losing_evidence(memory, embedder):
    contents = ["[auto] `pytest` failed (exit 1): missing module", "[auto] `python` failed (exit 2): missing module"]
    result = merge.dedupe_and_merge([memory(c) for c in contents], embedder=embedder, use_llm=False)
    assert len(result) == 1
    assert all(c in result[0].content for c in contents)


@pytest.mark.parametrize("provenances", [("inferred", "inferred"), ("explicit", "inferred"),
                                        ("inferred", "explicit"), ("explicit", "explicit")])
def test_merge_preserves_dates_max_score_and_all_source_ids(memory, now, provenances):
    old = memory("old evidence", created_at=now - timedelta(days=30),
                 last_accessed_at=now - timedelta(days=30), importance=.3, provenance=provenances[0])
    recent = memory("new evidence", tag="T-2", importance=.8, provenance=provenances[1])
    result = merge.merge_group([old, recent])
    assert result.content == "new evidence\n---\nold evidence"
    assert result.created_at == old.created_at and result.last_accessed_at == now
    assert result.importance == .8 and result.tag == "T-2"
    assert set(result.merged_from) == {old.id, recent.id}
    assert result.provenance == ("explicit" if "explicit" in provenances else "inferred")
    assert old.content == "old evidence" and recent.content == "new evidence"


def test_identical_content_is_written_once_but_retains_two_event_sources(memory):
    result = merge.merge_group([memory("same", merged_from=["event1"]),
                                memory("same", merged_from=["event2"])])
    assert result.content == "same"
    assert result.merged_from == ["event1", "event2"]


@pytest.mark.parametrize("reply", [None, "", "   ", "\n\t", RuntimeError("offline"), TimeoutError("timeout")])
def test_unusable_merge_falls_back_to_all_source_facts(memory, reply):
    callback = Mock(side_effect=reply) if isinstance(reply, Exception) else Mock(return_value=reply)
    source = [memory("one"), memory("two")]
    result = merge.merge_group(source, llm_call=callback)
    assert result.content == "one\n---\ntwo"
    assert len(result.merged_from) == 2
    callback.assert_called_once()


@pytest.mark.parametrize("verdict,accepted", [("FAITHFUL", True), (" faithful \n", True),
    ("UNFAITHFUL", False), ("FAITHFUL but UNFAITHFUL", False), ("", False), (None, False),
    ("unknown", False), (TimeoutError("judge unavailable"), False)])
def test_judge_controls_synthesis_or_fallback(memory, verdict, accepted):
    judge = Mock(side_effect=verdict) if isinstance(verdict, Exception) else Mock(return_value=verdict)
    result = merge.merge_group([memory("one"), memory("two")],
                               llm_call=Mock(return_value=" unified facts \n"), judge_call=judge)
    assert result.content == ("unified facts" if accepted else "one\n---\ntwo")
    judge.assert_called_once()
    assert "one" in judge.call_args.args[0][0]["content"]
    assert "unified facts" in judge.call_args.args[0][0]["content"]


@pytest.mark.parametrize("use_llm", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("validate", [False, True])
@pytest.mark.parametrize("verdict", ["FAITHFUL", "UNFAITHFUL"])
def test_llm_and_validation_configuration_matrix(memory, embedder, monkeypatch, use_llm, enabled, validate, verdict):
    monkeypatch.setattr(config, "MERGE_LLM_ENABLED", enabled)
    monkeypatch.setattr(config, "MERGE_VALIDATION_ENABLED", validate)
    embedder.cosine_similarity = lambda *_: 1
    llm, judge = Mock(return_value="synthesis"), Mock(return_value=verdict)
    result = merge.dedupe_and_merge([memory("one"), memory("two")], embedder=embedder,
                                    llm_call=llm, judge_call=judge, use_llm=use_llm)
    attempted = use_llm and enabled
    accepted = attempted and (not validate or verdict == "FAITHFUL")
    assert result[0].content == ("synthesis" if accepted else "one\n---\ntwo")
    assert llm.call_count == int(attempted)
    assert judge.call_count == int(attempted and validate)


def test_singleton_never_invokes_supplied_llm(memory):
    llm = Mock(side_effect=AssertionError("unnecessary call"))
    result = merge.merge_group([memory()], llm_call=llm)
    assert result.content == "Use pytest fixtures."
    llm.assert_not_called()


def test_injected_llm_receives_only_source_content(memory):
    llm = Mock(return_value="combined")
    merge.merge_group([memory("alpha"), memory("beta")], llm_call=llm)
    messages = llm.call_args.args[0]
    assert len(messages) == 1 and messages[0]["role"] == "user"
    assert "alpha" in messages[0]["content"] and "beta" in messages[0]["content"]


def test_empty_input_does_not_construct_services():
    assert merge.dedupe_and_merge([], use_llm=True) == []
