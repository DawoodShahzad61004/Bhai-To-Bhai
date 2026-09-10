"""Per-sentence keyword ground truth for the compaction-quality measurement.

Splits an episodic markdown file into sentences, records the important
keywords of each, and provides the matching side used to ask whether those
keywords survived compaction.

Three decisions here shape every number the suite reports:

MATCH TARGET IS ASYMMETRIC, DELIBERATELY. Tokens are counted over the whole
output *file* (so `_memories_to_markdown`'s marker overhead is charged
honestly), but keywords are matched against memory *content* only. Marker
lines carry 12-hex-character content ids, and a hex id token-matches a
numeric keyword often enough to inflate recall for free.

MATCHING IS TOKEN-BASED, NOT SUBSTRING-BASED. Substring matching would make
this metric structurally blind to the loss it exists to detect: `id` occurs
inside "identifier", `run` inside "running", and - fatally - every keyword of
a *deleted* record still "matches" whenever a surviving record happens to
share a prefix. The single exception is multi-word keywords (quoted phrases
and backtick spans containing spaces), which match as whitespace-normalized
substrings; `multiword_keyword_count` is reported so a reader can see how
much of a score leans on that exception.

THERE IS NO STEMMER. A hand-rolled Porter-lite is roughly sixty rules whose
failures are silent, asymmetric, and unreviewable in a diff. In its place is
one explicit rule - a keyword matches its own plural and vice versa, when
both are purely alphabetic and at least four characters.

The consequence worth stating plainly: a legitimate paraphrase
(`failed` -> `did not succeed`) is scored as a loss. This metric is a
CONSERVATIVE FLOOR on quality, not an estimate of it. That is why live-lane
recall sits below offline recall, and that gap must not be read as a
regression.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import config
from mem_manager.importance import parse_episodic_md

# Bumped by hand when extraction changes. The golden files record it, and
# regeneration refuses to overwrite a golden built by a different version
# unless the bump is deliberate - so extractor drift shows up as a reviewable
# JSON diff instead of a silently moving metric.
EXTRACTOR_VERSION = 1

STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are aren as at
    be because been before being below between both but by
    can cannot could couldn
    did didn do does doesn doing don down during
    each few for from further
    had hadn has hasn have haven having he her here hers herself him himself
    his how however
    i if in into is isn it its itself
    just
    let
    me more most must mustn my myself
    no nor not now
    of off on once only or other others ought our ours ourselves out over own
    rather really
    same shan she should shouldn so some such
    than that the their theirs them themselves then there these they this
    those through to too
    under until up upon use used using
    very
    was wasn we were weren what when where whether which while who whom why
    will with within without won would wouldn
    you your yours yourself yourselves
    """.split()
)

# --- segmentation ------------------------------------------------------------

_FENCE_OPEN = re.compile(r"^(`{3,}|~{3,})")
_MARKER_LINE = re.compile(r"^<!--.*-->$")
_LIST_OR_HEADING = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s+)(?P<text>.*)$")
# Group 1 is the closing quote/bracket run, captured so it stays attached to
# the sentence it closes rather than opening the next one.
_SENTENCE_BOUNDARY = re.compile(
    "(?<=[.!?])([\"')\\]]*)(\\s+)(?=[A-Z(\\[`\"'])"
)
_ABBREVIATIONS = frozenset(
    {"e.g.", "i.e.", "vs.", "etc.", "cf.", "al.", "fig.", "no.", "approx.", "resp."}
)
_TRAILING_INITIAL = re.compile(r"\b[A-Z]\.$")
_TRAILING_DECIMAL = re.compile(r"\d\.$")


def _blocks_sentence_split(head: str) -> bool:
    head = head.rstrip()
    if not head:
        return True
    if head.split()[-1].casefold() in _ABBREVIATIONS:
        return True
    return bool(_TRAILING_INITIAL.search(head) or _TRAILING_DECIMAL.search(head))


def _split_paragraph(text: str) -> list[str]:
    pieces: list[str] = []
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(text):
        head = text[start : match.start()] + match.group(1)
        if _blocks_sentence_split(head):
            continue
        pieces.append(head.strip())
        start = match.end()
    tail = text[start:].strip()
    if tail:
        pieces.append(tail)
    return [piece for piece in pieces if piece]


