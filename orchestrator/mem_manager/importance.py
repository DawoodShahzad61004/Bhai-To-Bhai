"""Composite importance scoring (Architecture.md Formulae row 1) over episodic
memory logs shaped like:

    ## 2026-08-29 12:48:55Z - requirements
    <!-- session:claude:9f3ab2c1 -->
    <free-text content>

    ## 2026-08-29 12:51:46Z - T-001
    <free-text content>

or user_choices.md's run-id-first shape:

    ## Run `run-20260829-174814` — 2026-08-29 12:48:55Z
    <!-- session:codex:0198ab -->
    <free-text content>

The optional session marker names the agent-backend session that wrote the
entry; it is absent on legacy entries and on the coding subagents' writes,
which come from their own OS process with no AgentResult in hand.

One `parse_episodic_md()` + five scoring functions, stdlib only, so the same
module drops into any project whose episodic log follows this header shape.
Each factor takes the record plus whatever cross-record context it needs
(the corpus, a session index) rather than reaching for global state.
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
    # "<backend>:<session_id>" of the agent turn that wrote this entry, when the
    # writer stamped one. Empty for legacy entries and for the coding subagents'
    # CLI writes, which run in their own OS process with no AgentResult to read
    # one from - those score 0 on salience rather than being given a fake identity.
    session: str = ""
    # Principle 6 (explicit vs inferred). These logs are the agent's own
    # observations, not a stated user preference, so "inferred" is the
    # honest default - callers that do have a stated preference set it explicitly.
    provenance: str = "inferred"
    # user_choices.md's run id, captured off the header itself for the
    # "Run `ID`" shape only - empty for every other record. Threaded through
    # separately from `tag` because /compact's file-level tag renumbering
    # (see compact.py's DURABLE_MEMORY_TAG_PREFIXES step) overwrites `tag` for
    # display, and losing this alongside it would make a rewritten
    # user_choices.md unable to re-emit the '<!-- run:ID -->' sentinel
    # append_user_choices() needs for idempotent replay.
    run_id: str = ""
    # Present only when a prior /compact rewrite stamped a
    # '<!-- last_accessed_at:... -->' marker on this record; None means "use
    # this record's own timestamp", the same default build_durable_memories()
    # already applied before these markers existed.
    last_accessed_at: datetime | None = None
    # Present only when a prior /compact rewrite stamped a
    # '<!-- merged_from:... -->' marker; empty means "no prior lineage to
    # inherit", so build_durable_memories() falls back to this record's own id.
    merged_from: tuple[str, ...] = ()
    # Present only when a prior /compact rewrite stamped a '<!-- id:... -->'
    # marker; empty means "never compacted before", so build_durable_memories()
    # derives a fresh id. See config.DURABLE_ID_MARKER_TEMPLATE for why this
    # can't just be re-derived from the record's current tag every time.
    id: str = ""


def _take_session_marker(body: str) -> tuple[str, str]:
    """Pull the writer's session marker off the top of a block body.

    The marker is the block's FIRST BODY line, not a line above the header: the
    block split is a lookahead on '## ', so anything written above a header
    belongs to the previous block. Anchored with `.match()`, so a marker quoted
    inside a finding is content, not metadata.
    """
    match = config.SESSION_MARKER_PATTERN.match(body)
    if not match:
        return "", body
    return match.group(1), body[match.end():]


def _take_metadata_markers(body: str) -> tuple[dict[str, str], str]:
    """Pull every durable-metadata marker off the top of a block body (after
    the session marker, if any), in whatever order they were written.

    Same anchoring rule as `_take_session_marker`: matched repeatedly at
    position 0 of the shrinking body, so a marker quoted mid-finding is left
    as content rather than consumed as metadata.
    """
    values: dict[str, str] = {}
    while True:
        match = config.DURABLE_METADATA_MARKER_PATTERN.match(body)
        if not match:
            return values, body
        values[match.group(1)] = match.group(2)
        body = body[match.end():]


def _parse_marker_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, config.EPISODIC_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_marker_merged_from(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part for part in value.split(",") if part)


def _is_record_header(header: str) -> bool:
    """Whether a '## '-prefixed line is a genuine record boundary.

    Used to tell a real header apart from a nested Markdown heading or a
    heading-shaped line inside a fenced code block that merely happens to
    start with '## ' - neither of those parses as either supported header
    shape, so neither should split the record it actually belongs to.
    """
    if config.USER_CHOICES_HEADER_PATTERN.match(header):
        return True
    parts = header[3:].strip().split(None, 3)
    if len(parts) < 4:
        return False
    date_str, time_str, _sep, _tag = parts
    try:
        datetime.strptime(f"{date_str} {time_str}", config.EPISODIC_TIMESTAMP_FORMAT)
    except ValueError:
        return False
    return True


def parse_episodic_md(text: str) -> list[EpisodicRecord]:
    """Split a log on '## ' headers. Header shape is 'DATE TIME SEP TAG' -
    SEP is whatever separator character the log uses (an em dash, a mojibake
    artifact, anything); only its position, not its value, matters. Falls
    back to user_choices.md's 'Run RUN-ID SEP DATE TIME' shape, using the
    run id as the record's tag."""
    records: list[EpisodicRecord] = []
    # Split right before each '## ' so every chunk keeps its own header +
    # body together; a lookahead split (vs. a plain split) doesn't eat the delimiter.
    raw_blocks = config.EPISODIC_BLOCK_SPLIT_PATTERN.split(text.strip())

    candidates = [
        raw_block for raw_block in raw_blocks if raw_block.strip().startswith("## ")
    ]  # drop stray text before the first header (e.g. a title line) - not a record
    genuine_indices = {
        i for i, raw_block in enumerate(candidates)
        if _is_record_header(raw_block.strip().partition("\n")[0])
    }
    last_genuine = max(genuine_indices, default=-1)

    # A '## '-prefixed chunk that doesn't parse as either real header shape
    # isn't a new record. Trailing after the last genuine header, it's a
    # nested heading, or a heading-shaped line inside a code fence, that the
    # split treated as a boundary purely because it starts with '## ' - fold
    # it back onto that final record (re-inserting the single '\n' the split
    # consumed) instead of silently dropping it, which is what truncated the
    # body before. Before or between genuine headers, it reads as a
    # deliberately malformed record attempt sitting between two real ones,
    # not a continuation of the one before it - the existing "malformed,
    # skip it" behavior stands there.
    blocks: list[str] = []
    for i, raw_block in enumerate(candidates):
        if i > last_genuine and blocks:
            blocks[-1] = blocks[-1] + "\n" + raw_block
            continue
        if i not in genuine_indices:
            continue  # malformed header before/between genuine ones - skip it
        blocks.append(raw_block)

    for block in blocks:
        block = block.strip()
        header, _, body = block.partition("\n")
        # Once, before either header shape is tried: both writers stamp the
        # marker in the same place, so neither branch below needs to know about it.
        session, body = _take_session_marker(body)
        # `append_user_choices()`'s '<!-- run:ID -->' sentinel lands at the
        # END of the block split BEFORE the one it names (see config.py) -
        # strip it back off rather than let it read as this record's content.
        body = config.USER_CHOICES_RUN_MARKER_PATTERN.sub("", body)
        metadata, body = _take_metadata_markers(body)
        record_id = metadata.get("id", "")
        last_accessed_at = _parse_marker_timestamp(metadata.get("last_accessed_at"))
        merged_from = _parse_marker_merged_from(metadata.get("merged_from"))

        # user_choices.md shape: '## Run `RUN-ID` SEP DATE TIME' - run id
        # leads instead of trailing, so it needs its own match before falling
        # back to the standard 'DATE TIME SEP TAG' header.
        choices_match = config.USER_CHOICES_HEADER_PATTERN.match(header)
        if choices_match:
            run_id, date_str, time_str = choices_match.groups()
            try:
                timestamp = datetime.strptime(
                    f"{date_str} {time_str}", config.EPISODIC_TIMESTAMP_FORMAT
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                continue  # header shape matched but the timestamp didn't parse - skip, don't crash the batch
            records.append(EpisodicRecord(
                timestamp, run_id, body.strip(), block, session,
                # A stated user choice is explicit provenance by construction
                # (principle 6) - a marker overrides that only if a prior
                # /compact pass recorded something different for it.
                provenance=metadata.get("provenance", "explicit"),
                run_id=run_id,
                last_accessed_at=last_accessed_at,
                merged_from=merged_from,
                id=record_id,
            ))
            continue

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
        records.append(EpisodicRecord(
            timestamp, tag.strip(), body.strip(), block, session,
            provenance=metadata.get("provenance", "inferred"),
            last_accessed_at=last_accessed_at,
            merged_from=merged_from,
            id=record_id,
        ))
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


def build_session_index(corpus: Sequence[EpisodicRecord]) -> dict[str, float]:
    """Rolling salience per session: how many records that session wrote,
    normalized to [0, 1]. A session that keeps producing entries is the
    recurring actor in this log. A real store would carry each session's own
    accrued importance instead of a record count - this is the dependency-free
    stand-in for that."""
    counts: dict[str, float] = defaultdict(float)
    for record in corpus:
        if record.session:
            counts[record.session] += 1.0  # record count is the proxy signal here, not a tracked importance value
    if not counts:
        return {}
    peak = max(counts.values())
    return {session: count / peak for session, count in counts.items()}  # scale relative to the busiest session


def f_session_salience(
    record: EpisodicRecord, session_index: dict[str, float], *, default: float = 0.0
) -> float:
    """Salience of the session that wrote this record. One session per record,
    so there is nothing to take a maximum over."""
    if not record.session:
        return default  # legacy entry, or an out-of-process CLI write - no identity to score
    return session_index.get(record.session, default)


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
    session_index: dict[str, float],
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
        "session": f_session_salience(record, session_index),
        "outcome": f_outcome(record),
    }
    score = sum(config.IMPORTANCE_WEIGHTS[key] * value for key, value in factors.items())
    if record.provenance == "explicit":
        score *= 1 + config.EXPLICIT_PROVENANCE_BOOST
    return min(1.0, score)  # the boost can push the weighted sum past 1.0; clamp to keep the scale meaningful
