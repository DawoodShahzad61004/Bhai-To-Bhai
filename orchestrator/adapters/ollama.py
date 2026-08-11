"""Local Ollama models exposed through a coding-agent CLI harness.

Ollama's bare ``run`` command is a chat interface, not a coding agent: it cannot
inspect or edit a worktree. The harness supplies the file, shell, sandbox,
deadline, and session machinery; ``config.OLLAMA_HARNESS`` chooses which one.
"""

from __future__ import annotations

from typing import Any

from adapters.base import AgentResult, register_backend
from adapters.claude import run as run_claude
from adapters.codex import run_codex
import config
from config import AgentSpec


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
    if not spec.model:
        return AgentResult(
            ok=False,
            error_kind="bad_request",
            error_message="The Ollama adapter requires an explicit model name.",
        )

    if config.OLLAMA_HARNESS == "codex":
        return run_codex(
            prompt,
            spec=spec,
            system_prompt=system_prompt,
            cwd=cwd,
            tag=tag,
            tools=tools,
            json_schema=json_schema,
            resume_session=resume_session,
            extra_dirs=extra_dirs,
            local_provider="ollama",
            backend_label="Ollama",
        )

    if config.OLLAMA_HARNESS == "claude":
        return run_claude(
            prompt,
            spec=spec,
            system_prompt=system_prompt,
            cwd=cwd,
            tag=tag,
            tools=tools,
            json_schema=json_schema,
            resume_session=resume_session,
            extra_dirs=extra_dirs,
        )

    return AgentResult(
        ok=False,
        error_kind="bad_request",
        error_message=(
            "OLLAMA_HARNESS must be 'codex' or 'claude'; "
            f"got {config.OLLAMA_HARNESS!r}."
        ),
    )


register_backend("direct:ollama", run)
