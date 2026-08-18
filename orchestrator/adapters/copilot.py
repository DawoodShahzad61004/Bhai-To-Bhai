"""The GitHub Copilot CLI as a subprocess.

Copilot has the same architectural role as the direct Claude and Codex adapters:
it is a coding-agent CLI with its own prompt transport, permissions, session
state, and output format. Those differences stay at this boundary so workflow
nodes still consume one non-raising `AgentResult`.
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

_CONSOLE_EXCERPT = 200
_DEBUG_BLOCK_LIMIT = 12000


def _debug_block(text: str) -> str:
    if len(text) <= _DEBUG_BLOCK_LIMIT:
        return text
    head = text[:4000]
    tail = text[-4000:]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n... [{omitted} chars omitted] ...\n{tail}"


def _text_from_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                nested = _text_from_value(item.get("text") or item.get("content"))
                if nested:
                    parts.append(nested)
        return "\n".join(part.strip() for part in parts if part and part.strip())
    if isinstance(value, dict):
        for key in ("text", "content", "message", "result", "response"):
            text = _text_from_value(value.get(key))
            if text:
                return text
    return ""


def _find_session_id(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("session_id", "sessionId"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        session = value.get("session")
        if isinstance(session, dict):
            candidate = session.get("id")
            if isinstance(candidate, str) and candidate:
                return candidate
        for nested in value.values():
            candidate = _find_session_id(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for nested in value:
            candidate = _find_session_id(nested)
            if candidate:
                return candidate
    return ""


def _error_message(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, str):
        return error.strip()
    if isinstance(error, dict):
        return _text_from_value(error) or str(error)
    if payload.get("is_error") is True:
        return _text_from_value(payload) or "Copilot reported an error"
    payload_type = str(payload.get("type") or payload.get("event") or "").lower()
    if payload_type == "error":
        return _text_from_value(payload) or "Copilot reported an error"
    return ""


def _parse_json_lines(stdout: str) -> tuple[str, str, str]:
    """Return (text, session_id, error) from Copilot JSONL-ish output.

    The CLI documents JSONL but not a stable event schema in local help. This
    parser therefore reads structured records conservatively, preferring common
    answer fields and preserving a session id when any event exposes one.
    """
    text = ""
    session_id = ""
    error = ""
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

        if not session_id:
            session_id = _find_session_id(payload)

        error = error or _error_message(payload)

        payload_type = str(payload.get("type") or payload.get("event") or "").lower()
        if payload_type == "assistant.message":
            data = payload.get("data")
            if isinstance(data, dict):
                candidate = _text_from_value(data.get("content"))
                if candidate:
                    text = candidate

        for key in ("result", "response", "text", "message", "content"):
            candidate = _text_from_value(payload.get(key))
            if candidate:
                text = candidate

    if not parsed_any:
        return stdout.strip(), "", ""
    return text.strip(), session_id, error.strip()


def _build_argv(
    *,
    spec: AgentSpec,
    prompt: str,
    cwd: str,
    resume_session: str,
    extra_dirs: tuple[str, ...],
) -> list[str]:
    argv = [
        resolve_binary(config.COPILOT_BIN),
        "-C",
        cwd,
        "--output-format",
        "json",
        "--silent",
        "--no-color",
        "--no-ask-user",
        "--allow-all-tools",
    ]
    if spec.model:
        argv += ["--model", spec.model]
    for extra_dir in extra_dirs:
        argv += ["--add-dir", extra_dir]
    if resume_session:
        argv += [f"--resume={resume_session}"]
    argv += ["-p", prompt]
    return argv


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
    """One GitHub Copilot CLI turn. Returns a result; never raises."""
    full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    if json_schema is not None:
        full_prompt = (
            f"{full_prompt}\n\nReply with a single JSON object matching this schema:\n"
            f"{json.dumps(json_schema)}"
        )

    argv = _build_argv(
        spec=spec,
        prompt=full_prompt,
        cwd=cwd,
        resume_session=resume_session,
        extra_dirs=extra_dirs,
    )
    logger.debug("[%s] argv: %s", tag, argv[:-1] + ["<prompt>"])
    logger.debug("[%s] prompt:\n%s", tag, _debug_block(full_prompt))
    if tools:
        logger.debug("[%s] requested tools are handled by Copilot permissions: %s", tag, tools)

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
                f"GitHub Copilot CLI {config.COPILOT_BIN!r} was not found from this process's "
                "environment. Set COPILOT_BIN or install it."
            ),
            duration_seconds=time.perf_counter() - started,
        )
    except subprocess.TimeoutExpired as exc:
        text, session_id, _ = _parse_json_lines(exc.stdout or "")
        return AgentResult(
            ok=False,
            error_kind="timeout",
            error_message=(
                f"GitHub Copilot CLI ran past its {spec.deadline_seconds}s deadline and was "
                "abandoned. Anything it saved before then is still on disk."
            ),
            text=text,
            duration_seconds=time.perf_counter() - started,
            session_id=session_id,
        )

    elapsed = time.perf_counter() - started
    stdout = completed.stdout or ""
    stderr = (completed.stderr or "").strip()
    if stderr:
        logger.debug("[%s] stderr:\n%s", tag, _debug_block(stderr))
    logger.debug("[%s] stdout:\n%s", tag, _debug_block(stdout.strip()))

    text, session_id, error = _parse_json_lines(stdout)
    if completed.returncode != 0:
        reason = error or text or stderr[-300:] or f"copilot exited {completed.returncode}"
        return AgentResult(
            ok=False,
            error_kind=classify_failure(reason),
            error_message=reason,
            duration_seconds=elapsed,
            session_id=session_id,
        )
    if error:
        return AgentResult(
            ok=False,
            error_kind=classify_failure(error),
            error_message=error,
            duration_seconds=elapsed,
            session_id=session_id,
        )
    if not text:
        if stderr:
            return AgentResult(
                ok=False,
                error_kind=classify_failure(stderr),
                error_message=stderr,
                duration_seconds=elapsed,
                session_id=session_id,
            )
        return AgentResult(
            ok=False,
            error_kind="no_output",
            error_message=(
                "GitHub Copilot CLI exited 0 but produced no parseable final reply. "
                f"{stderr[-300:] or '(no stderr)'}"
            ),
            duration_seconds=elapsed,
            session_id=session_id,
        )

    logger.info(
        "[%s] copilot done | %.1fs | session=%s",
        tag,
        elapsed,
        session_id[:8] or "-",
    )
    logger.info("[%s] reply: %s", tag, text[:_CONSOLE_EXCERPT])
    logger.debug("[%s] full reply:\n%s", tag, _debug_block(text))
    return AgentResult(ok=True, text=text, duration_seconds=elapsed, session_id=session_id)


register_backend("direct:copilot", run)
