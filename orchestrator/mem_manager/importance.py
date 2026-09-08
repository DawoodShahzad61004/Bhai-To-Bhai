"""Composite importance scoring (Architecture.md Formulae row 1) over episodic
memory logs shaped like:

    ## 2026-08-29 12:48:55Z - requirements
    <free-text content>

    ## 2026-08-29 12:51:46Z - T-001
    <free-text content>

One `parse_episodic_md()` + five scoring functions, stdlib only, so the same
module drops into any project whose episodic log follows this header shape.
Each factor takes the record plus whatever cross-record context it needs
(the corpus, an entity index) rather than reaching for global state.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Sequence

import config


@dataclass(frozen=True)
class EpisodicRecord:
    timestamp: datetime
    tag: str
    content: str
    raw: str = ""
    # Principle 6 (explicit vs inferred). These logs are the agent's own
    # observations, not a stated user preference, so "inferred" is the
    # honest default - callers that do have a stated preference set it explicitly.
    provenance: str = "inferred"


def parse_episodic_md(text: str) -> list[EpisodicRecord]:
    """Split a log on '## ' headers. Header shape is 'DATE TIME SEP TAG' -
    SEP is whatever separator character the log uses (an em dash, a mojibake
    artifact, anything); only its position, not its value, matters."""
    records: list[EpisodicRecord] = []
    # Split right before each '## ' so every chunk keeps its own header +
    # body together; a lookahead split (vs. a plain split) doesn't eat the delimiter.
    blocks = config.EPISODIC_BLOCK_SPLIT_PATTERN.split(text.strip())
    for block in blocks:
        block = block.strip()
        if not block.startswith("## "):
            continue  # stray text before the first header (e.g. a title line) - not a record
        header, _, body = block.partition("\n")
        # 'DATE TIME SEP TAG...' -> split into at most 4 pieces so a
        # multi-word tag doesn't get chopped up.
        parts = header[3:].strip().split(None, 3)
        if len(parts) < 4:
            continue  # malformed header - skip it rather than raise on a messy log
        date_str, time_str, _sep, tag = parts
        try:
            timestamp = datetime.strptime(
                f"{date_str} {time_str}", config.EPISODIC_TIMESTAMP_FORMAT
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue  # header shape matched but the timestamp didn't parse - skip, don't crash the batch
        records.append(EpisodicRecord(timestamp, tag.strip(), body.strip(), block))
    return records


def _similarity(a: str, b: str) -> float:
    # Stdlib-only text similarity (no embeddings dependency) - good enough to
    # group near-duplicate log lines; not a substitute for real semantic search.
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def f_recency(
    record: EpisodicRecord,
    *,
    now: datetime | None = None,
    half_life_hours: float = config.RECENCY_HALF_LIFE_HOURS,
) -> float:
    """Exponential decay from the record's timestamp. 168h (paper's own
    maturation half-life) is a starting point, not a settled constant."""
    now = now or datetime.now(timezone.utc)
    # Clamp at 0: guards against a record timestamped slightly ahead of `now`
    # (clock skew between whatever wrote the log and whatever is scoring it).
    age_hours = max(0.0, (now - record.timestamp).total_seconds() / 3600.0)
    return 0.5 ** (age_hours / half_life_hours)


def f_frequency(
    record: EpisodicRecord,
    corpus: Sequence[EpisodicRecord],
    *,
    similarity_threshold: float = config.FREQUENCY_SIMILARITY_THRESHOLD,
) -> float:
    """Inverse frequency of similar events. 'Similar' = same tag (same
    recurring task/event category) or near-duplicate content, so repeated
    failures on the same ticket count even when the error text varies."""
    similar = sum(
        1
        for other in corpus
        if other is not record
        and (
            other.tag == record.tag  # same recurring task/category, regardless of wording
            or _similarity(other.content, record.content) >= similarity_threshold  # or near-duplicate text
        )
    )
    return 1.0 / (1.0 + similar)  # 0 similar events -> 1.0 (novel); more repeats -> asymptotes toward 0


def f_surprise(record: EpisodicRecord, corpus: Sequence[EpisodicRecord]) -> float:
    """Distance from the prior distribution - compared only against records
    that existed *before* this one, so later memories can't leak into how
    surprising an earlier one was."""
    # Chronological filter is the point: scoring against the whole corpus
    # would let a similar memory formed *afterward* make this one look less
    # surprising than it actually was when it happened.
    prior = [r for r in corpus if r is not record and r.timestamp < record.timestamp]
    if not prior:
        return 1.0  # nothing preceded it - maximally surprising by definition
    max_sim = max(_similarity(record.content, p.content) for p in prior)
    return 1.0 - max_sim  # closest prior match sets the floor; distance from it is the surprise


def extract_entities(text: str) -> set[str]:
    # Each alternative in config.ENTITY_PATTERN captures into its own group;
    # exactly one group is non-None per match, so take whichever one fired.
    return {next(g for g in match.groups() if g) for match in config.ENTITY_PATTERN.finditer(text)}


def build_entity_index(corpus: Sequence[EpisodicRecord]) -> dict[str, float]:
    """Rolling salience per entity: how often it recurs across the corpus,
    normalized to [0, 1]. A real store would carry each entity's own accrued
    importance instead of a mention count - this is the dependency-free
    stand-in for that."""
    counts: dict[str, float] = defaultdict(float)
    for record in corpus:
        for entity in extract_entities(record.content):
            counts[entity] += 1.0  # mention count is the proxy signal here, not a tracked importance value
    if not counts:
        return {}
    peak = max(counts.values())
    return {entity: count / peak for entity, count in counts.items()}  # scale relative to the most-mentioned entity


def f_entity_salience(
    record: EpisodicRecord, entity_index: dict[str, float], *, default: float = 0.0
) -> float:
    """Max importance of any entity the record references."""
    entities = extract_entities(record.content)
    if not entities:
        return default  # nothing named in the text - no salience signal to read
    return max(entity_index.get(entity, default) for entity in entities)  # one salient entity is enough to lift the record


def f_outcome(
    record: EpisodicRecord,
    *,
    success_markers: Sequence[str] = config.DEFAULT_SUCCESS_MARKERS,
    failure_markers: Sequence[str] = config.DEFAULT_FAILURE_MARKERS,
) -> float:
    """Goal completion signal, read off the record's own text since these
    logs carry no separate status field. No markers of either kind (e.g. a
    plain observation/lesson, not a run outcome) scores neutral."""
    text = record.content.lower()
    failed = any(marker in text for marker in failure_markers)
    succeeded = any(marker in text for marker in success_markers)
    if failed and not succeeded:
        return 0.0
    if succeeded and not failed:
        return 1.0
    if failed and succeeded:
        return 0.4  # mixed signal (e.g. a failure fixed within the same entry) - lean below neutral, not a clean win
    return 0.5  # no signal either way - a plain observation/lesson, not a run outcome


# --- Ongoing lifecycle: passive decay (Architecture.md Formulae row 2) -----
# Separate from the five factors above: those set a record's *initial*
# importance once, at formation time. This runs repeatedly afterward and
# governs how that importance fades - or doesn't - with time and use.


def passive_decay(
    importance: float,
    encoded_at: datetime,
    *,
    now: datetime | None = None,
    last_accessed_at: datetime | None = None,
    lambda_: float = config.DECAY_LAMBDA_PER_HOUR,
) -> float:
    """I(t) = I0 * e^(-lambda * t), t in hours since the record was last
    *touched*. A retrieval resets the clock (pass its timestamp as
    `last_accessed_at`) - that reset is what lets an actively-used learning
    persist indefinitely instead of decaying on a fixed schedule regardless
    of whether anything ever reads it."""
    now = now or datetime.now(timezone.utc)
    # No retrieval yet -> the clock has only ever run from creation.
    reference = last_accessed_at or encoded_at
    # Clamp at 0 for the same reason as f_recency: tolerate clock skew rather
    # than produce a negative age (which would grow the score instead of decaying it).
    age_hours = max(0.0, (now - reference).total_seconds() / 3600.0)
    return importance * math.exp(-lambda_ * age_hours)


# --- Composing the five factors into one score (Formulae row 1) -----------


def composite_importance(
    record: EpisodicRecord,
    corpus: Sequence[EpisodicRecord],
    entity_index: dict[str, float],
    *,
    now: datetime | None = None,
) -> float:
    """S(e) = sum(w_i * f_i(e)) over the five factors. This is the *initial*
    value a new record's activation starts from; `passive_decay()` above
    takes it from there."""
    factors = {
        "recency": f_recency(record, now=now),
        "frequency": f_frequency(record, corpus),
        "surprise": f_surprise(record, corpus),
        "entity": f_entity_salience(record, entity_index),
        "outcome": f_outcome(record),
    }
    score = sum(config.IMPORTANCE_WEIGHTS[key] * value for key, value in factors.items())
    if record.provenance == "explicit":
        score *= 1 + config.EXPLICIT_PROVENANCE_BOOST
    return min(1.0, score)  # the boost can push the weighted sum past 1.0; clamp to keep the scale meaningful