def split_sentences(body: str) -> list[str]:
    """Segment one record body into sentence units.

    A fenced code block is ONE atomic unit - splitting code on `.` produces
    nonsense, and a path or version number inside it would fragment into
    meaningless pieces. A list item or heading is its own unit with the
    marker stripped, which is also what stops a bullet's own `1.` from
    becoming a trivially-matched numeric keyword. `<!-- ... -->` marker lines
    and the lone `---` that `_deterministic_union` joins merged content with
    are dropped, so a generation-1 file and a generation-5 file segment the
    same way.
    """
    units: list[str] = []
    paragraph: list[str] = []
    fence: str | None = None
    fenced: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            joined = " ".join(paragraph).strip()
            paragraph.clear()
            if joined:
                units.extend(_split_paragraph(joined))

    for line in body.splitlines():
        stripped = line.strip()
        if fence is not None:
            fenced.append(line)
            if stripped.startswith(fence):
                units.append("\n".join(fenced))
                fence, fenced = None, []
            continue
        opening = _FENCE_OPEN.match(stripped)
        if opening:
            flush_paragraph()
            fence, fenced = opening.group(1), [line]
            continue
        if not stripped or stripped == "---" or _MARKER_LINE.match(stripped):
            flush_paragraph()
            continue
        item = _LIST_OR_HEADING.match(line)
        if item:
            flush_paragraph()
            units.append(item.group("text").strip())
            continue
        paragraph.append(stripped)

    if fence is not None:  # unterminated fence: keep what we have, atomically
        units.append("\n".join(fenced))
    flush_paragraph()
    return [unit for unit in units if unit]


# --- extraction --------------------------------------------------------------

_BACKTICK_SPAN = re.compile("`([^`\\n]+)`")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PATH = re.compile(
    r"[\w.\-]+(?:/[\w.\-]+)+"
    r"|[\w\-]+\.(?:py|md|json|txt|toml|ini|cfg|ya?ml|js|ts|lock|sh|ps1)\b"
)
# The lookbehind is load-bearing: without it the hyphen inside an ordinary
# compound word ("danger-full-access", "auto-rejected") reads as a flag and
# injects a keyword no writer ever wrote.
_FLAG = re.compile(r"(?<![\w-])--?[A-Za-z][\w-]*")
_NUMBER = re.compile(r"\d+(?:\.\d+)*%?")
_QUOTED = re.compile("\"([^\"\\n]{2,}?)\"|'([^'\\n]{2,}?)'")
_ALLCAPS = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
_CAMEL = re.compile(r"\b[A-Za-z]+[a-z0-9][A-Z][A-Za-z0-9]*\b")
_CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]+")
_CAPITALIZED = re.compile(r"\b[A-Z][a-z]{2,}\b")
# `_` is excluded from the class, so `compact_markdown` yields `compact` and
# `markdown` - the same decomposition tokenize_for_match performs on output.
_WORD = re.compile(r"[^\W\d_]{3,}")

_QUOTE_CHARS = "`\"'\u201c\u201d\u2018\u2019"
_TRAILING_PUNCTUATION = ",;:.!?"


def normalize(token: str) -> str:
    """Casefold and shed the decoration that carries no meaning.

    A trailing `()` is stripped so `compact_markdown()` matches
    `compact_markdown`. Without that rule, every LLM rewrite that reformats a
    call - which changes nothing about the fact being remembered -
    manufactures a false "lost keyword" report.
    """
    text = token.strip().casefold()
    # Iterate to a fixed point rather than in a fixed order: real tokens nest
    # their decoration ("`compact_markdown()`," wraps a backtick outside a
    # paren outside a comma), and any single ordering leaves one layer on.
    previous = None
    while text != previous:
        previous = text
        text = text.strip(_QUOTE_CHARS).strip()
        text = text.rstrip(_TRAILING_PUNCTUATION)
        if text.endswith("()"):
            text = text[:-2]
        # Unbalanced wrapping parens, e.g. the "(exit" of "(exit 1)". A
        # balanced "f(1.5)" is left alone - the parens are part of the token.
        if text.endswith(")") and "(" not in text:
            text = text[:-1]
        if text.startswith("(") and ")" not in text:
            text = text[1:]
    return text


def _is_identifier_like(token: str) -> bool:
    """Bare alphabetic words are Pass B's job; Pass A wants only the ones
    whose shape marks them as code - an underscore or an embedded digit."""
    return "_" in token or any(character.isdigit() for character in token)


