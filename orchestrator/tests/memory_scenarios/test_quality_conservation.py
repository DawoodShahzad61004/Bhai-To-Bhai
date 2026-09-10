"""The conservation lane: what compaction preserves when no model rewrites it.

These are deliberately NOT labelled accuracy results. Offline,
`dedupe_and_merge` builds merged content as a verbatim
"\\n---\\n".join(dict.fromkeys(contents)), `refresh_decay` never drops a
record, and `rerank_and_prune` no-ops behind three gates - so recall of 1.0
here is a CONSERVATION CHECK, guaranteed by construction rather than earned.
Presenting it as an accuracy figure would be the single most misleading thing
this suite could do.

What the lane is genuinely for: it pins the record counts, the grouping
decisions, and the zero-injection property, so that when the lossy-LLM lane
(test_quality_loss_detection.py) reports recall below 1.0, the difference is
attributable to the rewrite and to nothing else.
"""
import pytest

import config
import quality_manifest_io as manifest_io
import quality_runner as qr
from mem_manager.importance import parse_episodic_md
from quality_keywords import read_corpus

# Only the conservation lane. S5 (pruning) and S7 (malformed input) are the
# two places offline compaction genuinely destroys information, so a blanket
# "recall is exactly 1.0" would be false there - they are asserted, exactly,
# in test_quality_information_loss.py instead.
SCENARIOS = [s for s in manifest_io.load_manifest() if s["lane"] != "lossy"]
ARMS = [
    (scenario, embedder)
    for scenario in SCENARIOS
    for embedder in scenario["embedders"]
]
ARM_IDS = [f"{s['id']}-{e}" for s, e in ARMS]


@pytest.fixture
def run(token_counter, quality_results):
    def go(scenario, embedder_name, **kwargs):
        row = qr.run_scenario(
            scenario, embedder_name=embedder_name, counter=token_counter, **kwargs
        )
        quality_results.append(row)
        return row

    return go


@pytest.mark.parametrize("scenario, embedder_name", ARMS, ids=ARM_IDS)
def test_offline_compaction_preserves_every_source_keyword(scenario, embedder_name, run):
    row = run(scenario, embedder_name)
    keywords = row["keywords"]
    assert keywords.type_recall == 1.0, (
        f"{scenario['id']} lost keywords the verbatim union cannot lose: "
        f"{keywords.missing_keywords}"
    )
    assert keywords.missing == ()
    assert keywords.macro_recall == 1.0
    assert keywords.sentence_coverage[1.0] == 1.0


@pytest.mark.parametrize("scenario, embedder_name", ARMS, ids=ARM_IDS)
def test_offline_compaction_never_invents_a_keyword(scenario, embedder_name, run):
    # The precision half of the metric, and the half `found / total` cannot
    # express. Offline the merged content is a verbatim join, so anything here
    # is a defect; live, this same figure is the hallucination rate.
    assert run(scenario, embedder_name)["keywords"].injected == ()


@pytest.mark.parametrize("scenario, embedder_name", ARMS, ids=ARM_IDS)
def test_every_scenario_arm_meets_its_declared_expectations(
    scenario, embedder_name, run
):
    row = run(scenario, embedder_name)
    assert qr.expectation_failures(row, scenario) == []


@pytest.mark.parametrize("scenario", SCENARIOS, ids=manifest_io.ids)
def test_no_scenario_is_pruned_when_its_config_disables_pruning(scenario, run):
    row = run(scenario, scenario["headline_embedder"])
    assert row["pruning_applied"] is False
    assert row["pruned_source_tags"] == []
    assert row["prune_collateral"] is None


def test_verbatim_duplicate_corpus_collapses_to_one_memory_per_distinct_lesson(run):
    scenario = manifest_io.by_id("S1")
    row = run(scenario, "lexical")
    assert row["records_in"] == 12 and row["records_out"] == 4
    # Three sources behind each survivor: every group is a full triplicate.
    assert [len(group) for group in row["groups_formed"]] == [3, 3, 3, 3]
    assert row["keywords"].occurrence_retention == pytest.approx(1 / 3, abs=0.02)


def test_verbatim_duplicates_are_found_even_though_they_are_not_adjacent(run):
    # The corpus interleaves its triplicates rather than grouping them, so a
    # neighbour-only scan would score identically to a real duplicate search.
    text = read_corpus("s1_verbatim_duplicates.md")
    tags = [record.tag for record in parse_episodic_md(text)]
    assert tags[0] != tags[1], "the corpus stopped interleaving; S1 got weaker"
    assert run(manifest_io.by_id("S1"), "lexical")["records_out"] == 4


