"""`maestro delegate` as the transport, per docs/Research.md topic 19.

Reaches the same vendor CLIs as the direct adapters, but through Maestro's
Session/Run lifecycle (ADR-004). What that buys: a persistent execution record, a
knowledge system the agent can search, and message injection into a running
worker. What it costs: a second process layer between this code and the thing
being driven, with its own failure surface.

Two constraints carry over from the direct path and are not optional here:

  * Maestro's `--timeout` is a **stale-stream** timeout — it force-terminates a
    CLI that produces no output for that long. A coding agent streaming its
    reasoning is never idle and may still be making no progress, so that is
    precisely the blind spot docs/Bugs.md #17 is about. A wall-clock deadline
    over the subprocess is applied on top of it regardless.
  * Maestro is installed repo-locally (ADR-007) and is therefore **not on PATH**.
    Resolution goes through config.MAESTRO_BIN. Bugs.md #3 is what happens when
    one consumer assumes otherwise.

This runs delegate synchronously (no `--async`). The graph is already the thing
that sequences work, and a background execution the graph then polls would put
two schedulers in the same pipeline.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from adapters.base import (
    AgentResult,
    classify_failure,
    register_backend,
    resolve_binary,
    run_with_deadline,
)
import config
from config import PROJECT_ROOT, AgentSpec
from logging_config import get_logger

logger = get_logger(__name__)

_CONSOLE_EXCERPT = 200
_DEBUG_BLOCK_LIMIT = 12000


def _debug_block(text: str) -> str:
    if len(text) <= _DEBUG_BLOCK_LIMIT:
        return text
    head = text[:4000]
    tail = text[-4000:]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n... [{omitted} chars omitted] ...\n{tail}"

# Maestro's own vocabulary. A node says what it is doing; this maps that onto the
# role Maestro understands, which selects its prompt protocol.
ROLE_BY_TAG_PREFIX = {
    "requirements": "analyze",
    "planner": "plan",
    "wave_orchestrator": "implement",
    "merger": "implement",
    "reviewer": "review",
    "supervisor": "review",
    "task": "implement",
}


def _resolve_maestro() -> str:
    """config.MAESTRO_BIN -> project-local node_modules -> PATH.

    One configurable code path with an explicit precedence, which is what
    Bugs.md #3's Prevention note asks for. A bare name embedded at a call site is
    what broke when the dependency moved.
    """
    configured = Path(config.MAESTRO_BIN)
    configured_cmd = configured.with_name(f"{configured.name}.cmd")
    if os.name == "nt" and configured_cmd.is_file():
        return str(configured_cmd)
    if configured.is_file():
        return str(configured)

    local = PROJECT_ROOT / "node_modules" / ".bin" / "maestro"
    local_cmd = local.with_name(f"{local.name}.cmd")
    if os.name == "nt" and local_cmd.is_file():
        return str(local_cmd)
    if local.is_file():
        return str(local)
    return resolve_binary("maestro")


def _role_for(tag: str) -> str:
    for prefix, role in ROLE_BY_TAG_PREFIX.items():
        if tag.startswith(prefix):
            return role
    return "implement"


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
    """One delegated turn. Returns a result; never raises."""
    argv = [
        _resolve_maestro(),
        "delegate",
        # The prompt is positional for delegate. It still goes over stdin where
        # the CLI beneath supports it; delegate does not, so it is passed as one
        # argument and the newline hazard is Maestro's to handle.
        prompt if not system_prompt else f"{system_prompt}\n\n{prompt}",
        "--to",
        spec.backend,
        "--role",
        _role_for(tag),
        # Every agent in this pipeline that runs through here either writes files
        # or drives one that does; a read-only mode would silently do nothing.
        "--mode",
        "write",
        "--cd",
        cwd,
        "--id",
        f"bhai-{tag}"[:48],
    ]
    if spec.model:
        argv += ["--model", spec.model]
    if resume_session:
        argv += ["--resume", resume_session]
    # Maestro's stale-stream timeout, in milliseconds. Set below the wall-clock
    # deadline so a genuinely silent CLI is reaped by Maestro first and the
    # harder kill stays the backstop.
    argv += ["--timeout", str(max(30, spec.deadline_seconds - 30) * 1000)]

    logger.debug("[%s] argv: %s", tag, argv)

    started = time.perf_counter()
    try:
        completed = run_with_deadline(
            argv,
            input=None,
            cwd=cwd,
            timeout=spec.deadline_seconds,
        )
    except FileNotFoundError:
        return AgentResult(
            ok=False,
            error_kind="not_installed",
            error_message=(
                f"Maestro was not found at {config.MAESTRO_BIN!r}, in "
                f"{PROJECT_ROOT / 'node_modules' / '.bin'}, or on PATH. "
                "It is installed repo-locally (ADR-007); run `npm install`."
            ),
            duration_seconds=time.perf_counter() - started,
        )
    except subprocess.TimeoutExpired:
        return AgentResult(
            ok=False,
            error_kind="timeout",
            error_message=(
                f"maestro delegate ran past its {spec.deadline_seconds}s wall-clock "
                "deadline and was abandoned. Anything it saved is still on disk."
            ),
            duration_seconds=time.perf_counter() - started,
        )

    elapsed = time.perf_counter() - started
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if stderr:
        logger.debug("[%s] stderr:\n%s", tag, _debug_block(stderr))
    logger.debug("[%s] stdout:\n%s", tag, _debug_block(stdout))

    if completed.returncode != 0:
        # Read the failure from the END of stderr. Maestro prints its own banner
        # first, and so does the CLI underneath it — two banners between the
        # caller and the reason (Bugs.md #23).
        reason = stderr[-500:] or stdout[-500:] or f"maestro exited {completed.returncode}"
        return AgentResult(
            ok=False,
            error_kind=classify_failure(reason),
            error_message=reason,
            duration_seconds=elapsed,
        )
    if not stdout:
        return AgentResult(
            ok=False,
            error_kind="no_output",
            error_message=(
                "maestro delegate exited 0 and produced no output. "
                f"{stderr[-300:] or '(no stderr)'}"
            ),
            duration_seconds=elapsed,
        )

    logger.info("[%s] maestro delegate done | %.1fs", tag, elapsed)
    logger.info("[%s] reply: %s", tag, stdout[:_CONSOLE_EXCERPT])
    # No usage figures cross this boundary in a form worth parsing, and the
    # execution id is Maestro's rather than the vendor's — so it is not a
    # resumable vendor session and is deliberately not returned as one.
    return AgentResult(ok=True, text=stdout, duration_seconds=elapsed)


register_backend("maestro", run)
