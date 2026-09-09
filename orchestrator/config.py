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
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# STAGE TOGGLES  —  the two switches the pipeline is built around
# ═══════════════════════════════════════════════════════════════════════════════
ENABLE_REVIEWER = True
ENABLE_SUPERVISOR = True

ENABLE_AGENT_DIAGNOSTICS = False
AGENT_DIAGNOSTIC_MAX_PARALLEL = 3


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

# Sandbox mode Codex enforces on every turn (`codex exec --sandbox <value>`).
# Options: "read-only" | "workspace-write" | "danger-full-access"
CODEX_SANDBOX = "danger-full-access"

# Approval policy Codex enforces on every turn (`codex exec --approval-policy <value>`).
# Options: "untrusted" | "on-request" | "on-failure" | "never"
CODEX_APPROVAL_POLICY = "untrusted"

GEMINI_APPROVAL_MODE = "yolo"


CUSTOM_API_BASE = os.getenv("CUSTOM_API_BASE", "")
CUSTOM_API_KEY = os.getenv("CUSTOM_API_KEY", "")
CUSTOM_API_MODEL_NAME = "Qwen/Qwen2.5-7B-Instruct"
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
    "requirements": AgentSpec(backend="gemini", model="gemini-3.1-flash-lite", deadline_seconds=900,),
    "wave_orchestrator": AgentSpec(backend="gemini", model="gemini-3.1-flash-lite", deadline_seconds=900,),
    "merger": AgentSpec(backend="gemini", model="gemini-3.1-flash-lite", deadline_seconds=900,),
    # ── Stronger model: judgment work ────────────────────────────────────────
    "planner": AgentSpec(backend="codex", model="", deadline_seconds=600,),
    "reviewer": AgentSpec(backend="codex", model="", deadline_seconds=600,),
    "supervisor": AgentSpec(backend="codex", model="", deadline_seconds=600,),
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

SMALL_MODELS = [
    ("qwen3.5:4b", "ollama"),
    # ("qwen3:8b", "ollama"),
    # ("gemini-3.1-flash-lite", "gemini"),
]

MEDIUM_MODELS = [
    # ("gpt-oss:20b-cloud", "ollama"),
    # ("nemotron-3-nano:30b-cloud", "ollama"),
    ("QuantTrio/Qwen3.6-27B-AWQ", "local_llm"),
    # ("gemma4:31b-cloud", "ollama"),
    # ("haiku", "claude"),
    ("auto", "copilot"),
]

EXPERT_MODELS = [
    # ("gpt-oss:120b-cloud", "ollama"),
    # ("nemotron-3-super:cloud", "ollama"),
    # ("nemotron-3-ultra:cloud", "ollama"),
    # ("sonnet", "claude"),
    ("", "codex"),
]

# ═══════════════════════════════════════════════════════════════════════════════
# TERMINATION BOUNDS
# ═══════════════════════════════════════════════════════════════════════════════
MAX_REWORK_ROUNDS = 3
MAX_REPLAN_ROUNDS = 2
MAX_WAVES = 20
MAX_PARALLEL_TASKS = 3

ENABLE_CODING_AGENT_FINISH_GUARD = True
MAX_CODING_AGENT_CONTINUATION_ATTEMPTS = 5


# ═══════════════════════════════════════════════════════════════════════════════
# REQUIREMENTS Q&A
# ═══════════════════════════════════════════════════════════════════════════════
INTERACTIVE_REQUIREMENTS = True
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

ARTIFACT_DIR_NAME = ".bhai-artifacts"
# Empty means "use the sibling default".
ARTIFACT_ROOT = ""


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


# ═══════════════════════════════════════════════════════════════════════════════
# MEM_MANAGER CONFIGURATION  —  Memory consolidation and deduplication
# ═══════════════════════════════════════════════════════════════════════════════

# --- Episodic markdown parsing -----------------------------------------------
EPISODIC_BLOCK_SPLIT_PATTERN = re.compile(r"\n(?=## )")
# Header shape: 'DATE TIME SEP TAG...', e.g. '2026-08-29 12:48:55Z - requirements'.
EPISODIC_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%SZ"

# user_choices.md: '## Run `run-20260829-174814` — 2026-08-29 12:48:55Z'. 
# Captures (run_id, date, time);
USER_CHOICES_HEADER_PATTERN = re.compile(
    r"^## Run `([^`]+)`.*?(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}Z)"
)

