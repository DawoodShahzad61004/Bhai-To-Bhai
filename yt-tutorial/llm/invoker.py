from __future__ import annotations

import logging

import concurrent.futures
import queue as _queue
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

import groq as _groq
import httpx as _httpx
import openai as _openai
import requests as _requests

from config import (
    LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS,
    LLM_RATE_LIMIT_BACKOFF_MAX_SECONDS,
    LLM_RATE_LIMIT_MAX_ATTEMPTS,
    LLM_RATE_LIMIT_MAX_DELAY_SECONDS,
    LLM_RESPONSE_TIMEOUT_SECONDS,
    MIN_COOLDOWN_TIME,
    MAX_COOLDOWN_TIME,
)

logger = logging.getLogger(__name__)

# ── FIFO call gate + adaptive cooldown ───────────────────────────────────────
#
# FIFO gate
# ─────────
# A queue.Queue of per-thread Events gives strict arrival-order fairness.
# A thread that gets a 429 does NOT re-enqueue — it holds the gate, sleeps
# the full reset window, then retries directly.  _gate_release_to_next() is
# only called after a final success or a terminal failure.
#
# Global token-reset deadline
# ───────────────────────────
# _token_reset_until (monotonic) is written by any thread that gets a 429.
# The front-of-queue thread reads it and sleeps the remaining delta before
# calling Groq, so the reset wait is paid exactly once per exhaustion event.
#
# Adaptive inter-call cooldown
# ────────────────────────────
# After every successful call, if the queue is non-empty, the active thread
# sleeps a "cooldown floor" before releasing the gate to the next waiter.
# This prevents back-to-back calls from draining the token window.
#
# Cooldown base = (1 - remaining_tokens / limit_tokens) * reset_window_seconds
#   — derived from Groq's x-ratelimit-* response headers on each success.
#   — acts as a floor: cooldown never drops below what the token state demands.
#
# Cooldown adjustment rules (applied after each successful release):
#   • Thread recovered from a 429 before succeeding, queue non-empty  → double
#   • Last 3 consecutive clean-first-attempt successes, queue non-empty → halve
#   • Queue empties → reset to 0.0
#
# "Clean success" = succeeded on the very first attempt (no 429 at all).
# A thread that eventually succeeded after a 429 resets the clean-success
# counter to 0 (it triggered the double, not the halve path).

# ── gate state ────────────────────────────────────────────────────────────────
_llm_queue:      _queue.Queue[threading.Event] = _queue.Queue()
_llm_gate_lock:  threading.Lock                = threading.Lock()
_llm_active:     bool                          = False

# ── token-reset deadline ──────────────────────────────────────────────────────
_token_reset_until: float          = 0.0   # monotonic epoch
_token_reset_lock:  threading.Lock = threading.Lock()

# ── adaptive cooldown state ───────────────────────────────────────────────────
_cooldown_floor:       float          = 0.0   # current floor in seconds
_clean_success_streak: int            = 0     # consecutive clean-first-attempt successes
_cooldown_lock:        threading.Lock = threading.Lock()

_CLEAN_STREAK_FOR_HALVE: int   = 3
_COOLDOWN_MIN:           float = MIN_COOLDOWN_TIME
_COOLDOWN_MAX:           float = MAX_COOLDOWN_TIME
_COOLDOWN_FALLBACK:      float = 1.0   # minimum cooldown after a 429 when floor and token_base are both zero

# ── HTTP header capture (for rate-limit headers) ──────────────────────────────
# LangChain's ChatGroq wrapper does not propagate raw HTTP response headers into
# AIMessage.response_metadata, so we install an httpx event hook on the
# underlying Groq client's HTTP session to capture them directly.
# Protected by the FIFO gate — only one LLM call runs at a time.
_captured_response_headers: _httpx.Headers | None = None


def _on_groq_response(response: _httpx.Response) -> None:
    global _captured_response_headers
    _captured_response_headers = response.headers


