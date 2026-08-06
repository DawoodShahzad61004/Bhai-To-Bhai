"""Central configuration.

Every tunable value lives here. Environment variables (loaded from .env) win
where a setting is deployment-specific; the literals below are the defaults.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
# Every document tool reads and writes inside this directory, and nowhere else.
WORKSPACE_DIR = BASE_DIR / os.getenv("WORKSPACE_DIR_NAME", "temp")
# Rendered mermaid PNGs of each compiled graph.
GRAPH_IMAGE_DIR = BASE_DIR / os.getenv("GRAPH_IMAGE_DIR_NAME", "graph_images")
# One .debug.log per run, kept for after-the-fact inspection.
RUN_LOGS_DIR = BASE_DIR / os.getenv("RUN_LOGS_DIR_NAME", "run_logs")

# ── Logging ───────────────────────────────────────────────────────────────────
# Console stays readable; the file keeps everything.
CONSOLE_LOG_LEVEL = logging.INFO
FILE_LOG_LEVEL = logging.DEBUG

# ── LLM provider ──────────────────────────────────────────────────────────────
# Local TGI / any OpenAI-compatible server.
CUSTOM_API_BASE = os.getenv("CUSTOM_API_BASE")
CUSTOM_API_KEY = os.getenv("CUSTOM_API_KEY")
CUSTOM_API_MODEL_NAME = os.getenv("CUSTOM_API_MODEL_NAME", "llama-3.1-8b-instruct")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL_NAME = os.getenv("GEN_MODEL_NAME", "llama-3.1-8b-instant")

# "custom" routes through CUSTOM_API_BASE, "groq" through the Groq API.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "custom")

LLM_TEMPERATURE = 0.1
LLM_MAX_TOKENS = 2048
# Near-greedy decoding on a small model falls into repetition loops, and inside a
# grammar-constrained tool argument the loop cannot end: it repeats the same URLs
# with the escaping doubling each pass until max_tokens runs out mid-string, which
# the server rejects as "EOF while parsing a string" after two minutes of work.
# Penalising by token count is what breaks the loop; temperature stays low so
# supervisor routing keeps its determinism.
LLM_FREQUENCY_PENALTY = 0.4
# 0 because invoker.py owns retry policy; the SDK retrying underneath would
# hide the 429s its FIFO gate and adaptive cooldown are built to handle.
LLM_MAX_RETRIES = 0

# TGI answers 500 for response_format json_schema/json_object, so structured
# output has to go through tool calling instead.
STRUCTURED_OUTPUT_METHOD = "function_calling"

# ── Rate limiting and retries (llm/invoker.py) ────────────────────────────────
LLM_RESPONSE_RETRY_LIMIT = 3
LLM_RATE_LIMIT_MAX_ATTEMPTS = 3
LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS = 1.0
LLM_RATE_LIMIT_BACKOFF_MAX_SECONDS = 30.0
LLM_RATE_LIMIT_MAX_DELAY_SECONDS = 1800.0
# Applied twice, because there are two paths to the model: invoker.py enforces it
# for supervisor calls, and it is handed to the client itself for worker calls,
# which go through create_agent and never reach invoker.py. Without the second,
# a stalled generation falls back to the OpenAI SDK default of 600s.
LLM_RESPONSE_TIMEOUT_SECONDS = 150
MIN_COOLDOWN_TIME = 0.0
MAX_COOLDOWN_TIME = 30.0

# ── Agent loop bounds ─────────────────────────────────────────────────────────
# Small models on TGI are grammar-constrained to emit a tool call on every turn
# while tools are attached, so they never produce a final answer on their own.
# These two caps are what actually guarantee termination.
#
# Tool calls one worker may make per invocation before further calls are blocked.
# The worker keeps its turn to report after this, so leave room for the tools it
# genuinely needs -- a worker that spends the whole budget searching still gets to
# write up what it found.
TOOL_CALL_RUN_LIMIT = 3
# Times a supervisor may route to the same worker before forcing FINISH.
MAX_WORKER_REPORTS = 2
# When a worker never writes a report, its tool output is forwarded instead. Cap
# per result, because a scraped page would otherwise bury the supervisor.
TOOL_RESULT_SALVAGE_CHARS = 700
# A failed worker reports the error to its supervisor instead of aborting the run.
# Provider errors quote the whole rejected generation, so keep only the head.
WORKER_ERROR_REPORT_CHARS = 300
# Wall-clock ceiling on one worker turn, covering all of its model calls and tool
# calls together. LLM_RESPONSE_TIMEOUT_SECONDS cannot do this job: it is a
# per-read timeout, and a server that holds the socket open while it generates
# never trips it -- one turn here ran 388s before the server answered at all.
# Normal turns finish in under 40s, so this only catches the pathological ones.
WORKER_DEADLINE_SECONDS = 180

# ── Graph recursion budgets ───────────────────────────────────────────────────
# Sub-graphs inherit the parent config unless given their own, so a team would
# otherwise draw down the orchestrator's budget.
SUPER_GRAPH_RECURSION_LIMIT = 50
TEAM_RECURSION_LIMIT = 25

# ── Tools ─────────────────────────────────────────────────────────────────────
TAVILY_MAX_RESULTS = 5

# ── Team membership ───────────────────────────────────────────────────────────
RESEARCH_TEAM_MEMBERS = ["search", "web_scraper"]
WRITING_TEAM_MEMBERS = ["doc_writer", "note_taker", "chart_generator"]
TOP_LEVEL_TEAMS = ["research_team", "writing_team"]
