"""A transport that runs no subprocess.

This is a first-class backend rather than a test mock, for two reasons. The whole
pipeline can be rehearsed offline for free, which matters when a single measured
end-to-end run on the paid path cost $1.05 across nine calls (Research.md topic
16). And every node's own logic — prompt assembly, output parsing, routing,
bound-checking — becomes testable without a CLI installed, which is the only way
"test each agent before implementing the next" is affordable.

Replies are registered by tag. A tag with no reply gets a deliberately useless
one, so a test that forgot to script a step fails on the assertion rather than
passing on a plausible-looking default — the failure mode Bugs.md #15, #21 and
#23 all wear is a plausible empty result, and a stub that invents one would be
manufacturing exactly that.
"""

from __future__ import annotations

import fnmatch
import time
from typing import Any, Callable

from adapters.base import AgentResult, register_backend
from config import AgentSpec
from logging_config import get_logger

logger = get_logger(__name__)

# tag pattern -> reply. A reply is either an AgentResult or a callable taking the
# prompt and returning one, so a test can assert on what it was actually asked.
Reply = AgentResult | Callable[[str], AgentResult]

_replies: list[tuple[str, Reply]] = []
# Every call, in order, for assertions afterwards.
calls: list[dict[str, Any]] = []


def set_reply(tag_pattern: str, reply: Reply) -> None:
    """Script a reply for every tag matching `tag_pattern` (fnmatch syntax).

    Later registrations win, so a test can register a broad default and then
    override one step of it.
    """
    _replies.insert(0, (tag_pattern, reply))


def set_text(tag_pattern: str, text: str, **kwargs: Any) -> None:
    """Shorthand for a successful reply carrying `text`."""
    set_reply(tag_pattern, AgentResult(ok=True, text=text, **kwargs))


def reset() -> None:
    """Forget every scripted reply and every recorded call."""
    _replies.clear()
    calls.clear()


def _lookup(tag: str) -> Reply | None:
    for pattern, reply in _replies:
        if fnmatch.fnmatch(tag, pattern):
            return reply
    return None


def run(
    prompt: str,
    *,
    spec: AgentSpec,
    system_prompt: str,
    cwd: str,
    tag: str,
    tools: tuple[str, ...],
    json_schema: dict[str, Any] | None,
    resume_session: str,
) -> AgentResult:
    started = time.perf_counter()
    calls.append(
        {
            "tag": tag,
            "prompt": prompt,
            "system_prompt": system_prompt,
            "cwd": cwd,
            "tools": tools,
            "json_schema": json_schema,
            "resume_session": resume_session,
            "spec": spec,
        }
    )

    reply = _lookup(tag)
    if reply is None:
        logger.warning("[%s] stub has no scripted reply", tag)
        return AgentResult(
            ok=False,
            error_kind="no_output",
            error_message=(
                f"stub transport has no reply registered for tag {tag!r}. "
                "Register one with adapters.stub.set_reply() or set_text()."
            ),
            duration_seconds=time.perf_counter() - started,
        )

    result = reply(prompt) if callable(reply) else reply
    # A fresh copy each call, so a scripted reply reused across steps does not
    # accumulate one call's duration onto the next.
    return AgentResult(
        ok=result.ok,
        text=result.text,
        structured=result.structured,
        error_kind=result.error_kind,
        error_message=result.error_message,
        cost_usd=result.cost_usd,
        turns=result.turns,
        duration_seconds=time.perf_counter() - started,
        session_id=result.session_id or f"stub-{tag}",
        denials=list(result.denials),
    )


register_backend("stub", run)
