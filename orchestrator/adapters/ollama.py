"""Local Ollama models exposed through Codex's coding-agent loop.

Ollama's bare ``run`` command is a chat interface, not a coding agent: it cannot
inspect or edit a worktree. Codex's local-provider mode supplies the same file,
shell, sandbox, deadline, and session machinery as the direct Codex adapter
while sending inference to the local Ollama server.
"""

from __future__ import annotations

from typing import Any

from adapters.base import AgentResult, register_backend
from adapters.codex import run_codex
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


register_backend("direct:ollama", run)