def _install_header_hook(llm) -> None:
    """Idempotently attach _on_groq_response to the underlying httpx client."""
    try:
        groq_client = getattr(llm, "client", None)
        httpx_client = getattr(groq_client, "_client", None)
        if not isinstance(httpx_client, _httpx.Client):
            return
        hooks = httpx_client.event_hooks.setdefault("response", [])
        if _on_groq_response not in hooks:
            hooks.append(_on_groq_response)
    except Exception:
        pass


# ── gate primitives ───────────────────────────────────────────────────────────

def _gate_acquire(caller_tag: str) -> None:
    """
    Acquire the FIFO gate.

    If no thread is active, take the gate immediately.
    Otherwise enqueue a per-thread Event and block until signalled.
    New arrivals always go to the back of the queue — retrying threads
    (on a 429) never call this; they hold the gate and loop directly.
    """
    global _llm_active
    with _llm_gate_lock:
        if not _llm_active:
            _llm_active = True
            return
        my_event = threading.Event()
        _llm_queue.put(my_event)

    logger.debug(f"  [{caller_tag}] queued — waiting for FIFO gate…")
    my_event.wait()
    logger.debug(f"  [{caller_tag}] FIFO gate acquired.")


def _gate_release_to_next() -> None:
    """
    Hand the gate to the oldest waiting thread (FIFO), or mark it free.
    Called only after the cooldown sleep so the next thread starts with a
    clean token-window state.
    """
    global _llm_active
    with _llm_gate_lock:
        try:
            next_event: threading.Event = _llm_queue.get_nowait()
            next_event.set()            # _llm_active stays True — handed over
        except _queue.Empty:
            _llm_active = False         # nobody waiting; gate is free


# ── token-reset helpers ───────────────────────────────────────────────────────

def _update_token_reset(reset_seconds: float) -> None:
    """Push the global token-reset deadline forward (never backward)."""
    global _token_reset_until
    deadline = time.monotonic() + reset_seconds
    with _token_reset_lock:
        if deadline > _token_reset_until:
            _token_reset_until = deadline


def _wait_for_token_window(caller_tag: str) -> None:
    """
    Sleep until the global token-reset deadline has passed, if it hasn't.
    Called by the thread at the front of the gate immediately before calling
    Groq — whether that is a fresh attempt or a retry after 429.
    """
    with _token_reset_lock:
        deadline = _token_reset_until
    remaining = deadline - time.monotonic()
    if remaining > 0:
        logger.warning(
            f"  [{caller_tag}] token window not yet refilled — "
            f"sleeping {remaining:.2f}s before calling Groq…"
        )
        time.sleep(remaining)


class LLMRateLimitAbortError(Exception):
    """Raised when the required backoff delay exceeds LLM_RATE_LIMIT_MAX_DELAY_SECONDS."""
    def __init__(self, delay: float) -> None:
        self.delay = delay
        super().__init__(f"Rate-limit backoff of {delay:.0f}s exceeds maximum allowed delay.")


class LLMResponseTimeoutError(Exception):
    """Raised when the LLM does not return a response within LLM_RESPONSE_TIMEOUT_SECONDS."""
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(f"LLM did not respond within {timeout:.0f}s.")


# ── Error taxonomy ────────────────────────────────────────────────────────────

class LLMErrorKind(Enum):
    """Structured error categories returned in LLMResult."""
    TOOL_USE_FAILED    = auto()   # 400, code="tool_use_failed" — partial gen available
    BAD_REQUEST        = auto()   # 400, other cause
    RATE_LIMIT         = auto()   # 429
    AUTH               = auto()   # 401
    PERMISSION         = auto()   # 403
    NOT_FOUND          = auto()   # 404
    UNPROCESSABLE      = auto()   # 422
    SERVER_ERROR       = auto()   # 5xx
    CONNECTION         = auto()   # network failure, no HTTP status
    TIMEOUT            = auto()   # request timed out
    UNKNOWN            = auto()   # anything else


# ── Result container ──────────────────────────────────────────────────────────

@dataclass
class LLMResult:
    ok:             bool

    # ── Success fields ────────────────────────────────────────────────────────
    response:       Any    = None   # LangChain AIMessage
    content:        str    = ""

    # ── Failure fields ────────────────────────────────────────────────────────
    error_kind:     LLMErrorKind | None = None
    status_code:    int | None          = None
    error_message:  str                 = ""
    recovered_text: str                 = ""
    raw_error:      BaseException | None = None


