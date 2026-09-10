"""The measurement apparatus itself, tested before anything depends on it.

A quality metric nobody has checked is a number, not evidence. These tests
pin the extractor's segmentation and matching rules, and hold the recorded
per-sentence keyword goldens in sync with the extractor that produced them -
so a change to extraction shows up as a reviewable JSON diff rather than as a
silently moving accuracy figure.

Regenerate the goldens with MEM_QUALITY_REGEN=1 (an environment variable
rather than a pytest flag: pytest_addoption is honoured only in an initial
conftest, and orchestrator/tests/conftest.py is shared with every other
suite).
"""
import json
import os

import pytest

import quality_keywords as qk
import quality_manifest_io as manifest_io

CORPORA = [scenario["file"] for scenario in manifest_io.load_manifest()]


@pytest.mark.parametrize("corpus_name", CORPORA)
def test_golden_keyword_files_match_the_current_extractor(corpus_name):
    rebuilt = qk.build_golden(corpus_name)
    if os.environ.get("MEM_QUALITY_REGEN") == "1":
        qk.write_golden(corpus_name)
    recorded = qk.load_golden(corpus_name)
    assert recorded["source_sha1"] == rebuilt["source_sha1"], (
        f"{corpus_name} was edited without regenerating its keyword golden; "
        "run with MEM_QUALITY_REGEN=1 and review the diff"
    )
    assert recorded["extractor_version"] == qk.EXTRACTOR_VERSION
    assert recorded["sentences"] == rebuilt["sentences"], (
        "the extractor changed; every recall figure in this suite moves with "
        "it, so regenerate deliberately and read the diff"
    )


@pytest.mark.parametrize("scenario", manifest_io.load_manifest(), ids=manifest_io.ids)
def test_every_corpus_file_parses_into_its_declared_record_count(scenario):
    from mem_manager.importance import parse_episodic_md

    text = qk.read_corpus(scenario["file"])
    assert len(list(qk._raw_blocks(text))) == scenario["expected_source_records"]
    assert len(parse_episodic_md(text)) == scenario["expected_parsed_records"]


@pytest.mark.parametrize("corpus_name", CORPORA)
def test_every_recorded_sentence_carries_at_least_one_keyword(corpus_name):
    # A keywordless sentence contributes nothing to recall but still divides
    # sentence_coverage, quietly deflating it. They are dropped at extraction;
    # this pins that they stay dropped.
    golden = qk.load_golden(corpus_name)
    assert all(entry["keywords"] for entry in golden["sentences"])


def test_a_fenced_code_block_stays_one_sentence_even_when_it_contains_a_header():
    body = "Before.\n\n```\n## 2026-09-08 12:00:00Z - not a record\nvalue = f(1.5)\n```\n\nAfter."
    units = qk.split_sentences(body)
    assert units[0] == "Before."
    assert units[-1] == "After."
    fenced = [unit for unit in units if unit.startswith("```")]
    assert len(fenced) == 1 and "## 2026-09-08" in fenced[0] and "f(1.5)" in fenced[0]


def test_a_list_marker_is_stripped_so_its_own_number_is_not_a_keyword():
    units = qk.split_sentences("1. Install `ruff` first.\n2. Then run it.")
    assert units == ["Install `ruff` first.", "Then run it."]
    assert "1" not in qk.extract_keywords(units[0])


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Use e.g. this one. Then stop.", 2),
        ("Version 1.5 shipped. It works.", 2),
        ("Ask J. Smith about it. Later.", 2),
    ],
    ids=["abbreviation", "decimal", "initial"],
)
def test_a_period_that_does_not_end_a_sentence_does_not_split_one(text, expected):
    assert len(qk.split_sentences(text)) == expected


def test_keyword_matching_is_token_based_so_a_shared_prefix_is_not_a_match():
    # The failure this prevents: a deleted record's keywords still "matching"
    # because a surviving record happens to share a prefix, which would make
    # the metric structurally unable to detect the loss it exists to detect.
    tokens = qk.tokenize_for_match("the identifier and the running total")
    collapsed = qk.collapse_for_multiword("the identifier and the running total")
    assert not qk.matches("id", tokens, collapsed)
    assert not qk.matches("run", tokens, collapsed)
    assert qk.matches("identifier", tokens, collapsed)


