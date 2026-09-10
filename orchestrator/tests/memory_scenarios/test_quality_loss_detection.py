"""Where the accuracy metric earns its keep: an LLM rewriting the content.

`docs/Research.md` topic 59 and `docs/Bugs.md` #57's diagnosis both record the
same gap - "live model output quality was not evaluated by this suite, so
these are structural/logic gaps, not model-quality findings". `merge_group`'s
LLM rewrite is the one place in the pipeline where content is REGENERATED
rather than concatenated, and therefore the one place a fact can vanish or be
invented without any record count changing.

These tests run that path with deterministic synthetic rewriters, free and in
CI, and they assert the metric DETECTS what those rewriters do. A measurement
that has only ever returned 1.0 has not been shown to work; that is the whole
reason this file exists rather than resting on the conservation lane.

The same code path takes the real provider when MEM_QUALITY_LIVE=1. Nothing
is skipped when it is unset, so there is no silent hole in the suite.
"""
import pytest

import quality_llms as llms
import quality_manifest_io as manifest_io
import quality_runner as qr

# Corpora that actually merge under the lexical embedder - a rewrite can only
# lose something where there is a group of more than one to rewrite.
MERGING = ["S1", "S2"]


@pytest.fixture
def run_with(token_counter, quality_results):
    def go(scenario_id, calls, *, embedder_name="lexical"):
        scenario = manifest_io.by_id(scenario_id)
        row = qr.run_scenario(
            scenario,
            embedder_name=embedder_name,
            counter=token_counter,
            llm_calls=calls.as_pair(),
            use_llm=True,
        )
        row["lane"] = calls.name
        row["llm_transcript"] = calls.transcript
        row["judge_verdicts"] = dict(calls.verdicts)
        quality_results.append(row)
        return row

    return go


@pytest.mark.parametrize("scenario_id", MERGING)
def test_a_faithful_rewrite_keeps_recall_at_one(scenario_id, run_with):
    # The control. If this ever fails, a later failure cannot be attributed to
    # the rewrite being lossy rather than to the harness being broken.
    row = run_with(scenario_id, llms.faithful_merge_calls())
    assert row["keywords"].type_recall == 1.0
    assert row["keywords"].injected == ()
    assert row["judge_verdicts"]["accepted"] > 0


@pytest.mark.parametrize("scenario_id", MERGING)
def test_a_lossy_rewrite_is_reported_as_missing_keywords(scenario_id, run_with):
    # THE test this suite exists for. The rewriter drops a whole source entry
    # and the judge waves it through, which is precisely the silent loss a
    # memory system must never suffer - and the metric has to see it.
    lossy = run_with(scenario_id, llms.lossy_merge_calls())
    assert lossy["keywords"].type_recall < 1.0, (
        "an LLM dropped an entire source entry and the metric reported no "
        "loss - the measurement is not working"
    )
    assert lossy["keywords"].missing_keywords
    assert lossy["keywords"].rare_recall is None or lossy["keywords"].rare_recall < 1.0


@pytest.mark.parametrize("scenario_id", MERGING)
def test_a_lossy_rewrite_names_which_records_lost_what(scenario_id, run_with):
    # A recall number nobody can act on is not much use; the report has to say
    # which source sentence each missing keyword came from.
    row = run_with(scenario_id, llms.lossy_merge_calls())
    assert row["keywords"].missing
    entry = row["keywords"].missing[0]
    assert entry.record_tag and entry.sentence_index >= 0
    assert entry.keyword in row["keywords"].missing_keywords


@pytest.mark.parametrize("scenario_id", MERGING)
def test_an_inventing_rewrite_is_reported_as_injected_keywords(scenario_id, run_with):
    # The precision half. `found / total` is recall and is structurally blind
    # to this, yet a fabricated instruction in durable memory is worse than a
    # dropped one - it will be acted on.
    row = run_with(scenario_id, llms.inventing_merge_calls())
    assert row["keywords"].injected, (
        "the rewrite added a sentence that appears nowhere in the source and "
        "nothing reported it"
    )
    assert "zeppelin" in row["keywords"].injected
    # Recall is unaffected: nothing was lost, something was added. Reporting
    # only recall would call this a perfect result.
    assert row["keywords"].type_recall == 1.0


