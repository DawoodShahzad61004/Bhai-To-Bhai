"""The two places offline compaction genuinely destroys information.

Everything in test_quality_conservation.py scores a perfect 1.0 because the
offline merge is a verbatim join. These do not, and that is the point: a
metric that has never returned less than 1.0 has not been shown to work.

  S5  pruning drops the bottom of the importance ranking, and most of what it
      drops survives nowhere else.
  S7  a malformed record between two genuine ones is discarded with no error,
      and scoring on the parsed denominator reports a flawless 1.0 while it
      happens.

S8 sits here too, as the negative control for both: recompacting the same file
five times must NOT erode it.
"""
import math

import pytest

import config
import quality_keywords as qk
import quality_manifest_io as manifest_io
import quality_metrics as qm
import quality_runner as qr
from mem_manager.importance import parse_episodic_md

S5 = manifest_io.by_id("S5")
S7 = manifest_io.by_id("S7")
S8 = manifest_io.by_id("S8")


@pytest.fixture
def run(token_counter, quality_results):
    def go(scenario, embedder_name, **kwargs):
        row = qr.run_scenario(
            scenario, embedder_name=embedder_name, counter=token_counter, **kwargs
        )
        quality_results.append(row)
        return row

    return go


# --- S5: pruning -------------------------------------------------------------


@pytest.mark.parametrize("embedder_name", S5["embedders"])
def test_pruning_actually_fires_on_the_pruning_scenario(embedder_name, run):
    # Asserted rather than assumed. The gate is sum(len(m.content)) >=
    # MIN_PRUNE_BUDGET on POST-MERGE content characters, so a corpus that
    # merges more than expected slips under the budget and the whole scenario
    # silently measures nothing at all.
    row = run(S5, embedder_name)
    assert row["pruning_applied"] is True
    assert row["records_after_merge"] == S5["expected_after_merge"][embedder_name]
    assert row["records_out"] == S5["expect_by_embedder"][embedder_name]["records_out"]


@pytest.mark.parametrize("embedder_name", S5["embedders"])
def test_pruning_drops_exactly_the_records_at_the_bottom_of_the_ranking(
    embedder_name, run
):
    # Exact, not approximate: `now` is frozen, so recency, decay and therefore
    # the whole importance ordering are deterministic.
    row = run(S5, embedder_name)
    assert row["pruned_source_tags"] == S5["expected_pruned_source_tags"][embedder_name]


@pytest.mark.parametrize("embedder_name", S5["embedders"])
def test_pruning_drops_the_count_the_prune_fraction_declares(embedder_name, run):
    row = run(S5, embedder_name)
    expected = math.floor(row["records_after_merge"] * S5["config"]["PRUNE_BOTTOM_PERCENT"])
    assert row["records_after_merge"] - row["records_out"] == expected


def test_pruning_costs_keywords_that_survive_nowhere_else(run):
    # THE measurement this whole suite exists for, on the one offline path that
    # can move it. A pruned record whose facts are duplicated elsewhere is a
    # good prune; one whose facts existed only there is real loss.
    row = run(S5, "lexical")
    keywords = row["keywords"]
    assert keywords.type_recall < 1.0, (
        "pruning removed records but no keyword went missing - either the "
        "corpus stopped carrying unique facts in its low-importance tail, or "
        "the metric has stopped detecting loss"
    )
    assert keywords.missing_keywords
    assert 0.0 < row["prune_collateral"] <= 1.0
    assert row["orphaned_keywords"]
    # Every orphan is a keyword that was in the source and is not in the output.
    assert set(row["orphaned_keywords"]) <= set(keywords.missing_keywords)


def test_a_pruning_loss_is_reported_against_the_records_it_came_from(run):
    # The report has to name what was lost and where, or nobody can act on it.
    row = run(S5, "lexical")
    pruned = set(row["pruned_source_tags"])
    assert {entry.record_tag for entry in row["keywords"].missing} <= pruned


def test_sentence_coverage_falls_further_than_type_recall_under_pruning(run):
    # Type recall is a keyword-set measure and hides a wholly-lost statement
    # whose individual words recur elsewhere. Coverage at threshold 1.0 counts
    # sentences that survived intact, and is the stricter view of the same loss.
    keywords = run(S5, "lexical")["keywords"]
    assert keywords.sentence_coverage[1.0] < keywords.type_recall
    assert keywords.sentence_coverage[1.0] <= keywords.sentence_coverage[0.8]
    assert keywords.sentence_coverage[0.8] <= keywords.sentence_coverage[0.5]


