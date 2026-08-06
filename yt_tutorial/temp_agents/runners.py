"""One turn of a CLI, for either backend.

Both runners take a prompt and give back a Result. Neither raises: a supervisor
that is waiting on two agents can report which one failed, but it cannot report
anything at all if a failure comes back as an exception from a worker thread.

Kept separate from claude_agents/cli.py on purpose. That module is wired into
the real graphs, roots every call in WORKSPACE_DIR and carries the MCP bridge;
this one is a sandbox that writes to its own directory and speaks to two CLIs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Sequence

from config import (
    CLAUDE_CODE_BIN,
    CLAUDE_CODE_MODEL,
    CLAUDE_CODE_PERMISSION_MODE,
    CODEX_BIN,
    CODEX_MODEL,
    TEMP_AGENT_TIMEOUT_SECONDS,
    TEMP_AGENTS_DIR,
)
from logging_config import get_logger

logger = get_logger(__name__)

BACKENDS = ("claude", "codex")


@dataclass
class Result:
    ok: bool
    text: str = ""
    error: str = ""


def _which(binary: str) -> str:
    """Resolve a CLI name to a path, keeping the name if it cannot be found.

    On Windows the npm-installed codex is codex.cmd, and shutil.which finds it
    through PATHEXT; running the bare name would miss it.
    """
    return shutil.which(binary) or binary


def _run(argv: list[str], *, prompt: str, tag: str) -> subprocess.CompletedProcess | Result:
    """Run a CLI under the deadline, or describe why it could not be run.

    The prompt always goes over stdin. On Windows the codex launcher is a .cmd
    shim, and cmd.exe re-parses the arguments it is handed: a multi-line prompt
    passed as an argument arrives truncated at its first newline. The first run
    of this sandbox lost the "Hi World!" line that way and codex, believing it
    had been asked for an empty file, faithfully produced one.
    """
    logger.debug("[%s] argv: %s", tag, argv)
    try:
        return subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TEMP_AGENT_TIMEOUT_SECONDS,
            cwd=str(TEMP_AGENTS_DIR),
        )
    except FileNotFoundError:
        logger.error("[%s] %r not found on PATH", tag, argv[0])
        return Result(ok=False, error=f"{argv[0]!r} was not found on PATH.")
    except subprocess.TimeoutExpired:
        logger.error("[%s] passed its %ds deadline", tag, TEMP_AGENT_TIMEOUT_SECONDS)
        return Result(ok=False, error=f"Ran past its {TEMP_AGENT_TIMEOUT_SECONDS}s deadline.")


def run_claude(prompt: str, *, tools: Sequence[str], tag: str) -> Result:
    """One Claude Code turn, restricted to `tools`."""
    argv = [
        _which(CLAUDE_CODE_BIN),
        "--print",
        "--output-format",
        "json",
        "--model",
        CLAUDE_CODE_MODEL,
        "--permission-mode",
        CLAUDE_CODE_PERMISSION_MODE,
        "--tools",
        ",".join(tools),
        "--no-session-persistence",
    ]
    started = time.perf_counter()
    logger.info("[%s] claude starting | model=%s | tools=%s", tag, CLAUDE_CODE_MODEL, ",".join(tools))
    outcome = _run(argv, prompt=prompt, tag=tag)
    if isinstance(outcome, Result):
        return outcome

    elapsed = time.perf_counter() - started
    if outcome.stderr.strip():
        logger.debug("[%s] stderr:\n%s", tag, outcome.stderr.strip())
    try:
        envelope = json.loads(outcome.stdout)
    except json.JSONDecodeError:
        logger.error("[%s] claude printed no result object (exit %d)", tag, outcome.returncode)
        return Result(ok=False, error=f"claude exited {outcome.returncode} without a result object.")

    text = (envelope.get("result") or "").strip()
    if envelope.get("is_error"):
        logger.error("[%s] claude failed after %.1fs: %s", tag, elapsed, text[:200])
        return Result(ok=False, error=text or "unknown error")

    logger.info(
        "[%s] claude done | %.1fs | $%.4f | %s",
        tag,
        elapsed,
        float(envelope.get("total_cost_usd") or 0.0),
        text[:120],
    )
    return Result(ok=True, text=text)


def _codex_error(stderr: str) -> str:
    """The line codex actually failed on.

    codex prints a banner -- workdir, model, sandbox, warnings -- before it does
    anything, and reports a failure at the very end. Taking the head of stderr
    therefore returns the banner and hides the reason, which is how a rejected
    model spent a run looking like a mysterious empty result.
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


def run_codex(prompt: str, *, tag: str) -> Result:
    """One Codex turn.

    The reply is collected through --output-last-message rather than parsed out
    of stdout: codex exec streams its reasoning and tool activity there, and the
    final answer has no marker around it to find.
    """
    handle, last_message = tempfile.mkstemp(prefix="codex-", suffix=".txt")
    os.close(handle)

    argv = [
        _which(CODEX_BIN),
        "exec",
        "--cd",
        str(TEMP_AGENTS_DIR),
        # Writing inside the working root is exactly what this agent is for;
        # anything beyond it should still be refused.
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        # No session files: these runs are one-shot and never resumed.
        "--ephemeral",
        "--output-last-message",
        last_message,
    ]
    if CODEX_MODEL:
        argv += ["--model", CODEX_MODEL]
    # "-" is codex's own marker for "the prompt is on stdin".
    argv.append("-")

    started = time.perf_counter()
    logger.info("[%s] codex starting | model=%s", tag, CODEX_MODEL or "(codex default)")
    try:
        outcome = _run(argv, prompt=prompt, tag=tag)
        if isinstance(outcome, Result):
            return outcome

        elapsed = time.perf_counter() - started
        if outcome.stderr.strip():
            logger.debug("[%s] stderr:\n%s", tag, outcome.stderr.strip())
        logger.debug("[%s] stdout:\n%s", tag, outcome.stdout.strip())

        try:
            with open(last_message, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except OSError:
            text = ""

        if outcome.returncode != 0:
            reason = text or _codex_error(outcome.stderr) or f"codex exited {outcome.returncode}"
            logger.error(
                "[%s] codex exited %d after %.1fs: %s", tag, outcome.returncode, elapsed, reason
            )
            return Result(ok=False, error=reason)
        if not text:
            logger.error("[%s] codex wrote no final message", tag)
            return Result(ok=False, error="codex produced no final message.")

        logger.info("[%s] codex done | %.1fs | %s", tag, elapsed, text[:120])
        return Result(ok=True, text=text)
    finally:
        try:
            os.unlink(last_message)
        except OSError:
            pass


def run(backend: str, prompt: str, *, tools: Sequence[str], tag: str) -> Result:
    """Dispatch to the backend named in config."""
    if backend == "claude":
        return run_claude(prompt, tools=tools, tag=tag)
    if backend == "codex":
        return run_codex(prompt, tag=tag)
    return Result(ok=False, error=f"Unknown backend {backend!r}; expected one of {BACKENDS}.")
