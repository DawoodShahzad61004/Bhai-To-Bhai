"""Near-duplicate grouping (embeddings) and merging (LLM-assisted, with a
deterministic fallback) over DurableMemory records.

Rewritten from the RAG-work sibling project's version, which grouped
LangGraph retrieval chunks (`GraphState`, `nodes.nac._merge_similar_chunks`,
`validators.validate_merge`, `switches`, `timing_tracker`) - none of which
exist here. The grouping *pattern* (embed once, pairwise cosine similarity,
threshold cutoff) carries over; everything downstream of it is new, built
for DurableMemory instead of retrieval chunks. The grouping *rule* is
complete-linkage (a candidate must be similar enough to every member already
in the group, not just the first one) - chosen over single-linkage/anchor-
only or transitive closure because a merge here concatenates the whole group
into one memory, so a false merge is costlier than a missed one.

merge_group() always computes the deterministic union first - the ADR-014
precedent this repo's own memora_mini established, so a merge can never fail
to produce *something* even if the LLM is disabled, unreachable, or its
output is judged unfaithful. The LLM path, when it succeeds and passes
judge_call, replaces just the merged content.
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Callable, Sequence

import config
from ..memory import DurableMemory, content_id

logger = logging.getLogger(__name__)

LLMCall = Callable[[list[dict]], "str | None"]

# Matches the literal prefix `run_shared_command()` in the Bhai-To-Bhai
# orchestrator (orchestrator/artifacts.py) writes onto every auto-recorded
# test/command failure: `[auto] `<command>` failed (exit <code>): <symptom>`.
# That prefix is identical across unrelated failures (same command, same exit
# code), so on short entries it dominates the sentence embedding and pulls
# topically unrelated auto-failures together - stripped here so only the
# symptom drives similarity. Stored/merged content is untouched; this only
# affects what gets embedded for grouping.
_AUTO_FAILURE_PREFIX = re.compile(r"^\[auto\] `.*?` failed \(exit -?\d+\): ")

# Semantically opposite statements ("always enable X" / "never enable X") can
# score as near-duplicates on embedding similarity alone - that's exactly what
# makes them dangerous to silently collapse into one merged record. This is a
# narrow, deterministic guard independent of the embedder: a negation-marker
# asymmetry between two otherwise-similar texts means they disagree, not that
# they're saying the same thing twice.
_NEGATION_MARKERS = re.compile(
    r"\b(never|not|no|n't|cannot|without|disable[ds]?|avoid|prevent)\b", re.IGNORECASE
)


def _negation_signature(text: str) -> bool:
    return bool(_NEGATION_MARKERS.search(text))


def _looks_contradictory(a: str, b: str) -> bool:
    return _negation_signature(a) != _negation_signature(b)


def _embedding_text(content: str) -> str:
    stripped = _AUTO_FAILURE_PREFIX.sub("", content, count=1)
    return stripped if stripped.strip() else content


def find_near_duplicate_groups(
    memories: Sequence[DurableMemory], embedder, threshold: float
) -> list[list[int]]:
    """Group indices into `memories` by complete-linkage cosine similarity: a
    candidate joins a group only if it's similar enough to every member
    already in it, not just the first (anchor) one - so a candidate that's
    similar to one member but not another doesn't fold in and drag an
    unrelated entry into the merge. Singleton groups (no duplicate found)
    are included too, so callers have one uniform list of groups to
    merge/pass through."""
    if not memories:
        return []
    embeddings = embedder.generate_embedding([_embedding_text(m.content) for m in memories])
    claimed: set[int] = set()
    groups: list[list[int]] = []
    for i in range(len(memories)):
        if i in claimed:
            continue
        group = [i]
        for j in range(i + 1, len(memories)):
            if j in claimed:
                continue
            if all(
                embedder.cosine_similarity(embeddings[member], embeddings[j]) >= threshold
                and not _looks_contradictory(memories[member].content, memories[j].content)
                for member in group
            ):
                group.append(j)
                claimed.add(j)
        claimed.add(i)
        groups.append(group)
        if len(group) > 1:
            logger.info(
                "[DEDUP_MERGE] near-duplicate group found (threshold=%.3f): indices %s will be merged",
                threshold,
                group,
            )
    return groups


def _deterministic_union(group: Sequence[DurableMemory]) -> DurableMemory:
    """Plain Python union, no LLM: concatenate distinct text, earliest
    created_at, latest last_accessed_at (so a reinforced memory decays
    slower - see consolidate.refresh_decay), max importance, explicit
    provenance wins if any member has it."""
    ordered = sorted(group, key=lambda memory: memory.importance, reverse=True)
    keeper = ordered[0]
    content = "\n---\n".join(dict.fromkeys(memory.content for memory in ordered))
    return DurableMemory(
        id=content_id(content),
        content=content,
        tag=keeper.tag,
        created_at=min(memory.created_at for memory in ordered),
        last_accessed_at=max(memory.last_accessed_at for memory in ordered),
        importance=max(memory.importance for memory in ordered),
        provenance="explicit" if any(memory.provenance == "explicit" for memory in ordered) else "inferred",
        # Same tie-break as `tag` above: the highest-importance member wins. A
        # merge spanning two sessions has no single truthful session, and
        # blanking it would throw away the identity of the common case, where
        # every member came from the same agent turn.
        session=keeper.session,
        run_id=keeper.run_id,
        merged_from=[source_id for memory in ordered for source_id in memory.merged_from],
    )


_MERGE_PROMPT = """You are consolidating {count} near-duplicate entries from an agent's episodic \
memory log into a single durable memory. The entries were already judged near-duplicate by \
embedding similarity - your job is to fold them into ONE statement that keeps every distinct fact \
and drops repetition. Do not add anything the entries don't say.

