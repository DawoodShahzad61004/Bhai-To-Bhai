"""The Claude Code CLI as a subprocess.

Modelled directly on yt_tutorial/claude_agents/cli.py, which is the only code in
this repository that has actually driven this CLI successfully. Everything here
that looks fussy is a defect from docs/Bugs.md wearing a fix.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

from adapters.base import (
    AgentResult,
    classify_failure,
    register_backend,
    resolve_binary,
    run_with_deadline,
)
import config
from config import AgentSpec
from logging_config import get_logger

logger = get_logger(__name__)

# How much of a reply reaches the console. The debug file keeps all of it; a
# report is thousands of characters and would bury a run at INFO.
_CONSOLE_EXCERPT = 200
_DEBUG_BLOCK_LIMIT = 12000


def _debug_block(text: str) -> str:
    if len(text) <= _DEBUG_BLOCK_LIMIT:
        return text
    head = text[:4000]
    tail = text[-4000:]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n... [{omitted} chars omitted] ...\n{tail}"


def _build_argv(
    *,
    spec: AgentSpec,
    system_prompt: str,
    tools: tuple[str, ...],
    json_schema: dict[str, Any] | None,
    resume_session: str,
    extra_dirs: tuple[str, ...],
) -> list[str]:
    argv = [
        resolve_binary(config.CLAUDE_BIN),
        "--print",
        "--output-format",
        "json",
        "--permission-mode",
        config.CLAUDE_PERMISSION_MODE,
    ]
    # An empty model defers to whatever the CLI itself selects. Never hardcode a
    # model into an adapter: entitlement is an account property, and a rejection
    # is indistinguishable from an empty result (Bugs.md #23).
    if spec.model:
        argv += ["--model", spec.model]
    if system_prompt:
        argv += ["--append-system-prompt", system_prompt]
    if tools:
        argv += ["--tools", ",".join(tools)]
    if extra_dirs:
        # Grants tool access outside cwd — a coding worktree is separate from
        # the target checkout that owns the shared artifact directory.
        argv += ["--add-dir", *extra_dirs]
    if json_schema is not None:
        argv += ["--json-schema", json.dumps(json_schema)]
    if spec.max_budget_usd > 0:
        argv += ["--max-budget-usd", str(spec.max_budget_usd)]
    if resume_session:
        # The reviewer's rework loop reaches the same coding subagent through
        # this, which is what makes "here is what you got wrong" refer to
        # anything the agent remembers doing.
        argv += ["--resume", resume_session]
    # There is deliberately no --no-session-persistence on the cold-start branch.
    # The flag's own help says sessions "will not be saved to disk and cannot be
    # resumed", and a turn run under it still hands back a session_id — so the id
    # looked resumable, was recorded as one, and --resume rejected it with "No
    # conversation found with session ID". That is not a cold start, it is a
    # failed turn: the rework loop asked for a session discarded at birth. The
    # price of persistence is one session file per dispatch in the user's own
    # Claude Code history, and that is what the rework loop costs.
    return argv


def _parse_envelope(stdout: str) -> dict[str, Any] | None:
    """The result object, tolerating anything the CLI printed around it."""
    stdout = stdout.strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
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
    extra_dirs: tuple[str, ...] = (),
) -> AgentResult:
    """One Claude Code turn. Returns a result; never raises.

    The prompt goes over **stdin**, always. Windows caps a command line at 32k
    characters — long enough to work in testing and fail once a run gets
    interesting — and argument handling belongs to the platform rather than to
    the CLI (Bugs.md #23).

    The deadline is wall-clock over the whole turn. There is no read timeout to
    fall back on: the process holds its pipes open for as long as it is working,
    so only elapsed time distinguishes a long turn from a stuck one (Bugs.md #17).
    """
    argv = _build_argv(
        spec=spec,
        system_prompt=system_prompt,
        tools=tools,
        json_schema=json_schema,
        resume_session=resume_session,
        extra_dirs=extra_dirs,
    )
    logger.debug("[%s] argv: %s", tag, argv)
    logger.debug("[%s] system prompt:\n%s", tag, _debug_block(system_prompt))
    logger.debug("[%s] prompt:\n%s", tag, _debug_block(prompt))

    started = time.perf_counter()
    try:
        completed = run_with_deadline(
            argv,
            input=prompt,
            cwd=cwd,
            timeout=spec.deadline_seconds,
        )
    except FileNotFoundError:
        return AgentResult(
            ok=False,
            error_kind="not_installed",
            error_message=(
                f"Claude Code CLI {config.CLAUDE_BIN!r} was not found from this process's "
                "environment. Set CLAUDE_BIN or install it."
            ),
            duration_seconds=time.perf_counter() - started,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - started
        return AgentResult(
            ok=False,
            error_kind="timeout",
            error_message=(
                f"Claude Code ran past its {spec.deadline_seconds}s deadline and was "
                "abandoned. Anything it saved before then is still on disk."
            ),
            duration_seconds=elapsed,
        )

    elapsed = time.perf_counter() - started
    stderr = (completed.stderr or "").strip()
    if stderr:
        logger.debug("[%s] stderr:\n%s", tag, _debug_block(stderr))

    envelope = _parse_envelope(completed.stdout or "")
    if envelope is None:
        return AgentResult(
            ok=False,
            error_kind=classify_failure(stderr, default="no_output"),
            error_message=(
                f"Claude Code exited {completed.returncode} and printed no result "
                f"object. {stderr[-300:] or '(no stderr)'}"
            ),
            duration_seconds=elapsed,
        )

    result = AgentResult(
        ok=not envelope.get("is_error", False) and envelope.get("subtype") == "success",
        text=(envelope.get("result") or "").strip(),
        structured=envelope.get("structured_output"),
        cost_usd=float(envelope.get("total_cost_usd") or 0.0),
        turns=int(envelope.get("num_turns") or 0),
        duration_seconds=elapsed,
        session_id=envelope.get("session_id") or "",
        denials=envelope.get("permission_denials") or [],
    )

    if result.denials:
        # A denied tool call is otherwise silent: the agent simply reports less
        # than it was asked for, and the caller cannot tell why.
        logger.warning(
            "[%s] denied %d tool call(s): %s",
            tag,
            len(result.denials),
            ", ".join(sorted({d.get("tool_name", "?") for d in result.denials})),
        )

    if not result.ok:
        # A refusal the CLI produced itself — a usage limit, most often — comes
        # back as is_error with subtype still "success", because the turn ran to
        # completion and only its content is a failure. Fall through to whichever
        # field did say what happened.
        kind = envelope.get("subtype") or ""
        if kind in ("", "success"):
            kind = envelope.get("api_error_status") or envelope.get("terminal_reason") or ""
        message = result.text or str(kind) or "unknown error"
        result.error_kind = classify_failure(f"{kind} {message}")
        result.error_message = message
        return result

    logger.info(
        "[%s] claude done | %.1fs | %d turn(s) | $%.4f | session=%s",
        tag,
        elapsed,
        result.turns,
        result.cost_usd,
        result.session_id[:8] or "-",
    )
    logger.info("[%s] reply: %s", tag, result.text[:_CONSOLE_EXCERPT])
    logger.debug("[%s] full reply:\n%s", tag, _debug_block(result.text))
    return result


register_backend("direct:claude", run)
