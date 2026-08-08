"""The adapter contract every agent node talks to.

One rule governs this layer, and it is the sentence docs/Bugs.md #19 exists for:

    A supervisor can route around a worker that failed; it cannot route around a
    traceback.

So `run_agent` **never raises**. A missing binary, a blown deadline, a rejected
model, a rate limit and a malformed reply all come back as an `AgentResult` with
`ok=False` and a classified `error_kind`. Converting an exception into a result
is not error-swallowing — it is what makes the failure routable, and it preserves
the property that matters most, which is that work already on disk survives.

The second rule is ADR-010's: a vendor's quirks are absorbed **here, once**, and
never in the nodes or the routers downstream. Research.md topic 17 catalogues
nine behaviours a coding-agent CLI has that a model API does not; every one of
them is this layer's problem.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import config
from config import AgentSpec
from logging_config import get_logger

logger = get_logger(__name__)

# Failure classes a caller may usefully branch on. Kept closed so a router can
# match exhaustively rather than grepping message text.
ERROR_KINDS = (
    "not_installed",  # the CLI is not resolvable from THIS process's environment
    "timeout",  # the wall-clock deadline fired; the turn was abandoned
    "rate_limit",  # a vendor limit; authoritative per ADR-006, unlike an estimate
    "no_output",  # the process ran and produced nothing parseable
    "agent_error",  # the CLI itself reported the turn as failed
    "bad_request",  # the invocation was malformed or the model was refused
)


@dataclass
class AgentResult:
    """The outcome of one agent invocation.

    Mirrors the sandbox's ClaudeResult and LLMResult on purpose: same shape, same
    promise. A caller checks `ok` and never needs a try block.
    """

    ok: bool

    # ── Success ──────────────────────────────────────────────────────────────
    text: str = ""
    # Populated only when the caller asked for structured output and got it.
    structured: dict[str, Any] | None = None

    # ── Failure ──────────────────────────────────────────────────────────────
    error_kind: str = ""
    error_message: str = ""

    # ── Always populated where the CLI reported it ───────────────────────────
    # Consumption, never remaining — no vendor exposes remaining allowance
    # (Research.md topic 6). Claude Code reports all of this; Codex reports none
    # of it, which is the mixed-measurement state ADR-006 argues for on principle.
    cost_usd: float = 0.0
    turns: int = 0
    duration_seconds: float = 0.0
    # The vendor's own resumable session id. Load-bearing for the reviewer's
    # rework loop, which must reach the SAME coding subagent (docs/Decisions.md ADR-018,
    # agent 5), and free of charge as a Session Registry key.
    session_id: str = ""
    denials: list[Any] = field(default_factory=list)

    def summary(self) -> str:
        """One line for a report or an event, whichever way the turn went."""
        if self.ok:
            return f"ok in {self.duration_seconds:.1f}s (${self.cost_usd:.4f})"
        return f"{self.error_kind}: {self.error_message[:200]}"


class Backend(Protocol):
    """What every transport must implement."""

    def __call__(
        self,
        prompt: str,
        *,
        spec: AgentSpec,
        system_prompt: str,
        cwd: str,
        tag: str,
        tools: tuple[str, ...],
        json_schema: dict[str, Any] | None,
        resume_session: str,
    ) -> AgentResult: ...


# Keyed by dispatch key, not by vendor. Two axes are in play and conflating them
# is a bug waiting to happen:
#
#   transport (config.INVOCATION) -- HOW the CLI is reached: direct subprocess,
#                                    through `maestro delegate`, or not at all.
#   vendor    (AgentSpec.backend) -- WHICH CLI: claude or codex.
#
# The direct transport needs one adapter per vendor, so it registers under
# "direct:<vendor>". `maestro` and `stub` speak to any vendor and register under
# their own name alone.
_BACKENDS: dict[str, Backend] = {}


def register_backend(key: str, backend: Backend) -> None:
    """Make a transport reachable by dispatch key.

    Use `"direct:<vendor>"` for a vendor-specific adapter and a bare transport
    name for one that handles every vendor itself.
    """
    _BACKENDS[key] = backend


def available_backends() -> tuple[str, ...]:
    return tuple(sorted(_BACKENDS))


def _dispatch_key(invocation: str, vendor: str) -> str:
    return f"direct:{vendor}" if invocation == "direct" else invocation


def classify_failure(text: str, default: str = "agent_error") -> str:
    """Map a vendor's message onto an ERROR_KINDS member.

    Vendors reword their own errors, so the markers this matches live in
    config.py rather than here (ADR-006). A rate limit is the one class the
    pipeline treats as authoritative rather than advisory: it means wait or fail
    over, not that the request was wrong.

    Read through the `config` module rather than a from-import, so editing
    config.py or the environment actually changes behaviour. A from-import binds
    the value once at import time and quietly ignores every later change — which
    is a configuration surface that only looks configurable.
    """
    lowered = text.lower()
    if any(marker in lowered for marker in config.RATE_LIMIT_MARKERS):
        return "rate_limit"
    return default


def resolve_binary(name: str) -> str:
    """Resolve a CLI name to a path, keeping the name if it cannot be found.

    On Windows the npm-installed codex is codex.cmd and is only found through
    PATHEXT, so running the bare name misses it. Keeping the original name on a
    miss lets the caller produce a "not installed" result rather than a
    FileNotFoundError from somewhere less informative.
    """
    return shutil.which(name) or name


def subprocess_env() -> dict[str, str]:
    """Environment passed to vendor CLIs.

    Coding agents run project tools from inside their own subprocesses. If a
    parent shell leaks Python path overrides, Windows console-script launchers
    can mix one Python's site-packages with another Python's stdlib/DLLs; the
    observed signature is `ImportError` while importing compiled stdlib modules
    such as `_bz2`. Keep user-site packages available, but remove interpreter
    overrides and force UTF-8 text boundaries.
    """
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    local_bin = str(config.PROJECT_ROOT / "node_modules" / ".bin")
    path_parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    if local_bin not in path_parts:
        env["PATH"] = os.pathsep.join([local_bin, *path_parts])
    return env


def run_agent(
    prompt: str,
    *,
    spec: AgentSpec,
    system_prompt: str = "",
    cwd: str,
    tag: str,
    tools: tuple[str, ...] = (),
    json_schema: dict[str, Any] | None = None,
    resume_session: str = "",
    invocation: str | None = None,
) -> AgentResult:
    """Run one agent turn. Returns a result; never raises.

    `tag` names the caller in the log and is the only way a run with three
    concurrent coding subagents stays readable.

    `resume_session` continues a vendor session rather than cold-starting one.
    The reviewer's rework loop needs this: its comments go back to the *same*
    subagent, which is materially cheaper than briefing a fresh one and is the
    only way "here is what you got wrong" refers to anything.
    """
    transport = invocation or config.INVOCATION
    key = _dispatch_key(transport, spec.backend)
    backend = _BACKENDS.get(key)
    if backend is None:
        # A misconfigured transport is a caller error, not an agent failure, but
        # it still comes back as a result — the graph has no better place to
        # handle an exception than the node that would have to re-raise it.
        message = (
            f"No adapter for transport {transport!r} with backend {spec.backend!r} "
            f"(dispatch key {key!r}). Registered: {available_backends()}."
        )
        logger.error("[%s] %s", tag, message)
        return AgentResult(ok=False, error_kind="bad_request", error_message=message)

    logger.info(
        "[%s] dispatch | transport=%s | backend=%s | model=%s | deadline=%ds",
        tag,
        transport,
        spec.backend,
        spec.model or "(cli default)",
        spec.deadline_seconds,
    )
    result = backend(
        prompt,
        spec=spec,
        system_prompt=system_prompt,
        cwd=cwd,
        tag=tag,
        tools=tools,
        json_schema=json_schema,
        resume_session=resume_session,
    )
    if result.ok:
        logger.info("[%s] %s", tag, result.summary())
    else:
        logger.error("[%s] %s", tag, result.summary())
    return result


def _load_builtin_backends() -> None:
    """Register the transports that ship with the project.

    Each module registers itself on import. `importlib` rather than a `from
    adapters import ...`, because this runs while the `adapters` package is still
    mid-initialization and a submodule import is the form that works there.

    `stub` is loaded first and unguarded: it must always be available so the test
    suite and an offline rehearsal work with no CLI installed at all. A vendor
    adapter that cannot import should not take the whole package down with it.
    """
    import importlib

    importlib.import_module("adapters.stub")

    for module in ("claude", "codex", "maestro"):
        try:
            importlib.import_module(f"adapters.{module}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("adapter %r unavailable: %s", module, exc)


_load_builtin_backends()