Respond with ONLY the merged text - no preamble, no labels, no surrounding quotes.

Entries:
{entries}"""

_JUDGE_PROMPT = """A memory-consolidation step merged these {count} source entries:
{entries}

into this single merged statement:
{merged}

Does the merged statement preserve every distinct fact from the sources, without inventing \
anything the sources don't say? Respond with exactly one word: FAITHFUL or UNFAITHFUL."""


def _format_entries(group: Sequence[DurableMemory]) -> str:
    return "\n\n".join(f"- {memory.content}" for memory in group)


def _llm_merge_text(group: Sequence[DurableMemory], llm_call: LLMCall) -> str | None:
    prompt = _MERGE_PROMPT.format(count=len(group), entries=_format_entries(group))
    try:
        text = llm_call([{"role": "user", "content": prompt}])
    except Exception:
        logger.exception("[DEDUP_MERGE] LLM merge call raised")
        return None
    return text.strip() if text and text.strip() else None


def _judge_accepts(group: Sequence[DurableMemory], merged_text: str, judge_call: LLMCall) -> bool:
    prompt = _JUDGE_PROMPT.format(
        count=len(group), entries=_format_entries(group), merged=merged_text
    )
    try:
        verdict = judge_call([{"role": "user", "content": prompt}])
    except Exception:
        logger.exception("[DEDUP_MERGE] judge call raised")
        return False
    if not verdict:
        return False
    # Exact match, not substring: the prompt asks for exactly one word, and
    # "NOT FAITHFUL"/"FAITHFULNESS"/prose containing the word all contain
    # "FAITHFUL" as a substring without being the unambiguous verdict it asks for.
    return verdict.strip().upper() == "FAITHFUL"


