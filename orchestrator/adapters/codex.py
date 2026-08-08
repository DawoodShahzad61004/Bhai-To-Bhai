"""The Codex CLI as a subprocess.

Modelled on yt_tutorial/temp_agents/runners.py. Every clause below that looks
like superstition is a Windows or Codex defect from docs/Bugs.md #23, each of
which produced a *plausible empty result* rather than an error — which is the
same signature as #15 and #21 and the reason this project refuses self-reports.

What Codex does not give you, and Claude Code does: any usage or cost figure at
all (Research.md topic 17). `cost_usd` therefore stays 0.0 here, and that is a
measured absence rather than a free turn — the ledger's mixed state, per ADR-006.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from typing import Any

from adapters.base import AgentResult, classify_failure, register_backend, resolve_binary, subprocess_env
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
) -> AgentResult:
    """One Codex turn. Returns a result; never raises.

    Codex has no `--append-system-prompt`, so a system prompt is prepended to the
    user prompt instead. It also has no tool allowlist of the kind Claude Code
    exposes — `tools` is accepted for interface parity and deliberately unused;
    the sandbox mode is what bounds it.
    """
    handle, last_message = tempfile.mkstemp(prefix="codex-", suffix=".txt")
    os.close(handle)

    argv = [
        resolve_binary(config.CODEX_BIN),
        "exec",
        "--cd",
        cwd,
        "--sandbox",
        config.CODEX_SANDBOX,
        "--skip-git-repo-check",
        # The final answer is collected from this file rather than parsed out of
        # stdout: `codex exec` streams its reasoning there with no marker around
        # the answer, so there is nothing to find.
        "--output-last-message",
        last_message,
    ]
    if resume_session:
        argv += ["resume", resume_session]
    else:
        argv += ["--ephemeral"]
    # An empty model is the only value guaranteed to be one the account may use:
    # naming a model got every run rejected with "not supported when using Codex
    # with a ChatGPT account".
    if spec.model:
        argv += ["--model", spec.model]
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
            completed = subprocess.run(
                argv,
                input=full_prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=spec.deadline_seconds,
                cwd=cwd,
                env=subprocess_env(),
            )
        except FileNotFoundError:
            return AgentResult(
                ok=False,
                error_kind="not_installed",
                error_message=(
                    f"Codex CLI {config.CODEX_BIN!r} was not found from this process's "
                    "environment. Set CODEX_BIN or install it."
                ),
                duration_seconds=time.perf_counter() - started,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(
                ok=False,
                error_kind="timeout",
                error_message=(
                    f"Codex ran past its {spec.deadline_seconds}s deadline and was "
                    "abandoned. Anything it saved before then is still on disk."
                ),
                duration_seconds=time.perf_counter() - started,
            )

        elapsed = time.perf_counter() - started
        stderr = (completed.stderr or "").strip()
        if stderr:
            logger.debug("[%s] stderr:\n%s", tag, _debug_block(stderr))
        logger.debug("[%s] stdout:\n%s", tag, _debug_block((completed.stdout or "").strip()))

        try:
            with open(last_message, "r", encoding="utf-8") as handle_in:
                text = handle_in.read().strip()
        except OSError:
            text = ""

        if completed.returncode != 0:
            reason = text or _codex_error(stderr) or f"codex exited {completed.returncode}"
            return AgentResult(
                ok=False,
                error_kind=classify_failure(reason),
                error_message=reason,
                duration_seconds=elapsed,
            )
        if not text:
            return AgentResult(
                ok=False,
                error_kind="no_output",
                error_message=(
                    "codex exited 0 but wrote no final message. "
                    f"{_codex_error(stderr) or '(no stderr)'}"
                ),
                duration_seconds=elapsed,
            )

        logger.info("[%s] codex done | %.1fs", tag, elapsed)
        logger.info("[%s] reply: %s", tag, text[:_CONSOLE_EXCERPT])
        logger.debug("[%s] full reply:\n%s", tag, _debug_block(text))
        # Codex has no session id in `exec` output, so the reviewer's rework loop
        # cold-starts against this vendor rather than resuming. Stated here
        # rather than discovered later.
        return AgentResult(ok=True, text=text, duration_seconds=elapsed)
    finally:
        try:
            os.unlink(last_message)
        except OSError:
            pass


register_backend("direct:codex", run)