# ── Internal helpers ──────────────────────────────────────────────────────────

_FUNCTION_SUFFIX_RE = re.compile(r"\s*<function=\w+>\{.*", re.DOTALL)

def _strip_function_suffix(text: str) -> str:
    return _FUNCTION_SUFFIX_RE.sub("", text).strip()


def _message_text(msg: Any) -> str:
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", block)))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content or "")


def _messages_char_len(messages: list) -> int:
    return sum(len(_message_text(m)) for m in messages)


def _handle_bad_request(exc: _groq.BadRequestError) -> LLMResult:
    try:
        err_body = exc.response.json()
    except Exception:
        err_body = {}

    error_detail = err_body.get("error", {})
    error_code   = error_detail.get("code", "")
    failed_gen   = error_detail.get("failed_generation", "")

    logger.warning(f"[LLM] BadRequestError — HTTP {exc.status_code} code={error_code!r}")

    if error_code == "tool_use_failed":
        recovered = _strip_function_suffix(failed_gen) if failed_gen else ""
        logger.warning(f"[LLM] tool_use_failed — recovered text length: {len(recovered)} chars")
        if recovered:
            logger.debug(f"  [LLM] recovered snippet: {recovered[:200]}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.TOOL_USE_FAILED,
            status_code=exc.status_code,
            error_message=f"tool_use_failed: {error_detail.get('message', '')}",
            recovered_text=recovered,
            raw_error=exc,
        )

    # Generic 400
    msg = error_detail.get("message", str(exc))
    logger.error(f"[LLM] bad_request detail: {msg[:300]}")
    return LLMResult(
        ok=False,
        error_kind=LLMErrorKind.BAD_REQUEST,
        status_code=exc.status_code,
        error_message=msg,
        raw_error=exc,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def _invoke_once(
    llm,
    messages: list,
    *,
    tools: list | None = None,
    caller_tag: str = "LLM",
    config=None,
) -> LLMResult:
    global _captured_response_headers
    _install_header_hook(llm)
    _captured_response_headers = None

    kwargs: dict[str, Any] = {}
    if tools is not None:
        kwargs["tools"] = tools

    input_chars   = _messages_char_len(messages)
    expected_out  = getattr(llm, "max_tokens", None)
    logger.debug(
        f"  [{caller_tag}] context size — input≈{input_chars} chars "
        f"(~{input_chars // 4} tokens est.), {len(messages)} messages, "
        f"expected output cap={expected_out if expected_out is not None else 'n/a'} tokens"
    )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _executor:
            # config is passed explicitly (not relied on via ambient contextvar)
            # because it crosses a thread boundary here, which does not inherit
            # the LangGraph-set RunnableConfig context.
            _future = _executor.submit(llm.invoke, messages, config=config, **kwargs)
            try:
                response = _future.result(timeout=LLM_RESPONSE_TIMEOUT_SECONDS)
            except concurrent.futures.TimeoutError:
                raise LLMResponseTimeoutError(LLM_RESPONSE_TIMEOUT_SECONDS)
        content  = (getattr(response, "content", "") or "").strip()
        usage    = (getattr(response, "response_metadata", {}) or {}).get("token_usage", {})
        logger.debug(
            f"  [{caller_tag}] token usage — input={usage.get('prompt_tokens', 'n/a')} "
            f"output={usage.get('completion_tokens', 'n/a')} "
            f"total={usage.get('total_tokens', 'n/a')}"
        )
        return LLMResult(ok=True, response=response, content=content)

    # ── Groq-specific errors ──────────────────────────────────────────────────

    except LLMResponseTimeoutError as e:
        logger.error(
            f"[{caller_tag}] LLM did not respond within {e.timeout:.0f}s "
            f"(LLM_RESPONSE_TIMEOUT_SECONDS={LLM_RESPONSE_TIMEOUT_SECONDS}) — skipping this call."
        )
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.TIMEOUT,
            status_code=None,
            error_message=str(e),
            raw_error=e,
        )

    except _groq.BadRequestError as e:
        return _handle_bad_request(e)

    except _groq.RateLimitError as e:
        logger.warning(f"[{caller_tag}] RateLimitError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.RATE_LIMIT,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    except _groq.AuthenticationError as e:
        logger.error(f"[{caller_tag}] AuthenticationError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.AUTH,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    except _groq.PermissionDeniedError as e:
        logger.error(f"[{caller_tag}] PermissionDeniedError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.PERMISSION,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    except _groq.NotFoundError as e:
        logger.error(f"[{caller_tag}] NotFoundError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.NOT_FOUND,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    except _groq.UnprocessableEntityError as e:
        logger.error(f"[{caller_tag}] UnprocessableEntityError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.UNPROCESSABLE,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    except _groq.InternalServerError as e:
        logger.error(f"[{caller_tag}] InternalServerError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.SERVER_ERROR,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    except _groq.APIConnectionError as e:
        logger.error(f"[{caller_tag}] APIConnectionError (no HTTP status): {e}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.CONNECTION,
            status_code=None,
            error_message=str(e),
            raw_error=e,
        )

    except _groq.APITimeoutError as e:
        logger.error(f"[{caller_tag}] APITimeoutError: {e}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.TIMEOUT,
            status_code=None,
            error_message=str(e),
            raw_error=e,
        )

    # Catch-all for any other APIStatusError subclass not listed above
    except _groq.APIStatusError as e:
        logger.error(f"[{caller_tag}] APIStatusError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.UNKNOWN,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    # ── OpenAI-compatible errors (ChatOpenAI / HuggingFace router) ────────────

    except _openai.BadRequestError as e:
        logger.warning(f"[{caller_tag}] openai.BadRequestError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.BAD_REQUEST,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    except _openai.RateLimitError as e:
        logger.warning(f"[{caller_tag}] openai.RateLimitError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.RATE_LIMIT,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    except _openai.AuthenticationError as e:
        logger.error(f"[{caller_tag}] openai.AuthenticationError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.AUTH,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    except _openai.PermissionDeniedError as e:
        logger.error(f"[{caller_tag}] openai.PermissionDeniedError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.PERMISSION,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    except _openai.NotFoundError as e:
        logger.error(f"[{caller_tag}] openai.NotFoundError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.NOT_FOUND,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    except _openai.UnprocessableEntityError as e:
        logger.error(f"[{caller_tag}] openai.UnprocessableEntityError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.UNPROCESSABLE,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    except _openai.InternalServerError as e:
        logger.error(f"[{caller_tag}] openai.InternalServerError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.SERVER_ERROR,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    except _openai.APITimeoutError as e:
        logger.error(f"[{caller_tag}] openai.APITimeoutError: {e}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.TIMEOUT,
            status_code=None,
            error_message=str(e),
            raw_error=e,
        )

    except _openai.APIConnectionError as e:
        logger.error(f"[{caller_tag}] openai.APIConnectionError (network/DNS failure): {e}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.CONNECTION,
            status_code=None,
            error_message=str(e),
            raw_error=e,
        )

    except _openai.APIStatusError as e:
        logger.error(f"[{caller_tag}] openai.APIStatusError — HTTP {e.status_code}: {e.message}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.UNKNOWN,
            status_code=e.status_code,
            error_message=e.message,
            raw_error=e,
        )

    except _httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 429:
            logger.warning(f"[{caller_tag}] httpx.HTTPStatusError 429 (rate limit): {e}")
            return LLMResult(
                ok=False,
                error_kind=LLMErrorKind.RATE_LIMIT,
                status_code=status,
                error_message=str(e),
                raw_error=e,
            )
        elif 500 <= status < 600:
            logger.error(f"[{caller_tag}] httpx.HTTPStatusError {status} (server error): {e}")
            return LLMResult(
                ok=False,
                error_kind=LLMErrorKind.SERVER_ERROR,
                status_code=status,
                error_message=str(e),
                raw_error=e,
            )
        else:
            logger.error(f"[{caller_tag}] httpx.HTTPStatusError {status}: {e}")
            return LLMResult(
                ok=False,
                error_kind=LLMErrorKind.UNKNOWN,
                status_code=status,
                error_message=str(e),
                raw_error=e,
            )

    except _httpx.ConnectError as e:
        logger.error(f"[{caller_tag}] httpx.ConnectError (network/DNS failure): {e}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.CONNECTION,
            status_code=None,
            error_message=str(e),
            raw_error=e,
        )

    except _requests.exceptions.Timeout as e:
        logger.error(f"[{caller_tag}] requests.Timeout: {e}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.TIMEOUT,
            status_code=None,
            error_message=str(e),
            raw_error=e,
        )

    except _requests.exceptions.ConnectionError as e:
        logger.error(f"[{caller_tag}] requests.ConnectionError (network/DNS failure): {e}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.CONNECTION,
            status_code=None,
            error_message=str(e),
            raw_error=e,
        )

    except _requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 429:
            logger.warning(f"[{caller_tag}] requests.HTTPError 429 (rate limit): {e}")
            kind = LLMErrorKind.RATE_LIMIT
        elif status is not None and 500 <= status < 600:
            logger.error(f"[{caller_tag}] requests.HTTPError {status} (server error): {e}")
            kind = LLMErrorKind.SERVER_ERROR
        else:
            logger.error(f"[{caller_tag}] requests.HTTPError {status}: {e}")
            kind = LLMErrorKind.UNKNOWN
        return LLMResult(
            ok=False,
            error_kind=kind,
            status_code=status,
            error_message=str(e),
            raw_error=e,
        )

    except Exception as e:
        logger.exception(f"[{caller_tag}] Unexpected error: {type(e).__name__}: {e}")
        return LLMResult(
            ok=False,
            error_kind=LLMErrorKind.UNKNOWN,
            status_code=None,
            error_message=str(e),
            raw_error=e,
        )


# ── cooldown helpers ──────────────────────────────────────────────────────────

def _parse_groq_duration(value: str) -> float:
    """
    Parse Groq's reset duration strings into seconds.

    Groq uses Go-style duration strings in x-ratelimit-reset-* headers, e.g.:
      "7.66s"    -> 7.66
      "1m30s"    -> 90.0
      "2m59.56s" -> 179.56
      "500ms"    -> 0.5
    Falls back to treating the value as a plain float (the retry-after header
    is just a number of seconds).
    """
    import re as _re
    value = value.strip()
    total = 0.0
    for amount, unit in _re.findall(r"([\d.]+)(ms|s|m|h)", value):
        n = float(amount)
        if unit == "ms":
            total += n / 1000
        elif unit == "s":
            total += n
        elif unit == "m":
            total += n * 60
        elif unit == "h":
            total += n * 3600
    if total > 0:
        return total
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _groq_wait_seconds(result: LLMResult) -> float:
    """
    Return the number of seconds to wait before retrying, sourced from Groq's
    response headers.  Priority order:
      1. x-ratelimit-reset-tokens   (most accurate: tells us when the TPM
                                     window refills — what we actually exhausted)
      2. x-ratelimit-reset-requests (RPM window refill)
      3. retry-after                (Groq sets this on 429 but can lag behind
                                     the reset headers)
    Returns 0.0 if no usable header is present.
    """
    response = getattr(result.raw_error, "response", None)
    headers  = getattr(response, "headers", None)
    if not headers:
        return 0.0

    for header in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests", "retry-after"):
        value = headers.get(header)
        if value:
            parsed = _parse_groq_duration(value)
            if parsed > 0:
                logger.debug(
                    f"[LLM] wait hint from {header!r}: {parsed:.2f}s (raw={value!r})"
                )
                return parsed
    return 0.0


def _parse_token_headers(result: LLMResult) -> tuple[float, float, float]:
    """
    Extract (remaining_tokens, limit_tokens, reset_window_seconds) from the
    Groq response headers of a *successful* call.

    Returns (0, 0, 0) if any header is missing or unparseable, which causes
    the cooldown base to be 0 (no enforced floor from this call).
    """
    # Primary: headers captured via httpx hook in _invoke_once (most reliable,
    # because LangChain's ChatGroq does not propagate raw HTTP headers into
    # AIMessage.response_metadata or _raw_response).
    headers = _captured_response_headers
    if headers is None:
        response = getattr(result.response, "response_metadata", None)
        if isinstance(response, dict):
            headers = response.get("headers")
    if headers is None:
        raw = getattr(result.response, "_raw_response", None)
        headers = getattr(raw, "headers", None)
    if not headers:
        logger.debug("[LLM] could not extract rate-limit headers — cooldown base will be 0")
        return 0.0, 0.0, 0.0

    try:
        remaining = float(headers.get("x-ratelimit-remaining-tokens", 0))
        limit     = float(headers.get("x-ratelimit-limit-tokens",     0))
        reset_raw = headers.get("x-ratelimit-reset-tokens", "")
        reset_sec = _parse_groq_duration(reset_raw) if reset_raw else 0.0
        if limit <= 0 or reset_sec <= 0:
            return 0.0, 0.0, 0.0
        return remaining, limit, reset_sec
    except (TypeError, ValueError):
        return 0.0, 0.0, 0.0


def _compute_cooldown_base(result: LLMResult) -> float:
    """
    Compute the token-state-derived cooldown floor for this call.

    cooldown_base = (1 - remaining / limit) * reset_window_seconds

    A nearly-full window → near-zero base (no throttling needed).
    A nearly-empty window → base approaches reset_window_seconds (be cautious).
    """
    remaining, limit, reset_sec = _parse_token_headers(result)
    if limit <= 0:
        return 0.0
    fraction_used = 1.0 - (remaining / limit)
    return max(0.0, fraction_used * reset_sec)


def _apply_cooldown(
    result:     LLMResult,
    had_429:    bool,
    caller_tag: str,
) -> None:
    """
    After a successful Groq call, sleep the cooldown floor (if queue is
    non-empty) then update the global cooldown state for future calls.

    Rules (evaluated under _cooldown_lock):
      • had_429=True  → double the floor (just recovered; be cautious)
      • clean_streak >= _CLEAN_STREAK_FOR_HALVE → halve the floor (ease off)
      • floor is always clamped to [_COOLDOWN_MIN, _COOLDOWN_MAX]
      • floor is always at least the token-state base from this call's headers
      • queue empty → reset floor and streak to zero
    """
    global _cooldown_floor, _clean_success_streak

    token_base     = _compute_cooldown_base(result)
    queue_empty    = _llm_queue.empty()
    floor_to_sleep = 0.0

    with _cooldown_lock:
        if queue_empty:
            _cooldown_floor       = 0.0
            _clean_success_streak = 0
            return

        if had_429:
            _clean_success_streak = 0
        else:
            _clean_success_streak += 1

        current = _cooldown_floor

        if had_429:
            if current > 0:
                current = current * 2.0
            elif token_base > 0:
                current = token_base * 2.0
            else:
                current = _COOLDOWN_FALLBACK
            logger.debug(
                f"  [{caller_tag}] cooldown doubled after 429-recovery → {current:.2f}s"
            )
        elif _clean_success_streak >= _CLEAN_STREAK_FOR_HALVE:
            current = current / 2.0
            _clean_success_streak = 0
            logger.debug(
                f"  [{caller_tag}] cooldown halved after {_CLEAN_STREAK_FOR_HALVE} "
                f"clean successes → {current:.2f}s"
            )

        current = max(current, token_base)
        current = max(_COOLDOWN_MIN, min(_COOLDOWN_MAX, current))
        _cooldown_floor = current
        floor_to_sleep  = current

    if floor_to_sleep > 0:
        logger.info(
            f"  [{caller_tag}] inter-call cooldown {floor_to_sleep:.2f}s "
            f"(token_base={token_base:.2f}s, had_429={had_429}, "
            f"streak={_clean_success_streak})…"
        )
        time.sleep(floor_to_sleep)


def _rate_limit_delay(
    result: LLMResult,
    attempt: int,
    *,
    base_seconds: float,
    max_seconds: float,
) -> float:
    """
    Compute how long to wait before the next retry attempt and publish the
    reset deadline globally so _wait_for_token_window() can use it.

    Jitter is intentionally absent: with the FIFO gate only one thread calls
    Groq at a time, so there is no collision to de-synchronize.
    """
    exponential = min(max_seconds, base_seconds * (2 ** (attempt - 1)))
    header_hint = _groq_wait_seconds(result)
    delay       = max(exponential, header_hint)
    _update_token_reset(delay)
    return delay


def llm_invoke(
    llm,
    messages: list,
    *,
    tools: list | None = None,
    caller_tag: str = "LLM",
    rate_limit_max_attempts: int = LLM_RATE_LIMIT_MAX_ATTEMPTS,
    rate_limit_backoff_base_seconds: float = LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS,
    rate_limit_backoff_max_seconds: float = LLM_RATE_LIMIT_BACKOFF_MAX_SECONDS,
    rate_limit_max_delay_seconds: float = LLM_RATE_LIMIT_MAX_DELAY_SECONDS,
    config=None,
) -> LLMResult:
    """
    Invoke an LLM with FIFO serialization, token-window awareness, and an
    adaptive inter-call cooldown.

    Gate / ordering
    ───────────────
    All threads share a FIFO gate.  A thread that gets a 429 does NOT
    re-enqueue — it holds the gate, sleeps the full reset window, then retries
    directly at the front.  _gate_release_to_next() is called only after a
    final success or terminal failure, never between retry attempts.

    Retry flow (per attempt)
    ────────────────────────
      1. On the first attempt: acquire the gate (may queue behind others).
         On a retry after 429: stay at the front — skip _gate_acquire.
      2. Sleep the remaining token-reset delta (if any) via
         _wait_for_token_window().  On retries this is the bulk of the wait;
         on fresh calls it is usually zero.
      3. Call Groq.
      4a. If 429: update global reset deadline, check abort threshold, loop
          back to step 2 — holding the gate throughout.
      4b. If success or non-retryable error: apply adaptive cooldown (sleep
          the floor if queue is non-empty, update floor state), then release
          the gate to the next waiter.
    """
    max_attempts = max(1, rate_limit_max_attempts)
    had_429      = False

    # ── Step 1 (first attempt only): enter the FIFO queue ────────────────────
    logger.debug(f"  [{caller_tag}] entering FIFO gate…")
    _gate_acquire(caller_tag)

    for attempt in range(1, max_attempts + 1):
        # ── Step 2: wait for the global token window ──────────────────────────
        _wait_for_token_window(caller_tag)

        # ── Step 3: call Groq ─────────────────────────────────────────────────
        t0 = time.perf_counter()
        logger.debug(f"  [{caller_tag}] invoking LLM (attempt {attempt}/{max_attempts})…")
        result   = _invoke_once(llm, messages, tools=tools, caller_tag=caller_tag, config=config)
        duration = time.perf_counter() - t0
        logger.debug(f"  [{caller_tag}] LLM call attempt {attempt} took {duration:.3f}s")

        # ── Step 4a: rate-limited → stay at front, update deadline, retry ─────
        if result.error_kind == LLMErrorKind.RATE_LIMIT and attempt < max_attempts:
            had_429 = True
            delay = _rate_limit_delay(
                result,
                attempt,
                base_seconds=max(0.0, rate_limit_backoff_base_seconds),
                max_seconds =max(0.0, rate_limit_backoff_max_seconds),
            )
            if delay >= rate_limit_max_delay_seconds:
                logger.error(
                    f"  [{caller_tag}] reset window {delay:.0f}s >= max allowed "
                    f"{rate_limit_max_delay_seconds:.0f}s — aborting."
                )
                # Release before raising so the next waiter isn't blocked forever
                _gate_release_to_next()
                raise LLMRateLimitAbortError(delay)

            logger.warning(
                f"  [{caller_tag}] 429 received; holding gate, "
                f"token window resets in {delay:.2f}s — retrying at front "
                f"(attempt {attempt + 1}/{max_attempts})…"
            )
            # Loop back to step 2 — _wait_for_token_window will sleep the delta
            continue

        # ── Step 4b: terminal result (success or non-retryable error) ─────────
        _apply_cooldown(result, had_429=had_429, caller_tag=caller_tag)
        _gate_release_to_next()
        return result

    # Exhausted all attempts while rate-limited — release and return last result
    _gate_release_to_next()
    return result  # type: ignore[return-value]
