"""The Gemini CLI as a subprocess.

This adapter follows the same boundary as the other direct coding-agent
transports: vendor-specific CLI behaviour is normalized here and every failure
returns an ``AgentResult`` instead of escaping as an exception.

Gemini CLI is driven in headless ``stream-json`` mode because its init event
contains the resumable session id and its result event carries final status and
usage metadata. Prompts are sent over stdin so Windows command-line length and
quoting rules cannot truncate a real task brief.
"""

from __future__ import annotations

import json
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
from config import AgentSpec
from logging_config import get_logger

logger = get_logger(__name__)

_CONSOLE_EXCERPT = 200
_DEBUG_BLOCK_LIMIT = 12000
_MAX_INCLUDE_DIRS = 5
_VALID_APPROVAL_MODES = {"default", "auto_edit", "yolo", "plan"}


def _debug_block(text: str) -> str:
    if len(text) <= _DEBUG_BLOCK_LIMIT:
        return text
    head = text[:4000]
    tail = text[-4000:]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n... [{omitted} chars omitted] ...\n{tail}"


def _unquote_dotenv(value: str) -> str:
    """Return the literal value for the simple KEY=VALUE entries we consume.

    The project deliberately keeps runtime dependencies small, so the adapter
    does not add python-dotenv merely to read two values. This parser is narrow
    on purpose: it supports optional ``export`` prefixes and single/double
    quoted values, which is sufficient for API keys and URLs.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value



def _load_gemini_dotenv() -> str:
    """Load GEMINI_API_KEY and GEMINI_API_BASE from the controller's .env.

    The orchestrator owns credentials, not the target repository. Reading from
    ``config.PROJECT_ROOT/.env`` also prevents a task checkout from silently
    changing which account or endpoint the controller uses.
    """
    env_path = Path(config.PROJECT_ROOT) / ".env"
    wanted = {"GEMINI_API_KEY", "GEMINI_API_BASE"}
    values: dict[str, str] = {}

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or key not in wanted:
            continue
        values[key] = _unquote_dotenv(value)

    # The explicit project .env is authoritative when present. Falling back to
    # the parent process keeps standard shell/CI secret injection usable too.
    api_key = values.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    return api_key.strip()



def _gemini_env() -> dict[str, str]:
    """Environment overrides for the Gemini subprocess only.

    The adapter forwards ``GEMINI_API_KEY`` and marks the workspace trusted for
    headless execution. ``GEMINI_API_BASE`` is intentionally not forwarded to
    ``GOOGLE_GEMINI_BASE_URL`` when using Google's standard Gemini endpoint,
    because current Gemini CLI auth selection can reject that path.
    """
    api_key = _load_gemini_dotenv()
    env: dict[str, str] = {
        "GEMINI_CLI_TRUST_WORKSPACE": "true",
        "NO_COLOR": "1",
    }
    if api_key:
        env["GEMINI_API_KEY"] = api_key
    return env


def _build_argv(
    *,
    spec: AgentSpec,
    resume_session: str,
    extra_dirs: tuple[str, ...],
) -> list[str]:
    gemini_bin = getattr(config, "GEMINI_BIN", "gemini")
    approval_mode = str(getattr(config, "GEMINI_APPROVAL_MODE", "yolo"))

    argv = [
        resolve_binary(gemini_bin),
        "--output-format",
        "stream-json",
        "--approval-mode",
        approval_mode,
        "--skip-trust",
    ]
    if spec.model:
        argv += ["--model", spec.model]
    if resume_session:
        argv += ["--resume", resume_session]
    for extra_dir in extra_dirs:
        argv += ["--include-directories", extra_dir]
    return argv


def _schema_instruction(json_schema: dict[str, Any] | None) -> str:
    if json_schema is None:
        return ""
    return (
        "\n\nYour final response MUST be exactly one JSON object matching this "
        "JSON schema. Do not wrap it in a markdown fence and do not add prose "
        "before or after it:\n"
        f"{json.dumps(json_schema, ensure_ascii=False)}"
    )


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort structured-output extraction without changing success state."""
    candidate = text.strip()
    if not candidate:
        return None
    try:
        payload = json.loads(candidate)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass

    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()
            try:
                payload = json.loads(candidate)
                return payload if isinstance(payload, dict) else None
            except json.JSONDecodeError:
                pass

    start = candidate.find("{")
    end = candidate.rfind("}")
    if 0 <= start < end:
        try:
            payload = json.loads(candidate[start : end + 1])
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            pass
    return None