def merge_group(
    group: Sequence[DurableMemory],
    *,
    llm_call: LLMCall | None = None,
    judge_call: LLMCall | None = None,
) -> DurableMemory:
    if len(group) <= 1:
        logger.info(
            "[DEDUP_MERGE] singleton group (no near-duplicates) — no merge, no LLM call needed"
        )
        # Unchanged pass-through: _deterministic_union() recomputes id from
        # content alone, which collides two distinct singleton records that
        # happen to share content (e.g. same text, different timestamps) and
        # discards the original source-derived id even when they don't.
        return group[0]

    base = _deterministic_union(group)

    if llm_call is None or not config.MERGE_LLM_ENABLED:
        logger.info(
            "[DEDUP_MERGE] LLM call skipped for group of %d (llm_call=%s, MERGE_LLM_ENABLED=%s) "
            "— using deterministic union",
            len(group),
            llm_call is not None,
            config.MERGE_LLM_ENABLED,
        )
        return base

    logger.info("[DEDUP_MERGE] LLM merge call: making call for group of %d", len(group))
    text = _llm_merge_text(group, llm_call)
    if not text:
        logger.info(
            "[DEDUP_MERGE] LLM merge call returned no usable text — using deterministic union"
        )
        return base

    if judge_call is None or not config.MERGE_VALIDATION_ENABLED:
        logger.info(
            "[DEDUP_MERGE] LLM judge call skipped (judge_call=%s, MERGE_VALIDATION_ENABLED=%s) "
            "— accepting LLM merge unvalidated",
            judge_call is not None,
            config.MERGE_VALIDATION_ENABLED,
        )
        return replace(base, content=text, id=content_id(text))

    logger.info("[DEDUP_MERGE] LLM judge call: making call to validate merge of %d entries", len(group))
    if _judge_accepts(group, text, judge_call):
        logger.info("[DEDUP_MERGE] judge accepted LLM merge")
        return replace(base, content=text, id=content_id(text))
    logger.info("[DEDUP_MERGE] judge rejected LLM merge — using deterministic union")
    return base


def _default_embedder():
    from .embedding_manager import EmbeddingManager

    return EmbeddingManager()


def _default_llm_calls() -> tuple[LLMCall, LLMCall]:
    from . import llm_caller, llm_setup

    def llm_call(messages: list[dict]) -> str | None:
        result = llm_caller.llm_invoke(llm_setup.llm, messages, caller_tag="mem_manage.merge")
        return result.content if result.ok else None

    def judge_call(messages: list[dict]) -> str | None:
        result = llm_caller.llm_invoke(llm_setup.judge_llm, messages, caller_tag="mem_manage.judge")
        return result.content if result.ok else None

    return llm_call, judge_call


def dedupe_and_merge(
    memories: Sequence[DurableMemory],
    *,
    embedder=None,
    threshold: float | None = None,
    llm_call: LLMCall | None = None,
    judge_call: LLMCall | None = None,
    use_llm: bool = True,
) -> list[DurableMemory]:
    """Group near-duplicates by embedding similarity, then merge each group.

    `embedder`/`llm_call`/`judge_call` default to the real services (built
    lazily, imported only here) when omitted; pass fakes to run entirely
    offline, e.g. in tests. `use_llm=False` skips the LLM path outright and
    always uses the deterministic union, without needing to touch
    config.MERGE_LLM_ENABLED.
    """
    if not memories:
        return []
    embedder = embedder or _default_embedder()
    threshold = config.MERGE_SIMILARITY_THRESHOLD if threshold is None else threshold
    groups = find_near_duplicate_groups(memories, embedder, threshold)

    if not use_llm:
        llm_call, judge_call = None, None
    elif llm_call is None and config.MERGE_LLM_ENABLED and any(len(group) > 1 for group in groups):
        # Build the default LLM/judge clients only when a real merge needs
        # them - a singleton-only or LLM-disabled batch has no use for a live
        # client, and constructing one anyway both wastes the call and turns
        # an unrelated provider outage into a failure for a batch that never
        # needed the LLM.
        try:
            llm_call, judge_call = _default_llm_calls()
        except Exception:
            logger.exception(
                "[DEDUP_MERGE] default LLM client initialization failed — using deterministic union"
            )
            llm_call, judge_call = None, None

    return [
        merge_group([memories[i] for i in group], llm_call=llm_call, judge_call=judge_call)
        for group in groups
    ]
