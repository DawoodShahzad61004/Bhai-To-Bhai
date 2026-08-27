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

# ═══════════════════════════════════════════════════════════════════════════════
# STAGE TOGGLES  —  the two switches the pipeline is built around
# ═══════════════════════════════════════════════════════════════════════════════
ENABLE_REVIEWER = True
ENABLE_SUPERVISOR = True


# ═══════════════════════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent

# One .debug.log per run, kept for after-the-fact inspection.
RUN_LOGS_DIR = BASE_DIR / "run_logs"


# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════
CONSOLE_LOG_LEVEL = logging.INFO
FILE_LOG_LEVEL = logging.DEBUG


# ═══════════════════════════════════════════════════════════════════════════════
# INVOCATION TRANSPORT
# ═══════════════════════════════════════════════════════════════════════════════
INVOCATION = "direct" # "direct" | "grpc" | "http" | "cli"

# Executable resolution. 
CLAUDE_BIN = "claude"
CODEX_BIN = "codex"
COPILOT_BIN = "copilot"
GEMINI_BIN = "gemini"
MAESTRO_BIN = str(PROJECT_ROOT / "node_modules" / ".bin" / "maestro")

CLAUDE_PERMISSION_MODE = "bypassPermissions"

CODEX_SANDBOX = "workspace-write"

GEMINI_APPROVAL_MODE = "yolo"


CUSTOM_API_BASE = os.getenv("CUSTOM_API_BASE", "")
CUSTOM_API_KEY = os.getenv("CUSTOM_API_KEY", "")
CUSTOM_API_MODEL_NAME = os.getenv("CUSTOM_API_MODEL_NAME", "")
MAX_OUTPUT_SIZE_FOR_LOCAL_MODEL = 2048

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

    backend: str  # "claude" | "codex" | "gemini" | "copilot" | "ollama" | "local_llm"
    model: str  # "" defers to whatever the CLI itself selects
    deadline_seconds: int
    max_budget_usd: float = 0.0


AGENTS = {
    # ── Smaller model: mechanical / dispatch work ────────────────────────────
    "requirements": AgentSpec(backend="claude", model="haiku", deadline_seconds=900,),
    "wave_orchestrator": AgentSpec(backend="gemini", model="gemini-3.1-flash-lite", deadline_seconds=900,),
    "merger": AgentSpec(backend="gemini", model="gemini-3.1-flash-lite", deadline_seconds=900,),
    # ── Stronger model: judgment work ────────────────────────────────────────
    "planner": AgentSpec(backend="claude", model="sonnet", deadline_seconds=600,),
    "reviewer": AgentSpec(backend="claude", model="sonnet", deadline_seconds=600,),
    "supervisor": AgentSpec(backend="claude", model="sonnet", deadline_seconds=600,),
}

CODING_AGENT_A = AgentSpec(
    backend="codex",
    model="",
    deadline_seconds=900,
)
CODING_AGENT_B = AgentSpec(
    backend="codex",
    model="",
    deadline_seconds=900,
)

MAX_CODING_AGENT_COUNT = 3

# Split by model scale, not by backend: within a tier, judgment about which
# backend can actually finish a given task (Bugs.md's Ollama-bridge findings —
# `apply_patch` unsupported and the shell fallback blocked by sandbox policy,
# reproduced against both a 4B and a 20B model) lives in the planner prompt,
# not in which list a model sits in.
SMALL_MODELS = [
    # ("haiku", "claude"),
    ("qwen3.5:4b", "ollama"),
    # ("qwen3:8b", "ollama"),
    # ("gemini-3.1-flash-lite", "gemini"),
]
MEDIUM_MODELS = [
    ("auto", "copilot"),
    ("QuantTrio/Qwen3.6-27B-AWQ", "local_llm"),
    # ("gpt-oss:120b-cloud", "ollama"),
    ("gpt-oss:20b-cloud", "ollama"),
    # ("gemma4:31b-cloud", "ollama"),
    ("gemma4:cloud", "ollama"),
    ("nemotron-3-nano:30b-cloud", "ollama"),
    # ("nemotron-3-super:cloud", "ollama"),
    # ("nemotron-3-ultra:cloud", "ollama"),
]
EXPERT_MODELS = [
    ("sonnet", "claude"), 
    # ("", "codex"),
    # ("QuantTrio/Qwen3.6-27B-AWQ", "local_llm"),
]

# ═══════════════════════════════════════════════════════════════════════════════
# TERMINATION BOUNDS
# ═══════════════════════════════════════════════════════════════════════════════
MAX_REWORK_ROUNDS = 3
MAX_REPLAN_ROUNDS = 2
MAX_WAVES = 20
MAX_PARALLEL_TASKS = 3

# A coding subagent's own turn can end one message short of the required status
# JSON (CODING_FRAME's {status, files_changed, ...}), because every vendor CLI
# ends the turn the moment it replies, tool call or not — small/local models in
# particular tend to narrate ("Now I need to update X") instead of emitting the
# final object. dispatch.py's coding-subagent call reads ENABLE_ below rather
# than hardcoding the guard on, so both knobs live in one place: ENABLE_ is
# whether a coding turn is held to that contract at all, and MAX_ATTEMPTS is,
# once held to it, how many times adapters.run_agent() may resume that same
# session — on whichever backend is doing the coding — and nudge it to finish
# before the narration is accepted as the turn's result.
ENABLE_CODING_AGENT_FINISH_GUARD = True
MAX_CODING_AGENT_CONTINUATION_ATTEMPTS = 5


# ═══════════════════════════════════════════════════════════════════════════════
# REQUIREMENTS Q&A
# ═══════════════════════════════════════════════════════════════════════════════
INTERACTIVE_REQUIREMENTS = False
MAX_CLARIFYING_QUESTIONS = 6


# ═══════════════════════════════════════════════════════════════════════════════
# WORKSPACE
# ═══════════════════════════════════════════════════════════════════════════════
USE_GIT_WORKTREES = True
WORKTREE_DIR_NAME = ".bhai-worktrees"
TASK_BRANCH_TEMPLATE = "bhai/{run}/{task}"
INTEGRATION_BRANCH_TEMPLATE = "bhai/{run}/integration"
# Seconds any single git command may take.
GIT_TIMEOUT_SECONDS = 120

# Where project-scoped shared memory and per-run audit records live: a sibling
# of the target repository, matching WORKTREE_DIR_NAME's own idiom. Not inside
# the target's working tree — a target project can already own a `runs/`
# directory of its own, and untracked state inside a repo does not survive
# ordinary Git hygiene (`git clean`, a fresh clone). See Decisions.md ADR-037.
ARTIFACT_DIR_NAME = ".bhai-artifacts"
# Absolute override for the rare case where the target's parent is not
# writable. Empty means "use the sibling default".
ARTIFACT_ROOT = os.getenv("BHAI_ARTIFACT_ROOT", "")


# ═══════════════════════════════════════════════════════════════════════════════
# GRAPH
# ═══════════════════════════════════════════════════════════════════════════════
RECURSION_LIMIT = 150
CHECKPOINT_DIR = BASE_DIR / "checkpoints"


# ═══════════════════════════════════════════════════════════════════════════════
# VENDOR ERROR STRINGS
# ═══════════════════════════════════════════════════════════════════════════════
RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "usage limit",
    "quota",
    "429",
    "too many requests",
    "insufficient_quota",
)
