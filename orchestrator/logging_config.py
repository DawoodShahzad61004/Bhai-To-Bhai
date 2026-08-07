"""One persistent DEBUG log per run, plus a readable console.

Three properties are load-bearing rather than stylistic, and all three were paid
for (docs/Research.md topic 14):

  * Written **during** the run, never buffered to exit, so a killed process still
    leaves evidence. Two runs' worth of debugging was lost to a dead pipe before
    this existed.
  * **Persisted per run**, so a defect can be found by reading afterwards rather
    than by catching it live. Bugs.md #15 — a pipeline that finished cleanly and
    produced nothing — was found this way and was invisible on the console.
  * Carrying a **per-run correlation id**, which is the minimum for reading work
    from several agents interleaved in one file. This pipeline has that by
    construction the moment a wave dispatches more than one coding subagent.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from config import CONSOLE_LOG_LEVEL, FILE_LOG_LEVEL, RUN_LOGS_DIR

_CONFIGURED_ATTR = "_orchestrator_logging_configured"
_LOG_FILE_ATTR = "_orchestrator_logging_file"

_run_id_var: ContextVar[str] = ContextVar("run_id", default="-")


def set_run_id(run_id: str) -> None:
    """Tag every subsequent log record in this context with `run_id`."""
    _run_id_var.set(run_id)


def get_run_id() -> str:
    return _run_id_var.get()


def get_logger(name: str) -> logging.Logger:
    """Module-level logger. Call setup_logging() once before these emit."""
    return logging.getLogger(name)


def current_log_file() -> Path | None:
    """This run's debug log, or None before setup_logging() has run.

    Lets a subprocess be pointed at the same file. An adapter that cannot get its
    child's diagnostics into the parent's audit trail has a hole exactly where
    the interesting failures live — Bugs.md #22 was undiagnosable until that hole
    was closed.
    """
    return getattr(logging.getLogger(), _LOG_FILE_ATTR, None)


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id_var.get()
        return True


class _DynamicStdoutHandler(logging.StreamHandler):
    """Follow sys.stdout so redirected runs still capture console logs."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


def setup_logging(
    log_dir: str | Path = RUN_LOGS_DIR,
    app_name: str = "orchestrator",
    console_level: int = CONSOLE_LOG_LEVEL,
) -> Path:
    """Configure logging once per process. Returns the debug log file path."""
    root_logger = logging.getLogger()
    if getattr(root_logger, _CONFIGURED_ATTR, False):
        return getattr(root_logger, _LOG_FILE_ATTR)

    resolved_log_dir = Path(log_dir)
    resolved_log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    run_id_filter = _RunIdFilter()

    console_handler = _DynamicStdoutHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%H:%M:%S")
    )
    console_handler.addFilter(run_id_filter)

    debug_file = resolved_log_dir / f"{app_name}_{timestamp}.debug.log"
    debug_handler = logging.FileHandler(debug_file, encoding="utf-8")
    debug_handler.setLevel(FILE_LOG_LEVEL)
    debug_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | [%(run_id)s] | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    debug_handler.addFilter(run_id_filter)

    root_logger.setLevel(FILE_LOG_LEVEL)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(debug_handler)

    setattr(root_logger, _CONFIGURED_ATTR, True)
    setattr(root_logger, _LOG_FILE_ATTR, debug_file)
    return debug_file
