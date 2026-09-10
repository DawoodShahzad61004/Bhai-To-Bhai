"""Token accounting: what compaction actually costs and saves.

`mem_manager` counts tokens nowhere - every size decision it makes is in
characters - so these are the first token figures the project has for the
compaction path. They are measured on FILE text, markers included, because
the markers are real: `_memories_to_markdown` writes four or five
`<!-- key:value -->` lines per memory, and on a log with little to merge that
overhead is larger than anything compaction removes.

`docs/Bugs.md` #52 (unbounded shared-log growth) is open and deliberately
deferred. test_a_corpus_with_nothing_to_merge_gets_bigger_not_smaller is that
entry as a number.
"""
import pytest

import quality_manifest_io as manifest_io
import quality_runner as qr
import quality_tokens as qt
from quality_keywords import read_corpus

SCENARIOS = [s for s in manifest_io.load_manifest() if s["lane"] != "lossy"]
ARMS = [(s, e) for s in SCENARIOS for e in s["embedders"]]
ARM_IDS = [f"{s['id']}-{e}" for s, e in ARMS]


@pytest.fixture
def row(token_counter, quality_results):
    def go(scenario_id, embedder_name):
        result = qr.run_scenario(
            manifest_io.by_id(scenario_id),
            embedder_name=embedder_name,
            counter=token_counter,
        )
        quality_results.append(result)
        return result

    return go


def test_verbatim_duplicate_corpus_frees_tokens_without_losing_keywords(row):
    # The only shape in this corpus set where compaction actually pays: many
    # verbatim repeats of bodies long enough to outweigh the markers.
    result = row("S1", "lexical")
    assert result["tokens"].freed > 0
    assert result["tokens"].freed_pct >= 0.25
    assert result["keywords"].type_recall == 1.0


def test_a_corpus_with_nothing_to_merge_gets_bigger_not_smaller(row):
    # Asserting the SIGN is the strongest guard available against a regression
    # that deletes records to look efficient: a module that quietly dropped
    # memories would show a saving here, and this test would fail.
    result = row("S3", "lexical")
    assert result["records_out"] == result["records_in"]
    assert result["tokens"].freed < 0
    assert result["keywords"].type_recall == 1.0, (
        "the file grew AND nothing was lost - the growth is pure metadata"
    )


def test_refusing_to_merge_turns_the_one_real_saving_into_a_loss(row):
    # The control arm that makes every other number interpretable. Same
    # corpus, same pipeline, only the embedder differs.
    merging = row("S1", "lexical")
    not_merging = row("S1", "noop")
    assert merging["tokens"].freed > 0 > not_merging["tokens"].freed
    assert merging["keywords"].type_recall == not_merging["keywords"].type_recall == 1.0


@pytest.mark.parametrize("scenario, embedder_name", ARMS, ids=ARM_IDS)
def test_the_marker_overhead_accounts_for_the_whole_gap(scenario, embedder_name, row):
    # after == content + markers, exactly. If this ever drifts, a "saving"
    # could be serialization changing shape rather than information going away.
    tokens = row(scenario["id"], embedder_name)["tokens"]
    assert tokens.after == tokens.after_content_only + tokens.marker_overhead
    assert tokens.marker_overhead > 0
    assert tokens.freed == tokens.before - tokens.after


@pytest.mark.parametrize("scenario, embedder_name", ARMS, ids=ARM_IDS)
def test_every_token_figure_records_the_counter_that_produced_it(
    scenario, embedder_name, row
):
    # A tokens_freed figure is meaningless without its counter: two counters
    # disagree by more than twofold on marker-heavy markdown.
    assert row(scenario["id"], embedder_name)["tokens"].counter_name == "regex-approx-v1"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=manifest_io.ids)
def test_two_counters_agree_on_the_direction_if_not_the_magnitude(scenario):
    # Only the sign is portable. Asserting equal magnitudes would pin this
    # suite to one counter's arithmetic instead of to the pipeline's behaviour.
    regex = qr.run_scenario(
        scenario,
        embedder_name=scenario["headline_embedder"],
        counter=qt.resolve_token_counter("regex"),
    )["tokens"]
    chars = qr.run_scenario(
        scenario,
        embedder_name=scenario["headline_embedder"],
        counter=qt.resolve_token_counter("chars"),
    )["tokens"]
    assert (regex.freed > 0) == (chars.freed > 0)
    assert regex.counter_name != chars.counter_name


def test_the_regex_counter_charges_more_for_a_marker_line_than_chars_over_four():
    # Why the default is not chars/4: a marker line is almost entirely
    # punctuation, and chars/4 under-counts exactly the overhead being
    # measured, making compaction look better than it is.
    marker = "<!-- last_accessed_at:2026-09-08 12:00:00Z -->"
    assert qt.regex_approx_count(marker) > qt.chars_div_four_count(marker)


def test_an_unavailable_named_counter_is_refused_rather_than_downgraded():
    # A token figure whose counter silently changed is a number nobody can
    # interpret later, so asking for tiktoken by name must fail loudly here.
    try:
        counter = qt.resolve_token_counter("tiktoken")
    except qt.TokenizerUnavailableError as error:
        assert "tiktoken" in str(error)
    else:
        assert counter.name == "tiktoken:cl100k_base"


def test_an_unknown_counter_name_is_rejected():
    with pytest.raises(ValueError, match="unknown tokenizer"):
        qt.resolve_token_counter("bpe-guess")


@pytest.mark.parametrize(
    "text", ["", "one", "a" * 40, "\n\n\n", "<!-- id:abc -->", "日本語"],
    ids=["empty", "word", "long-run", "newlines", "marker", "cjk"],
)
def test_the_counter_is_total_and_never_negative(text):
    assert qt.regex_approx_count(text) >= 0
    assert qt.chars_div_four_count(text) >= 0


@pytest.mark.parametrize("scenario", SCENARIOS, ids=manifest_io.ids)
def test_the_recorded_corpus_is_what_the_measurement_reads(scenario):
    # utf-8-sig on the read side, matching compact_markdown_file. Reading a
    # BOM as plain utf-8 attaches it to the first header and silently costs
    # that record - which would move both recall and tokens.
    text = read_corpus(scenario["file"])
    assert not text.startswith("﻿")
    assert text.lstrip().startswith("<!--") or text.lstrip().startswith("## ")