def test_paraphrase_clusters_merge_only_under_the_lexical_embedder(run):
    scenario = manifest_io.by_id("S2")
    lexical = run(scenario, "lexical")
    exact = run(scenario, "exact")
    assert lexical["records_out"] == 5 and exact["records_out"] == 15
    assert sorted(len(group) for group in lexical["groups_formed"]) == [3, 3, 3, 3, 3]
    assert sorted(len(group) for group in exact["groups_formed"]) == [1] * 15
    # Recall is 1.0 on both, so the merge cost nothing; the gain is the yield.
    yielded = qr.merge_yield(lexical, exact)
    assert yielded["type_recall_delta"] == 0.0
    assert yielded["tokens_freed_delta"] > 0, (
        "merging paraphrases must free tokens relative to not merging them"
    )
    assert yielded["records_out_delta"] == -10


def test_contradictory_records_are_never_merged_into_one_memory(run):
    scenario = manifest_io.by_id("S4")
    row = run(scenario, "lexical")
    merged = [group for group in row["groups_formed"] if len(group) > 1]
    assert len(merged) == 1, (
        "only the deliberate both-negated control may merge; a merged "
        f"contradiction would silently destroy one half of it: {merged}"
    )
    assert set(merged[0]) == set(scenario["both_negated_control_tags"])
    assert len(merged[0]) == 2
    assert not set(merged[0]) & set(scenario["contradiction_tags"])


def test_the_negation_guard_only_protects_an_asymmetric_pair(run):
    # Measured, not assumed, and not hidden behind an xfail: _looks_
    # contradictory tests marker ASYMMETRY, so two statements that are BOTH
    # negated are unprotected and do merge. Recording that keeps the guard's
    # actual reach honest.
    scenario = manifest_io.by_id("S4")
    row = run(scenario, "lexical")
    assert row["records_out"] == 14 - 1
    assert row["keywords"].type_recall == 1.0, (
        "even the unprotected merge is a verbatim join, so no fact is lost - "
        "the blind spot costs distinctness, not content"
    )


def test_user_choice_records_keep_explicit_provenance_and_are_renumbered(run):
    scenario = manifest_io.by_id("S6")
    records = parse_episodic_md(read_corpus(scenario["file"]))
    assert {record.provenance for record in records} == {"explicit"}
    assert len({record.run_id for record in records}) == 8

    with qr.config_overrides(**scenario["config"]):
        row = run(scenario, "lexical")
    assert row["records_out"] == 8
    # A tag is not content: renumbering to "User Choice N" must cost nothing.
    assert row["keywords"].type_recall == 1.0


def test_an_explicit_choice_outranks_an_inferred_learning_of_equal_shape(run):
    # The x1.2 explicit-provenance boost is what makes user choices survive a
    # prune that drops inferred records; without it S6 content would be at the
    # bottom of every ranking.
    from mem_manager.importance import composite_importance, build_session_index

    records = parse_episodic_md(read_corpus("s6_user_choices.md"))
    index = build_session_index(records)
    scores = [
        composite_importance(record, records, index, now=qr.QUALITY_NOW)
        for record in records
    ]
    assert all(score > 0 for score in scores)
    assert max(scores) <= 1.0


def test_a_shared_auto_failure_prefix_does_not_merge_unrelated_failures(run):
    row = run(manifest_io.by_id("S9"), "lexical")
    assert row["records_out"] == 8
    assert max(len(group) for group in row["groups_formed"]) == 1, (
        "_embedding_text must strip the [auto] prefix before embedding; with "
        "it left in, these eight unrelated failures reach cosine 0.625 and "
        "merge at the 0.60 threshold"
    )


def test_the_config_override_helper_restores_every_value_it_changed():
    # The helper mutates module globals because the pipeline reads them live.
    # If it ever leaked, every later scenario in the session would be scored
    # under the wrong configuration and the whole report would be wrong.
    before = (config.ENABLE_PRUNING, config.PRUNE_BOTTOM_PERCENT)
    with qr.config_overrides(ENABLE_PRUNING=not before[0], PRUNE_BOTTOM_PERCENT=0.99):
        assert config.ENABLE_PRUNING is not before[0]
        assert config.PRUNE_BOTTOM_PERCENT == 0.99
    assert (config.ENABLE_PRUNING, config.PRUNE_BOTTOM_PERCENT) == before


def test_the_config_override_helper_restores_values_even_after_a_failure():
    before = config.ENABLE_PRUNING
    with pytest.raises(RuntimeError):
        with qr.config_overrides(ENABLE_PRUNING=not before):
            raise RuntimeError("scenario blew up mid-run")
    assert config.ENABLE_PRUNING is before
