"""Scoring formulas for compaction quality and savings.

The requested figure is recall - how much of the source survived:

    type_recall = |keywords found in output| / |keywords in source|

TYPE-LEVEL, NOT OCCURRENCE-LEVEL, and that is the one real judgment call
here. If five duplicate sentences carry keyword `k` and merging correctly
collapses them into one, an occurrence-level recall scores a flawless
deduplicator at 0.2 - it is maximized by not compacting at all, which is the
opposite of what the metric is for. Type recall asks "was any distinct fact
destroyed", which is the actual question. occurrence_retention is still
reported, as the compression trace rather than as a quality score.

Four figures beyond the request, each closing a hole in found/total:

  rare_recall     type recall hides a loss behind keywords that happen to
                  recur elsewhere; keywords occurring in exactly one sentence
                  cannot hide. Sharpest single number here. None - not 1.0 -
                  when a corpus has no rare keywords at all, which is
                  inherent to a corpus built from verbatim triplicates.
  macro_recall    micro recall is implicitly weighted toward code-dense
                  records, which contribute far more keywords; losing one
                  prose record barely moves it.
  coverage(1.0)   catches a whole sentence lost while its individually common
                  words survive elsewhere.
  injected        found/total is RECALL. Nothing in it catches the model
                  INVENTING a fact, which for a memory system is the worse
                  failure. Offline this must be exactly zero.

And the rule the whole report is built around: recall is never emitted on its
own. Doing nothing scores 1.0 recall; deleting everything scores ~1.0
savings. Only the pair is meaningful.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

import quality_keywords as qk

COVERAGE_THRESHOLDS = (0.5, 0.8, 1.0)
# The report prints losses; beyond this many it prints a count instead.
MISSING_REPORT_CAP = 50


@dataclass(frozen=True)
class Missing:
    keyword: str
    sentence_index: int
    record_tag: str


@dataclass(frozen=True)
class KeywordScore:
    total: int
    found: int
    type_recall: float
    rare_total: int
    rare_found: int
    rare_recall: float | None
    macro_recall: float
    occurrence_retention: float
    sentence_coverage: dict[float, float]
    multiword_keyword_count: int
    mean_keywords_per_sentence: float
    injected: tuple[str, ...] = ()
    missing: tuple[Missing, ...] = ()

    @property
    def missing_keywords(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(entry.keyword for entry in self.missing))


def _output_sentences(output_content: str) -> list[tuple[str, ...]]:
    """Segment the compacted content the same way the source was segmented,
    so occurrence counts on both sides mean the same thing."""
    return [
        qk.extract_keywords(unit)
        for unit in qk.split_sentences(output_content)
        if qk.extract_keywords(unit)
    ]


def score_keywords(
    source: Sequence[qk.Sentence],
    output_content: str,
    *,
    source_text: str = "",
    thresholds: Sequence[float] = COVERAGE_THRESHOLDS,
) -> KeywordScore:
    if not source:
        raise ValueError("cannot score against an empty source; the corpus parsed to nothing")

    tokens = qk.tokenize_for_match(output_content)
    collapsed = qk.collapse_for_multiword(output_content)

    all_keywords = {keyword for sentence in source for keyword in sentence.keywords}
    found = {
        keyword
        for keyword in all_keywords
        if qk.matches(keyword, tokens, collapsed)
    }
    rare = qk.rare_keywords(source)

    missing = tuple(
        Missing(keyword, sentence.index, sentence.record_tag)
        for sentence in source
        for keyword in sentence.keywords
        if keyword not in found
    )

    per_record: dict[int, set[str]] = {}
    for sentence in source:
        per_record.setdefault(sentence.record_index, set()).update(sentence.keywords)
    macro = sum(
        len(keywords & found) / len(keywords) for keywords in per_record.values()
    ) / len(per_record)

    coverage = {}
    for threshold in thresholds:
        covered = sum(
            1
            for sentence in source
            if len(set(sentence.keywords) & found) / len(sentence.keywords) >= threshold
        )
        coverage[threshold] = covered / len(source)

    source_counts = Counter(
        keyword for sentence in source for keyword in set(sentence.keywords)
    )
    output_counts = Counter(
        keyword for keywords in _output_sentences(output_content) for keyword in set(keywords)
    )
    retained = sum(
        min(output_counts.get(keyword, 0), count) for keyword, count in source_counts.items()
    )
    occurrence_retention = retained / sum(source_counts.values())

    # Injection is measured against the whole source DOCUMENT, not against the
    # extracted keyword set. The parser folds an invalid pseudo-header's text
    # into the record above it, so words that only ever appeared in a header
    # legitimately reach the output; counting those as invented would report a
    # hallucination the model never had. Without source_text the check falls
    # back to the keyword set, which is stricter and sometimes wrong.
    if source_text:
        source_tokens = qk.tokenize_for_match(source_text)
        collapsed_source = qk.collapse_for_multiword(source_text)
        injected = tuple(
            sorted(
                keyword
                for keyword in set(output_counts)
                if not qk.matches(keyword, source_tokens, collapsed_source)
            )
        )
    else:
        injected = tuple(
            sorted(keyword for keyword in set(output_counts) if keyword not in all_keywords)
        )

    return KeywordScore(
        total=len(all_keywords),
        found=len(found),
        type_recall=len(found) / len(all_keywords),
        rare_total=len(rare),
        rare_found=len(rare & found),
        # A corpus of verbatim triplicates has no keyword in exactly one
        # sentence. Reporting 1.0 there would invent a passing grade for a
        # measurement that was never taken.
        rare_recall=(len(rare & found) / len(rare)) if rare else None,
        macro_recall=macro,
        occurrence_retention=occurrence_retention,
        sentence_coverage=coverage,
        multiword_keyword_count=sum(1 for keyword in all_keywords if " " in keyword),
        mean_keywords_per_sentence=sum(len(s.keywords) for s in source) / len(source),
        injected=injected,
        missing=missing,
    )


@dataclass(frozen=True)
class TokenScore:
    counter_name: str
    before: int
    after: int
    freed: int
    freed_pct: float
    after_content_only: int
    marker_overhead: int


def score_tokens(source_text: str, output_text: str, output_content: str, counter) -> TokenScore:
    """Measured on FILE text, so the four or five `<!-- key:value -->` marker
    lines _memories_to_markdown adds per memory are charged honestly.

    `freed` is routinely NEGATIVE on a corpus with nothing to merge, and that
    is reported rather than hidden - it is Bugs.md #52 (unbounded shared-log
    growth) expressed as a number. `after_content_only` separates "compaction
    removed information" from "serialization added overhead".
    """
    before = counter.count(source_text)
    after = counter.count(output_text)
    content_only = counter.count(output_content)
    return TokenScore(
        counter_name=counter.name,
        before=before,
        after=after,
        freed=before - after,
        freed_pct=(before - after) / before if before else 0.0,
        after_content_only=content_only,
        marker_overhead=after - content_only,
    )


def prune_collateral(
    source: Sequence[qk.Sentence],
    pruned_tags: Sequence[str],
) -> tuple[float | None, tuple[str, ...]]:
    """Of the keywords carried by pruned records, what share survive nowhere
    else?

    This, not a recall-versus-fraction sweep, is the number worth having.
    Pruning drops floor(n x fraction) records BY RANK, so a recall curve over
    the fraction is a step function fully determined by the importance
    ordering - it measures "did we delete what we said we would", not
    quality. A pruned record whose facts are duplicated elsewhere is a GOOD
    prune; one whose facts exist nowhere else is real information loss, and
    only this distinguishes them.

    Returns (collateral, orphaned_keywords); collateral is None when nothing
    was pruned.
    """
    pruned = set(pruned_tags)
    if not pruned:
        return None, ()
    pruned_keywords: set[str] = set()
    surviving_keywords: set[str] = set()
    for sentence in source:
        target = pruned_keywords if sentence.record_tag in pruned else surviving_keywords
        target.update(sentence.keywords)
    if not pruned_keywords:
        return None, ()
    orphaned = pruned_keywords - surviving_keywords
    return len(orphaned) / len(pruned_keywords), tuple(sorted(orphaned))
