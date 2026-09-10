"""Offline embedders for the quality scenarios.

find_near_duplicate_groups needs only `generate_embedding(list[str])` and
`cosine_similarity(a, b)`, and it never inspects what an embedding actually
is - so an embedding here is a sparse dict and the whole thing stays stdlib.

The existing suite's ExactEmbedder scores only byte-identical text as
similar, which cannot exercise near-duplicate merging at all - and
near-duplicate merging is precisely where compaction quality is won or lost.
LexicalEmbedder fills that gap deterministically, with no model download and
no network.

CALIBRATION WARNING, repeated in every report header: bag-of-words cosine at
MERGE_SIMILARITY_THRESHOLD = 0.60 is NOT MiniLM at 0.60. These scenarios
measure the pipeline GIVEN an embedder; they are not a prediction of what the
production embedder will group. That is why every scenario result records the
groups that actually formed - without it, a scenario that expected merging
and got none is indistinguishable from a passing conservation check.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Sequence

import compact_command as command
from quality_keywords import STOPWORDS

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _bag(text: str) -> Counter:
    return Counter(
        token
        for token in (match.group(0).casefold() for match in _TOKEN.finditer(text))
        if len(token) >= 3 and token not in STOPWORDS
    )


class LexicalEmbedder:
    """L2-normalized bag of words; cosine is a sparse dot product.

    Deterministic and order-independent, so a scenario's grouping is a
    property of its corpus rather than of a model version or a random seed.
    """

    name = "lexical"

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def generate_embedding(self, texts: Sequence[str]) -> list[dict[str, float]]:
        self.batches.append(list(texts))
        vectors = []
        for text in texts:
            bag = _bag(text)
            norm = math.sqrt(sum(count * count for count in bag.values())) or 1.0
            vectors.append({token: count / norm for token, count in bag.items()})
        return vectors

    def cosine_similarity(self, left: dict, right: dict) -> float:
        if len(right) < len(left):
            left, right = right, left
        return sum(weight * right.get(token, 0.0) for token, weight in left.items())


class IdenticalOnlyEmbedder:
    """Only byte-identical text is similar.

    Re-implemented rather than imported from conftest.ExactEmbedder: the
    measurement helpers must not depend on the contract suite's fixtures, and
    six lines buys that boundary. Used as the arm that isolates what
    near-duplicate merging actually contributes.
    """

    name = "exact"

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def generate_embedding(self, texts: Sequence[str]) -> list[str]:
        self.batches.append(list(texts))
        return list(texts)

    def cosine_similarity(self, left, right) -> float:
        return float(left == right)


def resolve_embedder(name: str):
    """`noop` is the production NoOpEmbedder from compact_command - the
    literal "what if we did not merge at all" control arm, which is what makes
    the other arms' recall and savings figures interpretable."""
    if name == "lexical":
        return LexicalEmbedder()
    if name == "exact":
        return IdenticalOnlyEmbedder()
    if name == "noop":
        embedder = command.NoOpEmbedder()
        embedder.name = "noop"
        return embedder
    if name == "real":
        # Reachable but uninvested: sentence_transformers is not installed
        # here, and the caller is expected to report that rather than crash.
        from mem_manager.services.dedup_merge import _default_embedder

        return _default_embedder()
    raise ValueError(
        f"unknown embedder {name!r}; expected 'lexical', 'exact', 'noop' or 'real'"
    )