def test_a_hyphenated_compound_word_is_not_read_as_a_command_line_flag():
    # Regression: "danger-full-access" once yielded a "-full-access" keyword,
    # inventing a term no writer wrote and inflating every denominator.
    keywords = qk.extract_keywords("Set it to danger-full-access when auto-rejected.")
    assert not any(keyword.startswith("-") for keyword in keywords)
    assert qk.extract_keywords("Pass `--json` to it.")[0] == "--json"


def test_a_trailing_call_parenthesis_never_hides_a_keyword():
    # An LLM reformatting compact_markdown() as compact_markdown changes
    # nothing about the remembered fact and must not read as a loss.
    tokens = qk.tokenize_for_match("compact_markdown is the entry point")
    assert qk.matches(qk.normalize("compact_markdown()"), tokens, "")
    assert qk.normalize("`compact_markdown()`,") == "compact_markdown"


@pytest.mark.parametrize(
    "keyword, text",
    [("fixture", "two fixtures here"), ("fixtures", "one fixture here")],
    ids=["singular-vs-plural", "plural-vs-singular"],
)
def test_the_one_plural_rule_matches_in_both_directions(keyword, text):
    assert qk.matches(keyword, qk.tokenize_for_match(text), text)


def test_a_short_word_is_not_pluralized_into_a_false_match():
    # The plural rule is deliberately narrow: without the length floor, "a"
    # would match "as", "id" would match "ids", and the rule would start
    # manufacturing matches instead of tolerating inflection.
    assert not qk.matches("bug", qk.tokenize_for_match("bugs everywhere"), "")


def test_the_raw_denominator_sees_every_visible_block_and_parsed_sees_fewer():
    # The asymmetry that makes silent record loss measurable at all. A block
    # with an unparseable timestamp is invisible to parse_episodic_md and
    # would otherwise leave the denominator along with the numerator.
    #
    # Position decides the outcome, which is why it is pinned here: a
    # malformed block BETWEEN genuine records is dropped outright, while one
    # after the last genuine record is folded into that record's body. Only
    # the first case loses anything, and only the raw denominator sees it.
    text = (
        "## 2026-09-08 11:00:00Z - T-1\n\nA genuine record about lockfiles.\n\n"
        "## 2026-13-45 99:99:99Z - T-2\n\nA malformed record about worktrees.\n\n"
        "## 2026-09-08 11:30:00Z - T-3\n\nAnother genuine record about sandboxes.\n"
    )
    parsed_keywords = {
        keyword
        for sentence in qk.sentences_for_markdown(text, denominator="parsed")
        for keyword in sentence.keywords
    }
    raw_keywords = {
        keyword
        for sentence in qk.sentences_for_markdown(text, denominator="raw")
        for keyword in sentence.keywords
    }
    assert {"lockfiles", "sandboxes"} <= parsed_keywords
    assert "worktrees" in raw_keywords
    assert "worktrees" not in parsed_keywords, (
        "a record dropped between two genuine ones must stay in the raw "
        "denominator, or the loss it represents cannot be measured"
    )


def test_an_unknown_denominator_is_rejected_rather_than_silently_defaulted():
    with pytest.raises(ValueError, match="unknown denominator"):
        qk.sentences_for_markdown("## x", denominator="everything")


def test_recorded_goldens_are_valid_json_with_the_fields_the_suite_reads():
    for corpus_name in CORPORA:
        golden = json.loads(qk.golden_path(corpus_name).read_text(encoding="utf-8"))
        assert golden.keys() >= {
            "corpus",
            "extractor_version",
            "source_sha1",
            "denominator",
            "record_count",
            "sentences",
        }
        assert golden["denominator"] == qk.GOLDEN_DENOMINATOR
