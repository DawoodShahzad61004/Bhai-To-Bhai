"""Reading structure back out of an agent's reply.

Four of the six agents need a decision, not prose: a list of questions, a task
decomposition, a verdict. Claude Code can be handed a `--json-schema` and will
return `structured_output`; Codex cannot, and returns whatever it wrote. Both
have to work, so parsing is tolerant on the way in and strict on the way out.

The strictness matters more than the tolerance. An agent that returns something
unparseable must produce a *failure*, never a default — a plausible-looking
default is exactly the shape of docs/Bugs.md #15, #21 and #23, where a run
reported success having done nothing. So `require_*` raises nothing and returns
no fallback: it reports what was missing, and the caller routes on that.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from logging_config import get_logger

logger = get_logger(__name__)

# ```json { ... } ``` or ``` { ... } ```
_FENCE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class Parsed:
    """A parse attempt: the value, or why there isn't one."""

    ok: bool
    value: dict[str, Any] | None = None
    error: str = ""


def _balanced_objects(text: str) -> list[str]:
    """Every brace-balanced `{...}` span in `text`, outermost first.

    A regex cannot do this — nested objects are not a regular language — and an
    agent's reply routinely wraps its JSON in explanation on both sides.
    """
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start : index + 1])
                start = -1
            elif depth < 0:
                depth = 0
    return spans


def extract_json(text: str, structured: dict[str, Any] | None = None) -> Parsed:
    """Get an object out of an agent reply, however it chose to present one.

    Order of preference, most to least trustworthy:
      1. `structured` — the CLI honoured a json_schema, so this is guaranteed.
      2. The whole reply, if it is itself JSON.
      3. A fenced ```json block.
      4. Any brace-balanced object in the prose, last one first — an agent that
         restates its answer puts the final version last.
    """
    if isinstance(structured, dict) and structured:
        return Parsed(ok=True, value=structured)

    text = (text or "").strip()
    if not text:
        return Parsed(ok=False, error="the agent replied with nothing at all")

    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return Parsed(ok=True, value=value)
    except json.JSONDecodeError:
        pass

    candidates: list[str] = []
    for match in _FENCE.finditer(text):
        candidates.append(match.group("body").strip())
    candidates.extend(reversed(_balanced_objects(text)))

    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return Parsed(ok=True, value=value)

    return Parsed(
        ok=False,
        error=f"no JSON object found in a {len(text)}-character reply",
    )


def require_str(payload: dict[str, Any], key: str, *, allow_empty: bool = False) -> str | None:
    """A string field, or None when it is absent or the wrong type."""
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    if not allow_empty and not value.strip():
        return None
    return value


def require_list(payload: dict[str, Any], key: str) -> list[Any] | None:
    value = payload.get(key)
    return value if isinstance(value, list) else None


def string_list(payload: dict[str, Any], key: str) -> list[str]:
    """A list of non-empty strings, dropping anything that is neither.

    Lenient because these are secondary fields — a question that came back as a
    number is worth skipping, not worth failing a run over.
    """
    raw = payload.get(key)
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def joined_and_capped(items: list[str], *, limit: int = 20, empty: str = "") -> str:
    """Comma-join `items`, capped at `limit` entries with a "+N more" suffix.

    A task whose own commands left thousands of files behind — an installed
    `node_modules`, say — must not turn a one-line evidence summary into a
    log line or reviewer-prompt payload hundreds of thousands of characters
    long (docs/Bugs.md #51). The count that overflows is itself the useful
    signal, so it is kept rather than silently dropped.
    """
    if not items:
        return empty
    shown = items[:limit]
    text = ", ".join(shown)
    remaining = len(items) - len(shown)
    if remaining > 0:
        text += f", and {remaining} more file(s)"
    return text


def one_of(payload: dict[str, Any], key: str, allowed: tuple[str, ...]) -> str | None:
    """A field constrained to a closed set, matched case-insensitively.

    Verdicts go through here. A verdict outside the set is not coerced to the
    safe option: a reviewer whose reply cannot be read has not approved anything,
    and the caller has to decide what that means.
    """
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    normalised = value.strip().lower()
    for candidate in allowed:
        if normalised == candidate.lower():
            return candidate
    return None
