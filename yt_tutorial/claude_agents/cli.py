"""The one place this package talks to the Claude Code CLI.

Everything above this module -- the router, the workers -- only assembles a
prompt and reads a report back. This is where that becomes a process: build the
argv, run it under a deadline, parse the result envelope, and log what it cost.

The CLI is asked for `--output-format json`, which prints a single JSON object
describing the whole turn (result text, token usage, dollar cost, session id,
any permission denials). Its warnings go to stderr, so stdout stays parseable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Sequence

from config import (
    CLAUDE_CODE_BIN,
    CLAUDE_CODE_MAX_BUDGET_USD,
    CLAUDE_CODE_MCP_SERVER_NAME,
    CLAUDE_CODE_MODEL,
    CLAUDE_CODE_PERMISSION_MODE,
    CLAUDE_CODE_TIMEOUT_SECONDS,
    WORKSPACE_DIR,
)
from logging_config import get_logger

logger = get_logger(__name__)

# How much of a prompt or report goes to the console. The debug log keeps all of
# it -- a transcript is thousands of characters and would bury a run at INFO.
_CONSOLE_EXCERPT_CHARS = 200


@dataclass
class ClaudeResult:
    """The outcome of one CLI invocation.

    Mirrors llm/invoker.py's LLMResult: never raises at the boundary, always
    reports whether the call worked, so a caller can route around a failure.
    """

    ok: bool

    # ── Success fields ────────────────────────────────────────────────────────
    text: str = ""
    # Populated only when a json_schema was requested.
    structured: dict | None = None

    # ── Failure fields ────────────────────────────────────────────────────────
    error_kind: str = ""
    error_message: str = ""

    # ── Always populated where the CLI reported them ──────────────────────────
    cost_usd: float = 0.0
    turns: int = 0
    duration_seconds: float = 0.0
    session_id: str = ""
    denials: list = field(default_factory=list)


def mcp_tool_name(tool: str) -> str:
    """The name a bridged tool answers to inside Claude Code."""
    return f"mcp__{CLAUDE_CODE_MCP_SERVER_NAME}__{tool}"


def _build_argv(
    *,
    system_prompt: str,
    builtin_tools: Sequence[str],
    mcp_tools: Sequence[str],
    mcp_config: str,
    json_schema: dict | None,
) -> list[str]:
    """Assemble the command line for one turn.

    `--tools` governs Claude Code's built-in set only, so passing "" there
    switches those off without touching the MCP tools. Combined with
    `--strict-mcp-config` -- which ignores every MCP server the user has
    configured elsewhere -- that leaves exactly the tools this run intends.
    """
    argv = [
        shutil.which(CLAUDE_CODE_BIN) or CLAUDE_CODE_BIN,
        "--print",
        "--output-format",
        "json",
        "--model",
        CLAUDE_CODE_MODEL,
        "--permission-mode",
        CLAUDE_CODE_PERMISSION_MODE,
        "--append-system-prompt",
        system_prompt,
        # An empty list still has to be passed: the default is every tool.
        "--tools",
        ",".join(builtin_tools),
        # Sessions here are single-turn and never resumed; persisting them would
        # leave one file per worker turn in the user's Claude Code history.
        "--no-session-persistence",
    ]
    if mcp_config:
        argv += ["--mcp-config", mcp_config, "--strict-mcp-config"]
        argv += ["--allowedTools", ",".join(mcp_tool_name(t) for t in mcp_tools)]
    if json_schema is not None:
        argv += ["--json-schema", json.dumps(json_schema)]
    if CLAUDE_CODE_MAX_BUDGET_USD > 0:
        argv += ["--max-budget-usd", str(CLAUDE_CODE_MAX_BUDGET_USD)]
    return argv


def _parse_envelope(stdout: str) -> dict | None:
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


def run_claude(
    prompt: str,
    *,
    system_prompt: str,
    tag: str,
    builtin_tools: Sequence[str] = (),
    mcp_tools: Sequence[str] = (),
    mcp_config: str = "",
    json_schema: dict | None = None,
) -> ClaudeResult:
    """Run one Claude Code turn and come back with a result, never an exception.

    The prompt goes over stdin rather than as an argument. Transcripts grow with
    every worker report, and Windows caps a command line at 32k characters --
    long enough to work in testing and fail once a run gets interesting.

    The deadline is the CLI's wall clock. There is no read timeout to fall back
    on here: the process holds its pipes open for as long as it is working, so
    only elapsed time distinguishes a long turn from a stuck one.
    """
    argv = _build_argv(
        system_prompt=system_prompt,
        builtin_tools=builtin_tools,
        mcp_tools=mcp_tools,
        mcp_config=mcp_config,
        json_schema=json_schema,
    )

    tools_in_play = list(mcp_tools) or list(builtin_tools) or ["none"]
    logger.info(
        "[%s] calling Claude Code | model=%s | tools=%s",
        tag,
        CLAUDE_CODE_MODEL,
        ", ".join(tools_in_play),
    )
    logger.debug("[%s] argv: %s", tag, argv)
    logger.debug("[%s] system prompt:\n%s", tag, system_prompt)
    logger.debug("[%s] prompt:\n%s", tag, prompt)

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLAUDE_CODE_TIMEOUT_SECONDS,
            # Built-in file tools resolve relative names against the working
            # directory, so rooting the process in the workspace puts the
            # documents where the project's own tools would have put them.
            cwd=str(WORKSPACE_DIR),
        )
    except FileNotFoundError:
        logger.error(
            "[%s] Claude Code CLI %r not found on PATH; set CLAUDE_CODE_BIN or "
            "install it (https://claude.com/claude-code)",
            tag,
            CLAUDE_CODE_BIN,
        )
        return ClaudeResult(
            ok=False,
            error_kind="not_installed",
            error_message=f"Claude Code CLI {CLAUDE_CODE_BIN!r} was not found on PATH.",
        )
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - started
        logger.error(
            "[%s] Claude Code passed its %ds deadline; abandoning the turn",
            tag,
            CLAUDE_CODE_TIMEOUT_SECONDS,
        )
        return ClaudeResult(
            ok=False,
            error_kind="timeout",
            error_message=f"Claude Code ran past its {CLAUDE_CODE_TIMEOUT_SECONDS}s deadline.",
            duration_seconds=elapsed,
        )

    elapsed = time.perf_counter() - started
    stderr = (completed.stderr or "").strip()
    if stderr:
        logger.debug("[%s] stderr:\n%s", tag, stderr)

    envelope = _parse_envelope(completed.stdout or "")
    if envelope is None:
        logger.error(
            "[%s] Claude Code exited %d without a result object; stderr: %s",
            tag,
            completed.returncode,
            stderr[:500] or "(empty)",
        )
        return ClaudeResult(
            ok=False,
            error_kind="no_output",
            error_message=(
                f"Claude Code exited {completed.returncode} and printed no result "
                f"object. {stderr[:300]}"
            ),
            duration_seconds=elapsed,
        )

    result = ClaudeResult(
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
        # A denied tool call is silent otherwise: the worker just reports less
        # than it was asked for, and the supervisor cannot tell why.
        logger.warning(
            "[%s] Claude Code was denied %d tool call(s): %s",
            tag,
            len(result.denials),
            ", ".join(sorted({d.get("tool_name", "?") for d in result.denials})),
        )

    if not result.ok:
        # A refusal the CLI itself produced -- a usage limit, most often -- comes
        # back as is_error with subtype still "success", because the turn ran to
        # completion and only its content is a failure. Labelling that "success"
        # in the log is useless, so fall through to whatever field did say what
        # happened.
        kind = envelope.get("subtype") or ""
        if kind in ("", "success"):
            kind = envelope.get("api_error_status") or envelope.get("terminal_reason") or "error"
        result.error_kind = str(kind)
        result.error_message = result.text or "unknown error"
        logger.error(
            "[%s] Claude Code failed after %.1fs (%s): %s",
            tag,
            elapsed,
            result.error_kind,
            result.error_message[:300],
        )
        return result

    logger.info(
        "[%s] Claude Code done | %.1fs | %d turn(s) | $%.4f | session=%s",
        tag,
        elapsed,
        result.turns,
        result.cost_usd,
        result.session_id[:8] or "-",
    )
    logger.info("[%s] reply: %s", tag, result.text[:_CONSOLE_EXCERPT_CHARS])
    logger.debug("[%s] full reply:\n%s", tag, result.text)
    return result