def extract_keywords(sentence: str) -> tuple[str, ...]:
    """Two unioned passes: high-signal code-shaped patterns, then content
    words. Order is preserved and duplicates dropped, so a golden file reads
    in the order a human reads the sentence."""
    seen: set[str] = set()
    found: list[str] = []

    def add(token: str | None) -> None:
        if not token:
            return
        normalized = normalize(token)
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)

    for match in _BACKTICK_SPAN.finditer(sentence):
        span = match.group(1).strip()
        add(span)
        for inner in _IDENTIFIER.finditer(span):
            if _is_identifier_like(inner.group(0)):
                add(inner.group(0))
    for match in _PATH.finditer(sentence):
        add(match.group(0))
    for match in _FLAG.finditer(sentence):
        add(match.group(0))
    for match in _ALLCAPS.finditer(sentence):
        add(match.group(0))
    for match in _CAMEL.finditer(sentence):
        add(match.group(0))
    for match in _CJK.finditer(sentence):
        add(match.group(0))
    for match in _QUOTED.finditer(sentence):
        add(match.group(1) or match.group(2))
    for match in _IDENTIFIER.finditer(sentence):
        if _is_identifier_like(match.group(0)):
            add(match.group(0))
    for match in _NUMBER.finditer(sentence):
        add(match.group(0))
    for match in _CAPITALIZED.finditer(sentence):
        if match.start() != 0:  # sentence-initial capitals are grammar, not signal
            add(match.group(0))
    for match in _WORD.finditer(sentence):
        word = match.group(0).casefold()
        if word not in STOPWORDS:
            add(word)
    return tuple(found)


def tokenize_for_match(text: str) -> set[str]:
    """The output side of the metric: every normalized token form a keyword
    could legitimately be found as.

    Emits whole whitespace chunks (so paths, flags and punctuated identifiers
    survive intact), identifiers, decomposed alphabetic runs, numbers, CJK
    runs, and backtick-span contents. The decomposition is what lets the
    keyword `json` match an output that only ever writes `--json`.
    """
    tokens: set[str] = set()

    def add(token: str | None) -> None:
        if not token:
            return
        normalized = normalize(token)
        if normalized:
            tokens.add(normalized)

    for chunk in text.split():
        add(chunk)
    for pattern in (_IDENTIFIER, _WORD, _NUMBER, _CJK, _FLAG, _PATH):
        for match in pattern.finditer(text):
            add(match.group(0))
    for match in _BACKTICK_SPAN.finditer(text):
        add(match.group(1))
    return tokens


_PLURAL_MIN_LENGTH = 4


def matches(keyword: str, tokens: set[str], collapsed_text: str) -> bool:
    """Is `keyword` present, under the one plural rule and the one multi-word
    exception documented at module level?"""
    if " " in keyword:
        return keyword in collapsed_text
    if keyword in tokens:
        return True
    if len(keyword) >= _PLURAL_MIN_LENGTH and keyword.isalpha():
        if keyword + "s" in tokens:
            return True
        if keyword.endswith("s") and keyword[:-1] in tokens:
            return True
    return False


def collapse_for_multiword(text: str) -> str:
    return " ".join(text.casefold().split())


# --- assembling the ground truth ---------------------------------------------


@dataclass(frozen=True)
class Sentence:
    index: int
    record_index: int
    record_tag: str
    text: str
    keywords: tuple[str, ...]
    rare: bool


def _tag_from_header(header: str) -> str:
    choices = config.USER_CHOICES_HEADER_PATTERN.match(header)
    if choices:
        return choices.group(1)
    parts = header[3:].strip().split(None, 3)
    return parts[3] if len(parts) >= 4 else header[3:].strip()


def _raw_blocks(text: str) -> Iterator[tuple[str, str]]:
    """Every `## `-led block, VALID HEADER OR NOT.

    This is the whole reason the `raw` denominator exists. Derive sentences
    from `parse_episodic_md` and a record the parser silently dropped leaves
    the denominator along with the numerator - the adversarial scenario then
    scores a flawless 1.0 while having lost an entire record.
    """
    for block in config.EPISODIC_BLOCK_SPLIT_PATTERN.split(text.strip()):
        block = block.strip()
        if not block.startswith("## "):
            continue
        header, _, body = block.partition("\n")
        yield _tag_from_header(header), body