def _int_from_nested(value: Any, names: tuple[str, ...]) -> int:
    if isinstance(value, dict):
        for name in names:
            current = value.get(name)
            if isinstance(current, bool):
                continue
            if isinstance(current, int):
                return current
            if isinstance(current, float):
                return int(current)
        for current in value.values():
            found = _int_from_nested(current, names)
            if found:
                return found
    elif isinstance(value, list):
        for current in value:
            found = _int_from_nested(current, names)
            if found:
                return found
    return 0


def _parse_stream(stdout: str) -> tuple[str, str, str, dict[str, Any], str]:
    """Return ``(text, session_id, status, stats, fatal_error)`` from JSONL.

    Gemini emits assistant text as delta message events, so those chunks are
    concatenated. Warning events remain diagnostic; only error/fatal severity is
    promoted to a failed AgentResult when the CLI/result also indicates failure.
    """
    chunks: list[str] = []
    full_message = ""
    session_id = ""
    result_status = ""
    stats: dict[str, Any] = {}
    fatal_errors: list[str] = []
    parsed_any = False

    for raw_line in stdout.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        parsed_any = True

        event_type = str(payload.get("type") or "").lower()
        if event_type == "init":
            candidate = payload.get("session_id") or payload.get("sessionId")
            if isinstance(candidate, str) and candidate:
                session_id = candidate
        elif event_type == "message" and str(payload.get("role") or "").lower() == "assistant":
            content = payload.get("content")
            if isinstance(content, str) and content:
                if payload.get("delta") is True:
                    chunks.append(content)
                else:
                    full_message = content
        elif event_type in {"tool_use", "tool_call"}:
            # Assistant prose emitted before a tool call is an intermediate turn,
            # not the adapter's final answer. Keep only text produced after the
            # last tool call so structured-output consumers do not receive a
            # concatenation of narration plus the final response.
            chunks.clear()
            full_message = ""
        elif event_type == "error":
            severity = str(payload.get("severity") or "error").lower()
            message = payload.get("message")
            if severity in {"error", "fatal"} and isinstance(message, str) and message:
                fatal_errors.append(message)
        elif event_type == "result":
            result_status = str(payload.get("status") or "").lower()
            candidate_stats = payload.get("stats")
            if isinstance(candidate_stats, dict):
                stats = candidate_stats

    if not parsed_any:
        return stdout.strip(), "", "", {}, ""

    text = "".join(chunks).strip() or full_message.strip()
    return text, session_id, result_status, stats, "\n".join(fatal_errors).strip()


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
    """Run one Gemini CLI turn. Returns a result; never raises.

    ``tools`` is intentionally not translated to ``--allowed-tools``. In the
    current Gemini CLI that flag means "skip confirmation for these tools", not
    "these are the only tools the model may use", so treating it as the shared
    interface's allowlist would create a false security boundary. Headless
    execution instead uses the configured approval mode; use Gemini's Policy
    Engine/settings if a deployment needs a strict Gemini-specific tool policy.
    """
    del tools  # interface parity; see docstring above

    gemini_bin = getattr(config, "GEMINI_BIN", "gemini")
    approval_mode = str(getattr(config, "GEMINI_APPROVAL_MODE", "yolo"))
    if approval_mode not in _VALID_APPROVAL_MODES:
        return AgentResult(
            ok=False,
            error_kind="bad_request",
            error_message=(
                f"Invalid GEMINI_APPROVAL_MODE {approval_mode!r}. Expected one of "
                f"{sorted(_VALID_APPROVAL_MODES)}."
            ),
        )
    if len(extra_dirs) > _MAX_INCLUDE_DIRS:
        return AgentResult(
            ok=False,
            error_kind="bad_request",
            error_message=(
                f"Gemini CLI accepts at most {_MAX_INCLUDE_DIRS} include directories; "
                f"received {len(extra_dirs)}."
            ),
        )

    api_key = _load_gemini_dotenv()
    if not api_key:
        return AgentResult(
            ok=False,
            error_kind="bad_request",
            error_message=(
                "GEMINI_API_KEY was not found in the project .env or parent process "
                "environment. Add GEMINI_API_KEY to .env before using backend='gemini'."
            ),
        )

    full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    full_prompt += _schema_instruction(json_schema)
    argv = _build_argv(
        spec=spec,
        resume_session=resume_session,
        extra_dirs=extra_dirs,
    )

    logger.debug("[%s] argv: %s", tag, argv)
    logger.debug("[%s] system prompt:\n%s", tag, _debug_block(system_prompt))
    logger.debug("[%s] prompt:\n%s", tag, _debug_block(full_prompt))

    started = time.perf_counter()
    try:
        completed = run_with_deadline(
            argv,
            input=full_prompt,
            cwd=cwd,
            timeout=spec.deadline_seconds,
            env_overrides=_gemini_env(),
        )
    except FileNotFoundError:
        return AgentResult(
            ok=False,
            error_kind="not_installed",
            error_message=(
                f"Gemini CLI {gemini_bin!r} was not found from this process's "
                "environment. Set GEMINI_BIN or install @google/gemini-cli."
            ),
            duration_seconds=time.perf_counter() - started,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - started
        _, session_id, _, _, _ = _parse_stream(exc.stdout or "")
        return AgentResult(
            ok=False,
            error_kind="timeout",
            error_message=(
                f"Gemini CLI ran past its {spec.deadline_seconds}s deadline and was "
                "abandoned. Anything it saved before then is still on disk."
            ),
            duration_seconds=elapsed,
            session_id=session_id,
        )

    elapsed = time.perf_counter() - started
    stdout = completed.stdout or ""
    stderr = (completed.stderr or "").strip()
    if stderr:
        logger.debug("[%s] stderr:\n%s", tag, _debug_block(stderr))
    logger.debug("[%s] stdout:\n%s", tag, _debug_block(stdout.strip()))

    text, session_id, result_status, stats, stream_error = _parse_stream(stdout)
    turns = _int_from_nested(stats, ("turns", "num_turns", "turn_count"))

    if completed.returncode != 0 or result_status == "error":
        reason = stream_error or stderr or text or f"gemini exited {completed.returncode}"
        if completed.returncode == 42:
            error_kind = "bad_request"
        else:
            error_kind = classify_failure(reason)
        return AgentResult(
            ok=False,
            error_kind=error_kind,
            error_message=reason,
            duration_seconds=elapsed,
            turns=turns,
            session_id=session_id,
        )

    if stream_error:
        # stream-json may contain diagnostic error events even when the final
        # result reports success. Preserve the diagnostic without converting a
        # successful vendor turn into a false failure.
        logger.warning("[%s] gemini stream diagnostic: %s", tag, stream_error[:300])

    if not text:
        return AgentResult(
            ok=False,
            error_kind=classify_failure(stderr, default="no_output"),
            error_message=(
                f"Gemini CLI exited {completed.returncode} but produced no final "
                f"assistant response. {stderr[-300:] or '(no stderr)'}"
            ),
            duration_seconds=elapsed,
            turns=turns,
            session_id=session_id,
        )

    structured = _parse_json_object(text) if json_schema is not None else None
    logger.info(
        "[%s] gemini done | %.1fs | %d turn(s) | session=%s",
        tag,
        elapsed,
        turns,
        session_id[:8] or "-",
    )
    logger.info("[%s] reply: %s", tag, text[:_CONSOLE_EXCERPT])
    logger.debug("[%s] full reply:\n%s", tag, _debug_block(text))
    return AgentResult(
        ok=True,
        text=text,
        structured=structured,
        duration_seconds=elapsed,
        turns=turns,
        session_id=session_id,
    )


register_backend("direct:gemini", run)