@pytest.mark.parametrize("fraction", [0.0, 0.1, 0.2, 0.4, 0.6], ids=lambda f: f"pct{f}")
def test_a_larger_prune_fraction_never_retains_more_records(fraction, token_counter):
    # The sweep is a figure, not the finding: pruning drops floor(n x fraction)
    # BY RANK, so recall against the fraction is a step function determined
    # entirely by the importance ordering. What is worth asserting is that the
    # count is exactly what the fraction declares and that it is monotone.
    scenario = dict(S5, config=dict(S5["config"], PRUNE_BOTTOM_PERCENT=fraction))
    row = qr.run_scenario(scenario, embedder_name="lexical", counter=token_counter)
    assert row["records_out"] == row["records_after_merge"] - math.floor(
        row["records_after_merge"] * fraction
    )
    assert (row["prune_collateral"] is None) == (row["records_out"] == row["records_after_merge"])


def test_pruning_is_skipped_entirely_below_the_character_budget(token_counter):
    # The budget is characters of post-merge content, and the same constant is
    # compared against file SIZE in compact_command.should_auto_compact - one
    # knob, two different measures. Raising it past the corpus disables pruning
    # without touching ENABLE_PRUNING.
    scenario = dict(S5, config=dict(S5["config"], MIN_PRUNE_BUDGET=10_000_000))
    row = qr.run_scenario(scenario, embedder_name="lexical", counter=token_counter)
    assert row["pruning_applied"] is False
    assert row["keywords"].type_recall == 1.0


def test_an_invalid_prune_fraction_is_rejected_before_anything_is_deleted():
    from mem_manager.consolidate import rerank_and_prune

    with qr.config_overrides(ENABLE_PRUNING=True):
        with pytest.raises(ValueError, match="prune_fraction"):
            rerank_and_prune([], prune_fraction=1.5)


# --- S7: silently dropped input ----------------------------------------------


def _orphans_of(corpus_name, tags):
    """Keywords contributed ONLY by the named records."""
    golden = qk.load_golden(corpus_name)
    theirs = qk.golden_keywords_for_records(golden, tags)
    others = {
        keyword
        for entry in golden["sentences"]
        if entry["record_tag"] not in set(tags)
        for keyword in entry["keywords"]
    }
    return theirs - others


def test_scoring_on_the_parsed_denominator_hides_the_dropped_record(token_counter):
    # The single most important methodological point in the suite. Same corpus,
    # same run, two denominators: one reports perfection, the other reports the
    # truth. Derive the source sentences from parse_episodic_md and a dropped
    # record leaves the denominator along with the numerator.
    on_parsed = qr.run_scenario(
        dict(S7, denominator="parsed"), embedder_name="lexical", counter=token_counter
    )
    on_raw = qr.run_scenario(
        dict(S7, denominator="raw"), embedder_name="lexical", counter=token_counter
    )
    assert on_parsed["keywords"].type_recall == 1.0
    assert on_raw["keywords"].type_recall < 1.0
    assert on_parsed["keywords"].missing == ()
    assert on_raw["keywords"].missing_keywords


def test_the_adversarial_corpus_loses_exactly_the_dropped_records_keywords(run):
    # Exact set equality, drawn from the recorded goldens rather than from a
    # fraction somebody chose. It self-updates if the corpus changes, and it
    # fails loudly if the parser starts dropping something else.
    row = run(S7, "lexical")
    assert set(row["keywords"].missing_keywords) == _orphans_of(
        S7["file"], S7["expected_dropped_tags"]
    )


def test_a_malformed_record_between_genuine_ones_is_dropped_without_an_error(run):
    text = qk.read_corpus(S7["file"])
    parsed_tags = {record.tag for record in parse_episodic_md(text)}
    raw_tags = {tag for tag, _ in qk._raw_blocks(text)}
    assert set(S7["expected_dropped_tags"]) <= raw_tags
    assert not set(S7["expected_dropped_tags"]) & parsed_tags
    assert run(S7, "lexical")["records_out"] == S7["expect_by_embedder"]["lexical"][
        "records_out"
    ]


