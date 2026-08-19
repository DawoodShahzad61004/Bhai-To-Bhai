"""An OpenAI-compatible local server used through Codex's agent harness.

This is deliberately separate from ``adapters.ollama``. Ollama uses Codex's
built-in ``--local-provider ollama`` integration; this adapter configures a
custom provider from ``CUSTOM_API_*``. Codex still owns the file and shell tools,
workspace sandbox, deadline, final-answer channel, and resumable session.

Codex custom providers require the Responses API. When the configured server
only documents Chat Completions, a loopback compatibility bridge translates
that wire protocol while Codex continues to own the coding-agent harness.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener

from adapters.base import AgentResult, register_backend
from adapters.codex import run_codex
from adapters.local_llm_bridge import responses_bridge
import config
from config import AgentSpec

_PROVIDER_ID = "local_llm"
_API_KEY_ENV = "CUSTOM_API_KEY"


def _advertised_wire_api(base_url: str, timeout: float) -> str | None:
    """Return ``responses`` or ``chat_completions`` from the server's OpenAPI.

    OpenAPI is only an optimization: absent, private, or malformed documents do
    not block dispatch. In that case Codex remains the authoritative probe of
    the configured Responses route.
    """
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    openapi_url = urlunsplit((parsed.scheme, parsed.netloc, "/openapi.json", "", ""))
    request = Request(openapi_url, headers={"Accept": "application/json"})
    try:
        # The local endpoint must be reached directly even when the parent shell
        # carries a corporate or sandbox proxy. Codex receives the equivalent
        # NO_PROXY exclusion from subprocess_env().
        opener = build_opener(ProxyHandler({}))
        with opener.open(request, timeout=max(0.1, min(timeout, 5.0))) as response:
            payload = json.load(response)
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError):
        return None

    paths = payload.get("paths") if isinstance(payload, dict) else None
    if not isinstance(paths, dict):
        return None

    responses_path = f"{parsed.path.rstrip('/')}/responses" or "/responses"
    if responses_path in paths:
        return "responses"
    chat_path = f"{parsed.path.rstrip('/')}/chat/completions" or "/chat/completions"
    if chat_path in paths:
        return "chat_completions"
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
    missing = [
        name
        for name, value in (
            ("CUSTOM_API_BASE", config.CUSTOM_API_BASE),
            ("CUSTOM_API_KEY", config.CUSTOM_API_KEY),
            ("CUSTOM_API_MODEL_NAME", spec.model or config.CUSTOM_API_MODEL_NAME),
        )
        if not value
    ]
    if missing:
        return AgentResult(
            ok=False,
            error_kind="bad_request",
            error_message=f"The Local LLM adapter requires: {', '.join(missing)}.",
        )

    base_url = config.CUSTOM_API_BASE.rstrip("/")
    effective_spec = replace(spec, model=spec.model or config.CUSTOM_API_MODEL_NAME)

    def dispatch(provider_base_url: str) -> AgentResult:
        return run_codex(
            prompt,
            spec=effective_spec,
            system_prompt=system_prompt,
            cwd=cwd,
            tag=tag,
            tools=tools,
            json_schema=json_schema,
            resume_session=resume_session,
            extra_dirs=extra_dirs,
            custom_provider_id=_PROVIDER_ID,
            custom_provider_base_url=provider_base_url,
            custom_provider_env_key=_API_KEY_ENV,
            backend_label="Local LLM",
        )

    if _advertised_wire_api(base_url, spec.deadline_seconds) == "chat_completions":
        with responses_bridge(
            base_url,
            config.CUSTOM_API_KEY,
            spec.deadline_seconds,
            tools,
        ) as bridge_base_url:
            return dispatch(bridge_base_url)
    return dispatch(base_url)


register_backend("direct:local_llm", run)
