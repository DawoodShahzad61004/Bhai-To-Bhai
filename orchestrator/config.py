"""Central configuration for the six-agent pipeline.

Every tunable lives here. Environment variables (loaded from .env) win where a
setting is deployment-specific; the literals below are the defaults.

Two rules this file follows, both bought with debugging cycles recorded in
docs/Bugs.md:

  * A constant that encodes a *finding* carries the finding. `MAX_REWORK_ROUNDS`
    is not an arbitrary number, and a reader should not have to guess why.
  * A default that selects a paid external dependency should be inert wherever
    that is possible, so a misconfiguration costs a failed run and not a bill
    (Bugs.md #25).

Prompts are NOT here. They change for different reasons and by different people:
a prompt is rewritten when an agent misbehaves, a constant is retuned when a
limit binds. Each agent keeps its own brief in its own package (ADR-012).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: bool) -> bool:
    """Read a boolean switch from the environment."""
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE TOGGLES  —  the two switches the pipeline is built around
# ═══════════════════════════════════════════════════════════════════════════════
# Turning either off removes the node from the graph entirely rather than making
# it a pass-through, so a disabled stage costs nothing and appears nowhere in the
# run log. graph.py rewires the edges around whatever is absent.
#
# What you lose in each case is stated plainly, because both of these are quality
# gates and switching one off is a decision, not a preference:
#
#   ENABLE_REVIEWER  = False -> no per-wave check that the merged work matches the
#                              task files. The Review -> Orchestrate rework loop
#                              disappears; a wave is accepted the moment it merges.
#   ENABLE_SUPERVISOR = False -> no final check that the finished work satisfies
#                              context.md. The Supervisor -> Plan replan loop
#                              disappears; the run ends when the last wave merges.
#
# With both off the pipeline is a straight line: requirements -> plan -> waves.
ENABLE_REVIEWER = _flag("ENABLE_REVIEWER", True)
ENABLE_SUPERVISOR = _flag("ENABLE_SUPERVISOR", True)


# ═══════════════════════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent

# Controller-side run identity and legacy artifact source. Current artifacts
# live under `<target_repo>/runs/`; this directory remains the stable checkpoint
# input for resumed runs and lets `artifacts.prepare()` migrate the old flat
# layout without breaking existing run ids.
RUNS_DIR = Path(os.getenv("RUNS_DIR", str(BASE_DIR / "runs")))

# One .debug.log per run, kept for after-the-fact inspection.
RUN_LOGS_DIR = Path(os.getenv("RUN_LOGS_DIR", str(BASE_DIR / "run_logs")))


# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════
# Console readable, file complete. Bugs.md #15 was found in a DEBUG file and was
# invisible on the console, which is the argument for keeping both levels.
CONSOLE_LOG_LEVEL = logging.INFO
FILE_LOG_LEVEL = logging.DEBUG


# ═══════════════════════════════════════════════════════════════════════════════
# INVOCATION TRANSPORT
# ═══════════════════════════════════════════════════════════════════════════════
# How an agent node reaches a CLI.
#
#   "direct"  -- subprocess straight to the vendor CLI. Proven on this machine;
#                carries every fix in Bugs.md #21-#23 (stdin prompts, error read
#                from the end of stderr, no hardcoded model, wall-clock deadline).
#   "maestro" -- `maestro delegate`, per docs/Research.md topic 19. Adds the Session/Run
#                lifecycle and message injection, at the cost of a second process
#                layer. Note that `--timeout` there is a STALE-STREAM timeout, so
#                the wall-clock deadline below still applies on top of it.
#   "stub"    -- no subprocess at all; replies come from a registered fixture.
#                This is what the test suite runs on, and it is a first-class
#                backend rather than a mock so that a run can be rehearsed
#                offline for free.
INVOCATION = os.getenv("INVOCATION", "direct")

# Executable resolution. Never a bare name embedded at a call site: Bugs.md #3 is
# what happens when a dependency moves and one consumer hardcoded its lookup.
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
CODEX_BIN = os.getenv("CODEX_BIN", "codex")
COPILOT_BIN = os.getenv("COPILOT_BIN", "copilot")
# Maestro is installed repo-locally (ADR-007), so it is NOT on PATH. Resolution
# order is: this setting -> project-local node_modules -> PATH.
MAESTRO_BIN = os.getenv("MAESTRO_BIN", str(PROJECT_ROOT / "node_modules" / ".bin" / "maestro"))

# Claude Code prompts for permission on every tool call by default and there is
# no terminal here to answer it: the process would sit until its deadline.
CLAUDE_PERMISSION_MODE = os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions")

# Codex's sandbox. Writing inside the working root is what a coding agent is for;
# anything beyond it should still be refused.
CODEX_SANDBOX = os.getenv("CODEX_SANDBOX", "workspace-write")

# ══════════════════════════════════════════════════════════════════════════════
# AGENT ROSTER
# ═════════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class AgentSpec:
    """Which CLI runs one pipeline stage, and under what limits.

    `deadline_seconds` is WALL-CLOCK over the whole turn, not a per-read socket
    timeout. Bugs.md #17 is a turn that ran 388 seconds under a live 150-second
    read timeout that structurally could not fire, because a server holding a
    socket open while it generates is never idle. A limit has to name its unit.
    """

    backend: str  # "claude" | "codex" | "copilot" | "ollama"
    model: str  # "" defers to whatever the CLI itself selects
    deadline_seconds: int
    # Dollar ceiling for one invocation. 0 leaves it uncapped. Honoured by the
    # Claude Code adapter; Codex exposes no equivalent (Research.md topic 17).
    max_budget_usd: float = 0.0


# The drawing's organizing idea, made concrete: every box that makes a quality or
# correctness judgment gets the larger model, every box that moves data around
# gets the smaller one. See docs/Research.md topic 20.
#
# The notes assign agents 1, 3 and 4 to the Gemini CLI. Gemini is not installed
# on this machine and is disabled in ~/.maestro/cli-tools.json, so the tiers are
# expressed as haiku vs sonnet on Claude Code. Any entry can be switched to
# `backend="codex"` or `backend="copilot"` — the adapter layer makes them interchangeable.
AGENTS: dict[str, AgentSpec] = {
    # ── Smaller model: mechanical / dispatch work ────────────────────────────
    "requirements": AgentSpec(backend="copilot", model="auto", deadline_seconds=900),
    "wave_orchestrator": AgentSpec(backend="ollama", model="gpt-oss:20b-cloud", deadline_seconds=900),
    "merger": AgentSpec(backend="codex", model="", deadline_seconds=900),
    # ── Larger model: judgment work ──────────────────────────────────────────
    "planner": AgentSpec(backend="codex", model="", deadline_seconds=600),
    "reviewer": AgentSpec(backend="codex", model="", deadline_seconds=600),
    "supervisor": AgentSpec(backend="codex", model="", deadline_seconds=600),
}

# The coding subagents dispatched inside a wave. These are the only agents that
# write to the target repository, and they are the reason the merger exists.
#
# A wave alternates its tasks between these two rather than cloning one spec,
# so a run can put two different vendors (or the same vendor twice) to work in
# parallel. CODING_AGENT_{A,B}_BACKEND/MODEL override per slot; unset, both
# fall back to the shared CODING_AGENT_BACKEND/MODEL, which reproduces the old
# single-agent behavior with A and B identical.
CODING_AGENT_A = AgentSpec(
    backend=os.getenv("CODING_AGENT_A_BACKEND", os.getenv("CODING_AGENT_BACKEND", "codex")),
    model=os.getenv("CODING_AGENT_A_MODEL", os.getenv("CODING_AGENT_MODEL", "")),
    deadline_seconds=_int("CODING_AGENT_DEADLINE_SECONDS", 900),
)
CODING_AGENT_B = AgentSpec(
    backend=os.getenv("CODING_AGENT_B_BACKEND", os.getenv("CODING_AGENT_BACKEND", "codex")),
    model=os.getenv("CODING_AGENT_B_MODEL", os.getenv("CODING_AGENT_MODEL", "")),
    deadline_seconds=_int("CODING_AGENT_DEADLINE_SECONDS", 900),
)

MAX_CODING_AGENT_COUNT = 5

# Bare aliases only ("haiku"/"sonnet"), never a versioned string like
# "sonnet-5" or "sonnet-4-6" — Claude Code's --model only accepts a bare alias
# (resolves to that family's latest) or a full model name (e.g.
# "claude-sonnet-5"); a bare-looking-but-invalid string like "sonnet-5" is
# neither and is rejected outright. Verified against this account directly
# (`claude auth status`: Pro plan): "haiku" -> claude-haiku-4-5-20251001,
# "sonnet" -> claude-sonnet-5, both reachable. Codex is logged in via ChatGPT,
# which rejects a named --model (codex.py's own comment), so "" (CLI default)
# is the only safe entry for that backend, not a specific model string.
SMALL_MEDIUM_MODELS = [
    # ("haiku", "claude"),
    ("auto", "copilot"),
    # ("gpt-oss:120b-cloud", "ollama"),
    ("gpt-oss:20b-cloud", "ollama"),
    # ("mistral-large-3:675b-cloud", "ollama"),
    # ("gemma4:31b-cloud", "ollama"),
    # ("gemma4:cloud", "ollama"),
    # ("qwen3.5:397b-cloud", "ollama"),
    # ("qwen3.5:cloud", "ollama"),
    ("nemotron-3-nano:30b-cloud", "ollama"),
    # ("nemotron-3-super:cloud", "ollama"),
    # ("nemotron-3-ultra:cloud", "ollama"),
    # ("minimax-m3:cloud", "ollama"),
    # ("minimax-m2.7:cloud", "ollama"),
    # ("kimi-k2.6:cloud", "ollama"),
    # ("kimi-k2.7-code:cloud", "ollama"),
    # ("kimi-k3:cloud", "ollama"),
    # ("deepseek-v4-pro:cloud", "ollama"),
    # ("deepseek-v4-flash:cloud", "ollama"),
    # ("glm-5.1:cloud", "ollama"),
    # ("glm-5.2:cloud", "ollama"),
    # ("muse-glimmer:latest", "ollama"),
    # ("devstral:24b-small-2505-q4_K_M", "ollama"),
    # ("qwen2.5-coder:14b-instruct-q4_K_M", "ollama"),
    # ("qwen3:14b-q4_K_M", "ollama"),
    # ("qwen2.5-coder:7b-instruct-q4_K_M", "ollama"),
    # ("qwen3.5:4b", "ollama"),
    # ("qwen3:14b", "ollama"),
    # ("qwen3:8b", "ollama"),
]
EXPERT_MODELS = [
    # ("sonnet", "claude"), 
    ("", "codex"),
]

# ═══════════════════════════════════════════════════════════════════════════════
# TERMINATION BOUNDS
# ═══════════════════════════════════════════════════════════════════════════════
# docs/Decisions.md ADR-017 raises "no termination guard on either
# loop" as an open question. These close it.
#
# The bounds are structural on purpose. A safety property cannot rest on the
# cooperation of the component it constrains (ADR-011), and both feedback edges
# are driven by a model's judgment about whether work is acceptable — which is
# exactly the kind of decision that can be made the same way indefinitely.
#
# Every bound below records WHY it fired. Bugs.md #15 is a run that stopped
# because it was bounded and logged it identically to stopping because it was
# done, and the audit trail reported the second.

# Review -> Orchestrate. How many times one wave may be sent back for rework
# before the pipeline stops trying and escalates it upward.
MAX_REWORK_ROUNDS = _int("MAX_REWORK_ROUNDS", 2)

# Supervisor -> Plan. How many times the whole plan may be redone. Expensive:
# each replan re-runs every wave, so this is deliberately small.
MAX_REPLAN_ROUNDS = _int("MAX_REPLAN_ROUNDS", 1)

# Backstop on wave count, in case a planner emits a pathological decomposition.
# Reaching this is always a lost run, never a clean finish — it is a backstop and
# not a termination strategy (Bugs.md #12).
MAX_WAVES = _int("MAX_WAVES", 20)

# Coding subagents dispatched at once inside one wave. Each is a subprocess
# blocked on a pipe, so the cost of concurrency is one thread each and the real
# constraint is workspace and budget, not dispatch (Research.md topic 18).
MAX_PARALLEL_TASKS = _int("MAX_PARALLEL_TASKS", 3)


# ═══════════════════════════════════════════════════════════════════════════════
# REQUIREMENTS Q&A
# ═══════════════════════════════════════════════════════════════════════════════
# The requirements agent pauses the graph with interrupt() to ask the user. Off
# turns the pause into a no-op, so the agent works from the goal string alone —
# which is what the test suite and any scripted run need.
INTERACTIVE_REQUIREMENTS = _flag("INTERACTIVE_REQUIREMENTS", True)

# Cap on questions in one clarification round, so a chatty agent cannot turn a
# run into an interview.
MAX_CLARIFYING_QUESTIONS = _int("MAX_CLARIFYING_QUESTIONS", 6)


# ═══════════════════════════════════════════════════════════════════════════════
# WORKSPACE
# ═══════════════════════════════════════════════════════════════════════════════
# Each coding subagent in a wave gets its own git worktree on its own branch, and
# the merger merges them. Turning this off runs a wave sequentially against the
# target checkout and makes the merger a pass-through.
USE_GIT_WORKTREES = _flag("USE_GIT_WORKTREES", True)

# Where worktrees are created, relative to the target repository's parent. Kept
# outside the repository so `git status` in the target stays clean.
WORKTREE_DIR_NAME = os.getenv("WORKTREE_DIR_NAME", ".bhai-worktrees")

# Branch names for per-task worktrees and for the branch the merger integrates
# into. `{run}` and `{task}` are substituted.
TASK_BRANCH_TEMPLATE = os.getenv("TASK_BRANCH_TEMPLATE", "bhai/{run}/{task}")
INTEGRATION_BRANCH_TEMPLATE = os.getenv("INTEGRATION_BRANCH_TEMPLATE", "bhai/{run}/integration")

# Seconds any single git command may take.
GIT_TIMEOUT_SECONDS = _int("GIT_TIMEOUT_SECONDS", 120)


# ═══════════════════════════════════════════════════════════════════════════════
# GRAPH
# ═══════════════════════════════════════════════════════════════════════════════
# A super-step budget, not a nesting depth — nothing recurses despite the name,
# and a sub-graph invoked without its own config inherits the parent's
# (Bugs.md #12). Sized for MAX_WAVES waves each capable of MAX_REWORK_ROUNDS
# rework rounds, plus the replan loop, with headroom.
RECURSION_LIMIT = _int("RECURSION_LIMIT", 150)

# The checkpointer backs both crash-resume and the interrupt() the requirements
# agent pauses on. Written per run so a run can be resumed by id.
CHECKPOINT_DIR = Path(os.getenv("CHECKPOINT_DIR", str(BASE_DIR / "checkpoints")))


# ═══════════════════════════════════════════════════════════════════════════════
# VENDOR ERROR STRINGS
# ═══════════════════════════════════════════════════════════════════════════════
# In configuration rather than in code, because vendors reword them (ADR-006).
# A match here classifies a failure as a limit rather than a defect, which is the
# difference between waiting and giving up.
RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "usage limit",
    "quota",
    "429",
    "too many requests",
    "insufficient_quota",
)