@pytest.mark.parametrize("scenario_id", MERGING)
def test_a_rejecting_judge_restores_full_recall_after_a_lossy_rewrite(
    scenario_id, run_with
):
    # What the exact-"FAITHFUL" gate at dedup_merge.py:183 is actually worth,
    # measured rather than assumed: the same lossy rewrite, refused, falls back
    # to the verbatim union and loses nothing.
    lossy = run_with(scenario_id, llms.lossy_merge_calls())
    guarded = run_with(scenario_id, llms.lossy_merge_calls_with_honest_judge())
    assert lossy["keywords"].type_recall < 1.0
    assert guarded["keywords"].type_recall == 1.0
    assert guarded["judge_verdicts"]["rejected"] > 0
    assert guarded["judge_verdicts"]["accepted"] == 0


def test_an_ambiguous_verdict_is_refused_rather_than_read_as_approval(run_with):
    # merge_group compares the verdict for exact equality, so prose that merely
    # CONTAINS "FAITHFUL" is refused. That is the difference between a judge and
    # a substring search, and it is worth a number.
    row = run_with("S1", llms.ambiguous_verdict_calls())
    assert row["judge_verdicts"]["unparseable"] > 0
    assert row["judge_verdicts"]["accepted"] == 0
    assert row["keywords"].type_recall == 1.0, (
        "an unparseable verdict must fall back to the verbatim union"
    )


def test_a_rewrite_that_loses_content_still_frees_more_tokens_than_a_faithful_one(
    run_with,
):
    # The reason recall is never reported alone. The lossy rewrite looks BETTER
    # on savings precisely because it threw information away; only the pair of
    # figures tells the truth about it.
    faithful = run_with("S1", llms.faithful_merge_calls())
    lossy = run_with("S1", llms.lossy_merge_calls())
    assert lossy["tokens"].freed > faithful["tokens"].freed
    assert lossy["keywords"].type_recall < faithful["keywords"].type_recall


def test_no_llm_is_consulted_when_no_group_has_more_than_one_member(run_with):
    # S3 is all singletons, and merge_group returns a singleton unchanged
    # without calling anything. A rewriter that was invoked here would mean the
    # pipeline is paying for calls it does not need.
    calls = llms.faithful_merge_calls()
    row = run_with("S3", calls)
    assert calls.transcript == []
    assert row["keywords"].type_recall == 1.0


def test_the_live_provider_is_unreachable_from_pytest_even_when_enabled(
    monkeypatch, token_counter
):
    # The live lane genuinely cannot run under pytest, and that is by design
    # rather than an oversight. conftest's autouse `offline` fixture replaces
    # dedup_merge._default_llm_calls with a tripwire that raises, so even with
    # MEM_QUALITY_LIVE=1 the suite cannot reach a paid API. Asserting the
    # tripwire is a stronger guarantee than asserting a measurement.
    #
    # The real live measurement therefore runs OUTSIDE pytest, through
    # quality_live.py, where no fixture is patching anything.
    monkeypatch.setenv(llms.LIVE_ENV_VAR, "1")
    assert llms.live_enabled() is True
    with pytest.raises(AssertionError, match="live LLM client"):
        llms.live_merge_calls()


def test_the_live_gate_needs_both_the_flag_and_the_environment_variable(monkeypatch):
    monkeypatch.delenv(llms.LIVE_ENV_VAR, raising=False)
    assert llms.live_enabled() is False
    assert llms.live_merge_calls() is None
    monkeypatch.setenv(llms.LIVE_ENV_VAR, "0")
    assert llms.live_merge_calls() is None


def test_the_default_llm_path_stays_a_tripwire_even_with_the_variable_set(
    monkeypatch, token_counter
):
    # The structural CI safety proof. conftest's autouse `offline` fixture
    # replaces dedup_merge._default_llm_calls with something that raises, and
    # dedupe_and_merge swallows the exception and falls back to the
    # deterministic union. So a merging corpus run with use_llm=True and no
    # injected calls reaches no network, whatever the environment says.
    monkeypatch.setenv(llms.LIVE_ENV_VAR, "1")
    row = qr.run_scenario(
        manifest_io.by_id("S1"),
        embedder_name="lexical",
        counter=token_counter,
        llm_calls=None,
        use_llm=True,
    )
    assert row["records_out"] == 4
    assert row["keywords"].type_recall == 1.0
