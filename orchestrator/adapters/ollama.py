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

# Desktop-app features Codex otherwise wires into every headless turn:
# collaboration/multi-agent tooling, plugin and MCP surfaces, and interactive
# extras (browser/computer use, image generation, personality) that a
# single-worktree coding task never touches. Measured against this project's
# Bugs.md #44/#39 Ollama-bridge findings: a small local model given this much
# irrelevant tool surface buried the actual task and either stalled asking for
# input or hallucinated an unrelated tool call, instead of writing the file.
_IRRELEVANT_FEATURES = (
    "browser_use",
    "computer_use",
    "apps",
    "image_generation",
    "in_app_browser",
    "in_app_updates",
    "plugins",
    "plugin_sharing",
    "remote_plugin",
    "multi_agent",
    "multi_agent_v2",
    "goals",
    "personality",
    "hooks",
    "mentions_v2",
    "skill_search",
    "skill_mcp_dependency_install",
    "default_mode_request_user_input",
)


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
        # The local model needs Codex's coding-agent harness, not the operator's
        # interactive desktop profile that harness also happens to load. See
        # run_codex()'s docstring and Bugs.md #44/#39.
        isolate_from_user_config=True,
        disable_features=_IRRELEVANT_FEATURES,
    )


register_backend("direct:ollama", run)
