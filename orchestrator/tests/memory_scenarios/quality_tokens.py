"""Pluggable token counters for the compaction-savings measurement.

`mem_manager` counts tokens nowhere: every size decision it makes is in
characters (`MIN_PRUNE_BUDGET` against `sum(len(m.content))` in
consolidate.py, and against `st_size` in compact_command.py), and the only
token figure anywhere in the repository is a log string, `input_chars // 4`
at services/llm_caller.py:312. So a "tokens freed by compaction" number has
to bring its own counter.

Three are offered, and every report records which one ran, because a
`tokens_freed` figure produced by one counter is not comparable with one
produced by another:

  regex-approx-v1     the default, and what every test pins. Deterministic,
                      stdlib-only, and closer to BPE behaviour on
                      marker-heavy markdown than a flat chars/4.
  chars-div-4         `len(text) // 4`, matching llm_caller.py:312 exactly,
                      so a measurement here can be cross-checked against the
                      one estimate the codebase already ships.
  tiktoken:cl100k_base  used only when tiktoken both imports AND can build an
                      encoding without touching the network.

tiktoken is deliberately NOT added to requirements-dev.txt. It downloads its
BPE vocabulary on first use, which would make this suite network-dependent
and would make the metric silently machine-dependent - a counter that works
on one developer's machine and degrades on another's is worse than a crude
counter that behaves identically everywhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

# One alternation, ordered, covering every character exactly once: alphabetic
# runs, digit runs, newlines, intra-line whitespace, then anything else as a
# single character. DOTALL so the final `.` also catches a stray \r.
_RUN_PATTERN = re.compile(r"([^\W\d_]+)|(\d+)|(\n)|([ \t]+)|(.)", re.DOTALL)

_ALPHA_CHARS_PER_TOKEN = 4
_DIGIT_CHARS_PER_TOKEN = 3
# Below this length a word is one BPE token far more often than not, so
# charging ceil(len/4) would over-count every short word in the corpus.
_ALPHA_SPLIT_THRESHOLD = 5


@dataclass(frozen=True)
class TokenCounter:
    """A named counting strategy. The name travels with every figure it
    produces; `compare`-style diffing across two different names is
    meaningless and callers are expected to refuse it."""

    name: str
    count: Callable[[str], int]


def _ceil_div(value: int, divisor: int) -> int:
    return -(-value // divisor)


def regex_approx_count(text: str) -> int:
    """Charge one token per punctuation character and per newline, nothing
    for intra-line whitespace, `ceil(len/4)` for alphabetic runs of 5+
    characters and 1 for shorter ones, and `ceil(len/3)` for digit runs.

    The punctuation and newline rules are what make this worth having over
    chars/4: `_memories_to_markdown` emits four or five
    `<!-- key:value -->` marker lines per memory, which are almost entirely
    punctuation, and a chars/4 estimate under-counts exactly that overhead -
    the overhead this suite exists to measure.
    """
    total = 0
    for match in _RUN_PATTERN.finditer(text):
        alpha, digits, newline, _spaces, other = match.groups()
        if alpha is not None:
            total += (
                _ceil_div(len(alpha), _ALPHA_CHARS_PER_TOKEN)
                if len(alpha) >= _ALPHA_SPLIT_THRESHOLD
                else 1
            )
        elif digits is not None:
            total += _ceil_div(len(digits), _DIGIT_CHARS_PER_TOKEN)
        elif newline is not None or other is not None:
            total += 1
    return total


def chars_div_four_count(text: str) -> int:
    """`len(text) // 4` - the estimate services/llm_caller.py:312 logs."""
    return len(text) // 4


def _tiktoken_counter() -> TokenCounter | None:
    # Broad except on purpose: tiktoken raises ImportError when absent, and a
    # network/cache error when present but unable to fetch its vocabulary.
    # Both mean "unavailable here", and neither should abort a measurement.
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        encoding.encode("probe")
    except Exception:
        return None
    return TokenCounter(
        name="tiktoken:cl100k_base",
        count=lambda text: len(encoding.encode(text)),
    )


class TokenizerUnavailableError(RuntimeError):
    """A specific counter was asked for by name and cannot be provided.

    Raised rather than silently downgraded: a token figure whose counter
    quietly changed is a number nobody can interpret later.
    """


def resolve_token_counter(preferred: str = "regex") -> TokenCounter:
    if preferred == "regex":
        return TokenCounter(name="regex-approx-v1", count=regex_approx_count)
    if preferred == "chars":
        return TokenCounter(name="chars-div-4", count=chars_div_four_count)
    if preferred == "tiktoken":
        counter = _tiktoken_counter()
        if counter is None:
            raise TokenizerUnavailableError(
                "tiktoken is unavailable (not installed, or unable to build "
                "cl100k_base without network access); use 'regex' or 'chars'"
            )
        return counter
    if preferred == "auto":
        return _tiktoken_counter() or TokenCounter(
            name="regex-approx-v1", count=regex_approx_count
        )
    raise ValueError(
        f"unknown tokenizer {preferred!r}; expected one of "
        "'regex', 'chars', 'tiktoken', 'auto'"
    )
