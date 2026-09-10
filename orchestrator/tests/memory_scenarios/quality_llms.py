"""Merge and judge callables for the lane where recall is a real measurement.

Offline, `dedupe_and_merge` joins content verbatim, so recall is 1.0 by
construction. The one place content is REGENERATED rather than concatenated is
`merge_group`'s LLM rewrite - and `docs/Research.md` topic 59 records that
live model output quality "was not evaluated by this suite".

These are the synthetic stand-ins that make that path testable for free. They
are deterministic string transforms over the prompt the pipeline actually
builds, injected through `compact_markdown(..., llm_call=, judge_call=)` - the
documented seam - so the real `merge_group` decision ladder runs, including
the exact-"FAITHFUL" judge gate.

The point of the lossy and inventing stand-ins is not to test the LLM. It is
to prove the METRIC works: a measurement that has never reported a loss has
not been shown capable of reporting one.

`live_merge_calls` returns the real provider pair, and only when
MEM_QUALITY_LIVE == "1". Nothing is skipped when it is unset - the test falls
back to the synthetic pair and still asserts - so CI never pays and never goes
quiet. Belt and braces: conftest's autouse `offline` fixture also replaces
`dedup_merge._default_llm_calls` with a tripwire that raises on any attempt to
construct a live client through the default path.
"""
from __future__ import annotations

import os
import re
from typing import Callable

LLMCall = Callable[[list], "str | None"]

LIVE_ENV_VAR = "MEM_QUALITY_LIVE"

# merge_group builds its prompt with _format_entries: one "- " bullet per
# source memory, blank line separated. Parsing it back is how a synthetic
# rewriter sees the same content the model would.
_ENTRY = re.compile(r"^- (.*)$", re.MULTILINE | re.DOTALL)


def _entries_from(prompt: str) -> list[str]:
    body = prompt.split("Entries:", 1)[-1].strip()
    return [chunk[2:].strip() for chunk in body.split("\n\n") if chunk.startswith("- ")]


def _prompt_of(messages: list) -> str:
    return "".join(message.get("content", "") for message in messages)


class RecordingCalls:
    """A merge/judge pair that remembers what it was asked and what it said.

    The transcript is the evidence when recall drops: without it, a live-lane
    number is a figure with no explanation attached.
    """

    def __init__(self, rewrite, verdict, name: str = "synthetic") -> None:
        self.name = name
        self._rewrite = rewrite
        self._verdict = verdict
        self.transcript: list[dict] = []
        self.verdicts: dict[str, int] = {"accepted": 0, "rejected": 0, "unparseable": 0}

    def merge(self, messages: list) -> str | None:
        prompt = _prompt_of(messages)
        answer = self._rewrite(_entries_from(prompt))
        self.transcript.append(
            {"role": "merge", "entries": _entries_from(prompt), "response": answer}
        )
        return answer

    def judge(self, messages: list) -> str | None:
        answer = self._verdict(_prompt_of(messages))
        normalized = (answer or "").strip().upper()
        if normalized == "FAITHFUL":
            self.verdicts["accepted"] += 1
        elif normalized in {"UNFAITHFUL"}:
            self.verdicts["rejected"] += 1
        else:
            # Anything else is refused by merge_group's exact comparison, and
            # counting it apart is how "the judge is decoration" and "the judge
            # is saving you" become distinguishable claims.
            self.verdicts["unparseable"] += 1
        self.transcript.append({"role": "judge", "response": answer})
        return answer

    def as_pair(self) -> tuple[LLMCall, LLMCall]:
        return self.merge, self.judge


def faithful_merge_calls() -> RecordingCalls:
    """Rewrites to something equivalent to the deterministic union, and passes
    judgement. The control: recall must stay at 1.0."""
    return RecordingCalls(
        rewrite=lambda entries: " ".join(dict.fromkeys(entries)),
        verdict=lambda _prompt: "FAITHFUL",
        name="faithful",
    )


def _drop_final_sentence(entries: list[str]) -> str:
    """Join the distinct entries, then discard the last sentence.

    Dropping a whole ENTRY is not enough to model a lossy rewrite: where the
    entries are verbatim duplicates of one another, removing one costs nothing
    at all, and the metric would correctly report no loss. Dropping the final
    sentence of the merged text loses real content in every case, duplicates
    included - which is what makes this a usable probe of the measurement.
    """
    text = " ".join(dict.fromkeys(entries))
    sentences = text.split(". ")
    if len(sentences) > 1:
        return ". ".join(sentences[:-1]) + "."
    return text


def lossy_merge_calls() -> RecordingCalls:
    """Silently discards the final sentence, and claims to be faithful.

    This is the failure a memory system must never suffer without noticing,
    and the reason the judge gate exists. If the metric cannot see this, it
    cannot see anything.
    """
    return RecordingCalls(
        rewrite=_drop_final_sentence, verdict=lambda _prompt: "FAITHFUL",
        name="lossy",
    )


def lossy_merge_calls_with_honest_judge() -> RecordingCalls:
    """The same lossy rewrite, but the judge refuses it.

    merge_group then falls back to the verbatim union, so recall returns to
    1.0 - which is exactly what the `== "FAITHFUL"` gate is worth, measured.
    """
    return RecordingCalls(
        rewrite=_drop_final_sentence, verdict=lambda _prompt: "UNFAITHFUL",
        name="lossy-judged",
    )


INVENTED_SENTENCE = (
    "Escalate to the zeppelin quartermaster before rerunning the pipeline."
)


def inventing_merge_calls() -> RecordingCalls:
    """Keeps every source fact and adds one that appears nowhere in the source.

    The precision half of the metric. `found / total` is recall and cannot
    express this at all, yet for a memory system a fabricated instruction is
    worse than a dropped one.
    """
    return RecordingCalls(
        rewrite=lambda entries: " ".join(dict.fromkeys(entries)) + " " + INVENTED_SENTENCE,
        verdict=lambda _prompt: "FAITHFUL",
        name="inventing",
    )


def ambiguous_verdict_calls() -> RecordingCalls:
    """A judge that answers in prose containing the word FAITHFUL.

    merge_group compares for exact equality precisely so that "NOT FAITHFUL"
    and "faithfulness is preserved" are refused rather than read as approval.
    """
    return RecordingCalls(
        rewrite=_drop_final_sentence,
        verdict=lambda _prompt: "I would say this merge is FAITHFUL overall.",
        name="ambiguous-verdict",
    )


def live_enabled() -> bool:
    return os.environ.get(LIVE_ENV_VAR) == "1"


def live_merge_calls() -> tuple[LLMCall, LLMCall] | None:
    """The real provider pair, or None.

    Two independent gates, so neither a leaked environment variable nor a
    stray argument alone reaches a paid API: the caller must ask for the live
    lane AND the variable must be set. Import is deferred because
    services/llm_setup constructs its clients at module import time.
    """
    if not live_enabled():
        return None
    from mem_manager.services.dedup_merge import _default_llm_calls

    return _default_llm_calls()