def test_a_trailing_pseudo_header_is_folded_into_the_record_above_it(run):
    # The other half of the position rule: after the last genuine record, an
    # invalid header costs nothing, because its text is appended rather than
    # discarded. Its keywords must therefore NOT be missing.
    row = run(S7, "lexical")
    folded = _orphans_of(S7["file"], S7["expected_folded_tags"])
    assert folded, "the folded block stopped carrying any unique keyword"
    assert not folded & set(row["keywords"].missing_keywords)


def test_a_valid_header_inside_a_code_fence_becomes_a_real_memory_record(run):
    # Found by measurement, not predicted: the block split is textual, not
    # markdown-aware, so a documentation example inside a fence is promoted
    # into a memory of its own. No content is lost - a record is invented -
    # so it is recorded as behaviour rather than asserted away.
    parsed_tags = [record.tag for record in parse_episodic_md(qk.read_corpus(S7["file"]))]
    assert set(S7["expected_phantom_tags"]) <= set(parsed_tags)
    assert run(S7, "lexical")["keywords"].injected == ()


def test_a_leading_byte_order_mark_does_not_cost_the_first_record(run):
    # Only compact_markdown_file reads utf-8-sig. Read as plain utf-8 the BOM
    # attaches to the first header, fails validation, and silently costs that
    # record - so the runner goes through the file API, and this pins it.
    assert qk.corpus_path(S7["file"]).read_bytes().startswith(b"\xef\xbb\xbf")
    first = _orphans_of(S7["file"], ["T-701"])
    assert first, "T-701 stopped carrying a unique keyword"
    assert not first & set(run(S7, "lexical")["keywords"].missing_keywords)


def test_unicode_content_survives_the_round_trip(run):
    row = run(S7, "lexical")
    missing = set(row["keywords"].missing_keywords)
    non_ascii = {
        keyword
        for sentence in qk.sentences_for_markdown(
            qk.read_corpus(S7["file"]), denominator="raw"
        )
        for keyword in sentence.keywords
        if any(ord(character) > 127 for character in keyword)
    }
    assert non_ascii, "the adversarial corpus stopped exercising non-ASCII content"
    assert not non_ascii & missing


# --- S8: repeated compaction -------------------------------------------------


@pytest.fixture(scope="module")
def generations(request):
    import quality_tokens

    return qr.run_generations(
        S8,
        embedder_name="lexical",
        counter=quality_tokens.resolve_token_counter("regex"),
        generations=S8["generations"],
    )


def test_repeated_compaction_never_erodes_a_keyword(generations):
    # Measured against generation ZERO each time, never against the previous
    # generation: two percent lost per pass looks harmless next to its
    # predecessor and ruinous next to the source.
    recalls = [row["keywords"].type_recall for row in generations]
    assert recalls == [1.0] * len(recalls)
    assert all(row["keywords"].injected == () for row in generations)


def test_repeated_compaction_reaches_a_content_fixed_point(generations):
    # The fixed point exists only because build_durable_memories honours a
    # prior pass's `<!-- id: -->` marker. Without it, compact_markdown_file's
    # tag renumbering would change _record_id on every pass and the file would
    # never settle. Nothing else guards that end to end.
    texts = [row["text"] for row in generations]
    assert len(set(texts)) == 1, (
        "the file never settled; recompaction is still rewriting it, and the "
        "id marker that makes a memory recognizable across passes is not "
        "surviving the round trip"
    )
    # ...and the first pass did real work, so the fixed point is not the
    # trivial one of a pipeline that changes nothing.
    assert generations[0]["records_out"] < S8["expected_parsed_records"]


def test_repeated_compaction_stops_changing_the_record_count(generations):
    counts = [row["records_out"] for row in generations]
    assert counts[0] == S8["expect_by_embedder"]["lexical"]["records_out"]
    assert len(set(counts)) == 1, f"record count drifted across generations: {counts}"


def test_repeated_compaction_stops_adding_tokens_after_the_first_pass(generations):
    after = [row["tokens"].after for row in generations]
    assert after[1:] == sorted(after[1:], reverse=True) or len(set(after[1:])) == 1
    assert after[2] == after[3] == after[4]


def test_the_metric_refuses_to_score_against_an_empty_source():
    # A corpus that parsed to nothing would otherwise divide by zero and be
    # reported as a passing 1.0.
    with pytest.raises(ValueError, match="empty source"):
        qm.score_keywords([], "anything")


def test_collateral_is_none_rather_than_zero_when_nothing_was_pruned():
    # Reporting 0.0 would claim a measurement that was never taken.
    collateral, orphaned = qm.prune_collateral([], [])
    assert collateral is None and orphaned == ()