# --- Session identity --------------------------------------------------------
# The identity a memory record is scored under is the agent-backend session that
# wrote it, not a noun scraped out of its prose. A regex over the text made
# importance a function of writing style - an entry mentioning `npm ci` and
# package.json outscored one saying the same thing in plain words.
#
# The writer stamps the marker on the line directly AFTER the '## ' header,
# never before it: EPISODIC_BLOCK_SPLIT_PATTERN splits on a lookahead, so a line
# above a header belongs to the PREVIOUS block (which is exactly what already
# happens to append_user_choices' '<!-- run:ID -->' marker).
SESSION_MARKER_TEMPLATE = "<!-- session:{session} -->"
# One pattern, two uses: .match() against a block body (anchored at position 0
# regardless of MULTILINE, so a marker quoted mid-finding stays content) and
# .sub() to strip markers out of text shown to a peer agent.
SESSION_MARKER_PATTERN = re.compile(
    r"^[ \t]*<!--[ \t]*session:(\S+)[ \t]*-->[ \t]*\n?", re.MULTILINE
)
# The backend prefix is what stops two vendors' id spaces colliding in one
# shared log; a session id is only unique within its own vendor. `\S+` above
# assumes the id carries no whitespace - true of every adapter's ids, which are
# UUID- or hex-shaped.
SESSION_KEY_TEMPLATE = "{backend}:{session_id}"

# --- Composite importance ----------------------------------------------------
IMPORTANCE_WEIGHTS = {
    "recency": 0.25,
    "frequency": 0.25,
    "surprise": 0.20,
    "session": 0.15,
    "outcome": 0.15,
}
# Principle 6 (explicit choices outrank inferred preferences)
EXPLICIT_PROVENANCE_BOOST = 0.2

RECENCY_HALF_LIFE_HOURS = 168.0
FREQUENCY_SIMILARITY_THRESHOLD = 0.6
DEFAULT_SUCCESS_MARKERS = ("passed", "success", "resolved", "fixed", "works")
DEFAULT_FAILURE_MARKERS = ("failed", "error", "not recognized", "exit 1", "traceback")

# --- Passive decay  ------------------------------------------------------------
DECAY_LAMBDA_PER_HOUR = 0.001

# --- Embeddings / dedup-merge --------------------------------------------------
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_ENCODING_TIMEOUT_SECONDS = 30.0
MERGE_SIMILARITY_THRESHOLD = 0.60

JUDGE_MODEL_NAME = CUSTOM_API_MODEL_NAME

MERGE_LLM_TEMPERATURE = 0.1
MERGE_LLM_MAX_TOKENS = 2048
JUDGE_LLM_TEMPERATURE = 0.0
JUDGE_LLM_MAX_TOKENS = 1024
LLM_MAX_RETRIES = 0

LLM_RATE_LIMIT_MAX_ATTEMPTS = 5
LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS = 2.0
LLM_RATE_LIMIT_BACKOFF_MAX_SECONDS = 60.0
LLM_RATE_LIMIT_MAX_DELAY_SECONDS = 120.0
LLM_RESPONSE_TIMEOUT_SECONDS = 60.0
MIN_COOLDOWN_TIME = 0.0
MAX_COOLDOWN_TIME = 30.0

LLM_REACHABILITY_TIMEOUT_SECONDS = 5.0

MERGE_LLM_ENABLED = True
MERGE_VALIDATION_ENABLED = True

# --- Consolidation / pruning ---------------------------------------------------
PRUNE_BOTTOM_PERCENT = 0.20
ENABLE_PRUNING = True
MIN_PRUNE_BUDGET = 1_000

# Final durable-memory tags, numbered chronologically within each file.
# Files not listed here retain their source entry tags.
DURABLE_MEMORY_TAG_PREFIXES = {
    "learnings.md": "Learning",
    "user_choices.md": "User Choice",
}

# ═══════════════════════════════════════════════════════════════════════════════
# COMPACT COMMAND CONFIGURATION  —  Episodic memory artifacts to consolidate
# ═══════════════════════════════════════════════════════════════════════════════
COMPACT_ARTIFACT_FILES = [
    Path.home() / "Desktop" / "Projects" / ".bhai-artifacts" / "temp_work_repo-55fce4bb" / "shared" / "user_choices.md",
    Path.home() / "Desktop" / "Projects" / ".bhai-artifacts" / "temp_work_repo-55fce4bb" / "shared" / "learnings.md",
]
