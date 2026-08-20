"""The Codex CLI as a subprocess.

Modelled on yt_tutorial/temp_agents/runners.py. Every clause below that looks
like superstition is a Windows or Codex defect from docs/Bugs.md #23, each of
which produced a *plausible empty result* rather than an error — which is the
same signature as #15 and #21 and the reason this project refuses self-reports.

What Codex does not give you, and Claude Code does: a cost figure (Research.md
topic 17). `--json` reports token counts on `turn.completed` but never a price,
so `cost_usd` stays 0.0 here, and that is a measured absence rather than a free
turn — the ledger's mixed state, per ADR-006.

What it does give you, once asked in the right way, is a resumable session id.
It is only ever printed as a `--json` event, and only ever persisted when the
turn is not `--ephemeral`; both are set below, which is what lets the reviewer's
rework loop reach the same Codex agent it reaches on the Claude path.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
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
_PROVIDER_ID = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


def _debug_block(text: str) -> str:
    if len(text) <= _DEBUG_BLOCK_LIMIT:
        return text
    head = text[:4000]
    tail = text[-4000:]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n... [{omitted} chars omitted] ...\n{tail}"


def _codex_error(stderr: str) -> str:
    """The line codex actually failed on.

    Codex prints a banner — workdir, model, sandbox, feature warnings — before it
    does anything, and reports a failure at the very end. Taking the head of
    stderr therefore returns the banner and hides the reason, which is how a
    rejected model spent a whole run looking like a mysterious empty result.
    """
    errors = [
        line.strip()[len("ERROR:") :].strip()
        for line in stderr.splitlines()
        if line.strip().startswith("ERROR:")
    ]
    if not errors:
        return stderr.strip()[-300:]
    try:
        return json.loads(errors[-1])["error"]["message"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return errors[-1]


def _codex_failure(stdout: str) -> str:
    """Return the structured failure emitted by ``codex exec --json``."""
    fallback = ""
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "error" and isinstance(payload.get("message"), str):
            fallback = payload["message"]
        if payload.get("type") == "turn.failed":
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"]
    return fallback


def _thread_id(stdout: str) -> str:
    """The resumable session id for this turn, from codex's JSONL events.

    `--json` is the only place `codex exec` reports it; the human-readable banner
    prints one too, but on stderr, interleaved with warnings, and this project
    does not parse prose it can parse a record instead.

    `thread.started` is the first event on a cold start *and* on a resume, and on
    a resume it repeats the id being resumed — so one rule covers both and the
    caller never has to know which kind of turn it asked for.

    Every other event is ignored on purpose. Notably, `item.completed` events
    with `"type": "error"` are emitted on turns that succeed (they carry feature
    warnings), so reading them as failures would fail a run that worked.
    """
    for line in stdout.splitlines():
        # A BOM survives some Windows pipelines and would break json.loads on
        # the very first line — which is the only line that matters here.
        line = line.strip().lstrip("﻿")
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "thread.started":
            thread = payload.get("thread_id")
            if isinstance(thread, str) and thread:
                return thread
    return ""


def run_codex(
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
    local_provider: str = "",
    custom_provider_id: str = "",
    custom_provider_base_url: str = "",
    custom_provider_env_key: str = "",
    backend_label: str = "Codex",
    disable_memories: bool = False,
) -> AgentResult:
    """One Codex-backed turn. Returns a result; never raises.

    Codex has no `--append-system-prompt`, so a system prompt is prepended to the
    user prompt instead. It also has no tool allowlist of the kind Claude Code
    exposes — `tools` is accepted for interface parity and deliberately unused;
    the sandbox mode is what bounds it.

    `extra_dirs` is not cosmetic here: `--sandbox workspace-write` confines
    writes to `--cd`'s tree (verified against the installed `codex exec --help`),
    so an agent whose worktree is one directory and whose shared artifacts live
    in another cannot write to the second without `--add-dir` naming it
    explicitly.
    """
    if local_provider and custom_provider_id:
        return AgentResult(
            ok=False,
            error_kind="bad_request",
            error_message="Codex cannot use a built-in local provider and a custom provider together.",
        )
    if custom_provider_id and (
        not _PROVIDER_ID.fullmatch(custom_provider_id)
        or not custom_provider_base_url
        or not custom_provider_env_key
    ):
        return AgentResult(
            ok=False,
            error_kind="bad_request",
            error_message="The Codex custom provider configuration is incomplete or invalid.",
        )

    handle, last_message = tempfile.mkstemp(prefix="codex-", suffix=".txt")
    os.close(handle)

    # Everything up to the subcommand belongs to `exec` and must be placed before
    # it: `codex exec resume` has no --cd and no --sandbox of its own, so moving
    # these after `resume` makes the invocation unparseable. Verified against
    # codex-cli 0.146.0, where this order resumes into the right workdir.
    argv = [
        resolve_binary(config.CODEX_BIN),
        "exec",
        "--cd",
        cwd,
        "--sandbox",
        config.CODEX_SANDBOX,
        "--skip-git-repo-check",
        # Events as JSONL on stdout, which is where the session id comes from.
        # This does not disturb --output-last-message; both are populated.
        "--json",
    ]
    if local_provider:
        # Codex assumes reasoning for unknown local models; Ollama rejects that
        # request for non-thinking models such as Qwen 2.5 Coder.
        argv += [
            "--oss",
            "--local-provider",
            local_provider,
            "-c",
            "model_reasoning_effort=none",
        ]
    elif custom_provider_id:
        # Codex custom providers speak the Responses API only. JSON string
        # encoding is also valid TOML string encoding and keeps URLs/names safe
        # when passed through repeated `-c key=value` overrides.
        provider = f"model_providers.{custom_provider_id}"
        argv += [
            "-c",
            f"model_provider={json.dumps(custom_provider_id)}",
            "-c",
            f"{provider}.name={json.dumps(backend_label)}",
            "-c",
            f"{provider}.base_url={json.dumps(custom_provider_base_url)}",
            "-c",
            f"{provider}.env_key={json.dumps(custom_provider_env_key)}",
            "-c",
            f'{provider}.wire_api="responses"',
            "-c",
            "model_reasoning_effort=none",
        ]
    if disable_memories:
        argv += ["--disable", "memories"]
    for extra_dir in extra_dirs:
        argv += ["--add-dir", extra_dir]
    argv += [
        # The final answer is collected from this file rather than parsed out of
        # stdout: `codex exec` streams its reasoning there with no marker around
        # the answer, so there is nothing to find.
        "--output-last-message",
        last_message,
    ]
    # An empty model is the only value guaranteed to be one the account may use:
    # naming a model got every run rejected with "not supported when using Codex
    # with a ChatGPT account".
    if spec.model:
        argv += ["--model", spec.model]
    # No --ephemeral counterpart on the cold-start branch, and that omission is
    # the whole feature. --ephemeral means "persist no session file", and codex
    # can only resume from a rollout on disk: an ephemeral turn hands back a
    # thread id that `resume` then rejects with "no rollout found for thread id".
    # The cost is a session file per dispatch under CODEX_HOME, which is the
    # price of the reviewer's rework loop reaching the agent that did the work.
    if resume_session:
        argv += ["resume", resume_session]
    # "-" is codex's own marker for "the prompt is on stdin". The prompt never
    # goes as an argument: the npm-installed codex is a .cmd shim, cmd.exe
    # re-parses its arguments, and a multi-line prompt arrives truncated at its
    # first newline. Codex then faithfully does the truncated thing.
    argv.append("-")

    full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    logger.debug("[%s] argv: %s", tag, argv)
    logger.debug("[%s] prompt:\n%s", tag, _debug_block(full_prompt))

    started = time.perf_counter()
    try:
        try:
            completed = run_with_deadline(
                argv,
                input=full_prompt,
                cwd=cwd,
                timeout=spec.deadline_seconds,
            )
        except FileNotFoundError:
            return AgentResult(
                ok=False,
                error_kind="not_installed",
                error_message=(
                    f"{backend_label} requires Codex CLI {config.CODEX_BIN!r}, which was not "
                    "found from this process's "
                    "environment. Set CODEX_BIN or install it."
                ),
                duration_seconds=time.perf_counter() - started,
            )
        except subprocess.TimeoutExpired as exc:
            # `thread.started` is the first event codex emits, so a turn killed at
            # its deadline has almost certainly already reported its id. Keeping
            # it is what lets the retry resume the agent that was part-way through
            # rather than brief a fresh one on work it cannot see.
            return AgentResult(
                ok=False,
                error_kind="timeout",
                error_message=(
                    f"{backend_label} ran past its {spec.deadline_seconds}s deadline and was "
                    "abandoned. Anything it saved before then is still on disk."
                ),
                duration_seconds=time.perf_counter() - started,
                session_id=_thread_id(exc.stdout or ""),
            )

        elapsed = time.perf_counter() - started
        stderr = (completed.stderr or "").strip()
        stdout = completed.stdout or ""
        if stderr:
            logger.debug("[%s] stderr:\n%s", tag, _debug_block(stderr))
        logger.debug("[%s] stdout:\n%s", tag, _debug_block(stdout.strip()))

        # Carried onto the failure results too: a turn that errored or printed
        # nothing may still have a resumable session behind it, and the rework
        # loop is exactly the caller that wants it.
        session_id = _thread_id(stdout)

        try:
            with open(last_message, "r", encoding="utf-8") as handle_in:
                text = handle_in.read().strip()
        except OSError:
            text = ""

        if completed.returncode != 0:
            reason = (
                text
                or _codex_failure(stdout)
                or _codex_error(stderr)
                or f"codex exited {completed.returncode}"
            )
            return AgentResult(
                ok=False,
                error_kind=classify_failure(reason),
                error_message=reason,
                duration_seconds=elapsed,
                session_id=session_id,
            )
        if not text:
            return AgentResult(
                ok=False,
                error_kind="no_output",
                error_message=(
                    "codex exited 0 but wrote no final message. "
                    f"{_codex_failure(stdout) or _codex_error(stderr) or '(no error event)'}"
                ),
                duration_seconds=elapsed,
                session_id=session_id,
            )

        logger.info(
            "[%s] %s done | %.1fs | session=%s",
            tag,
            backend_label.lower(),
            elapsed,
            session_id[:8] or "-",
        )
        logger.info("[%s] reply: %s", tag, text[:_CONSOLE_EXCERPT])
        logger.debug("[%s] full reply:\n%s", tag, _debug_block(text))
        return AgentResult(
            ok=True, text=text, duration_seconds=elapsed, session_id=session_id
        )
    finally:
        try:
            os.unlink(last_message)
        except OSError:
            pass


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
    )


register_backend("direct:codex", run)