def _finalize(rows: Sequence[tuple[int, str, str, tuple[str, ...]]]) -> list[Sentence]:
    appearances = Counter(
        keyword for _, _, _, keywords in rows for keyword in set(keywords)
    )
    return [
        Sentence(
            index=index,
            record_index=record_index,
            record_tag=tag,
            text=text,
            keywords=keywords,
            rare=all(appearances[keyword] == 1 for keyword in keywords),
        )
        for index, (record_index, tag, text, keywords) in enumerate(rows)
    ]


def _rows_from(bodies: Iterable[tuple[str, str]]) -> list[Sentence]:
    rows: list[tuple[int, str, str, tuple[str, ...]]] = []
    for record_index, (tag, body) in enumerate(bodies):
        for text in split_sentences(body):
            keywords = extract_keywords(text)
            if keywords:  # a keywordless sentence would only deflate coverage
                rows.append((record_index, tag, text, keywords))
    return _finalize(rows)


def sentences_for_markdown(text: str, *, denominator: str = "parsed") -> list[Sentence]:
    """`parsed` scores against what the pipeline actually ingested; `raw`
    scores against everything the file visibly contains. Where the two
    disagree, the gap IS the silent-drop failure mode, as a number."""
    if denominator == "parsed":
        records = parse_episodic_md(text)
        return _rows_from((record.tag, record.content) for record in records)
    if denominator == "raw":
        return _rows_from(_raw_blocks(text))
    raise ValueError(f"unknown denominator {denominator!r}; expected 'parsed' or 'raw'")


def rare_keywords(sentences: Sequence[Sentence]) -> set[str]:
    """Keywords occurring in exactly one sentence.

    Type recall hides losses behind keywords that happen to recur elsewhere;
    these cannot hide, which makes recall over them the sharpest single
    figure in the report.
    """
    appearances = Counter(
        keyword for sentence in sentences for keyword in set(sentence.keywords)
    )
    return {keyword for keyword, count in appearances.items() if count == 1}


# --- recorded ground truth ---------------------------------------------------

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"

# Goldens are always recorded on the `raw` denominator: it is the superset, so
# a record the parser silently drops still has its keywords on file. That is
# what lets the adversarial scenario assert the exact set of keywords a drop
# cost, instead of comparing against a fraction somebody picked.
GOLDEN_DENOMINATOR = "raw"


def golden_path(corpus_name: str) -> Path:
    return CORPUS_DIR / (Path(corpus_name).stem + ".keywords.json")


def corpus_path(corpus_name: str) -> Path:
    return CORPUS_DIR / corpus_name


def read_corpus(corpus_name: str) -> str:
    # utf-8-sig, matching compact_markdown_file: a BOM read as plain utf-8
    # attaches to the first header and silently costs that record.
    return corpus_path(corpus_name).read_text(encoding="utf-8-sig")


def build_golden(corpus_name: str) -> dict:
    raw_bytes = corpus_path(corpus_name).read_bytes()
    sentences = sentences_for_markdown(
        read_corpus(corpus_name), denominator=GOLDEN_DENOMINATOR
    )
    return {
        "corpus": corpus_name,
        "extractor_version": EXTRACTOR_VERSION,
        "source_sha1": hashlib.sha1(raw_bytes).hexdigest(),
        "denominator": GOLDEN_DENOMINATOR,
        "record_count": len(list(_raw_blocks(read_corpus(corpus_name)))),
        "sentence_count": len(sentences),
        "keyword_count": len({k for s in sentences for k in s.keywords}),
        "sentences": [
            {
                "index": sentence.index,
                "record_index": sentence.record_index,
                "record_tag": sentence.record_tag,
                "sentence": sentence.text,
                "keywords": list(sentence.keywords),
                "rare": sentence.rare,
            }
            for sentence in sentences
        ],
    }


def load_golden(corpus_name: str) -> dict:
    return json.loads(golden_path(corpus_name).read_text(encoding="utf-8"))


def write_golden(corpus_name: str) -> Path:
    path = golden_path(corpus_name)
    path.write_text(
        json.dumps(build_golden(corpus_name), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def golden_keywords_for_records(golden: dict, record_tags: Sequence[str]) -> set[str]:
    """Every keyword the named records contribute - the exact set a drop of
    those records costs."""
    wanted = set(record_tags)
    return {
        keyword
        for entry in golden["sentences"]
        if entry["record_tag"] in wanted
        for keyword in entry["keywords"]
    }
