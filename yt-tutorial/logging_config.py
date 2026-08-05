"""Central logging configuration.

Each run writes one persistent DEBUG file to config.RUN_LOGS_DIR, named
"<app>_<timestamp>.debug.log". The console stays at INFO so a run is readable
while the file keeps the full detail for afterwards.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from config import CONSOLE_LOG_LEVEL, FILE_LOG_LEVEL, RUN_LOGS_DIR

_CONFIGURED_ATTR = "_agent_logging_configured"
_LOG_FILE_ATTR = "_agent_logging_file"

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(request_id: str) -> None:
    """Tag every subsequent log record in this context with `request_id`."""
    _request_id_var.set(request_id)


def get_request_id() -> str:
    return _request_id_var.get()


def get_logger(name: str) -> logging.Logger:
    """Module-level logger. Call setup_logging() once before these emit."""
    return logging.getLogger(name)


class _RequestIdFilter(logging.Filter):
    """Inject the current request_id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        return True


class _DynamicStdoutHandler(logging.StreamHandler):
    """Follow sys.stdout so redirected runs still capture console logs."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


class _TracingHandler(logging.Handler):
    """Mirror records onto the active LangSmith run, if any.

    No-ops when LangSmith is absent or no run is active, so this stays cheap
    and harmless in a normal local run.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            from langsmith.run_helpers import get_current_run_tree

            run_tree = get_current_run_tree()
            if run_tree is not None:
                run_tree.add_event(
                    {
                        "name": record.levelname.lower(),
                        "time": datetime.now(timezone.utc).isoformat(),
                        "message": message,
                    }
                )
        except Exception:
            pass


def setup_logging(
    log_dir: str | Path = RUN_LOGS_DIR,
    app_name: str | None = None,
    console_level: int = CONSOLE_LOG_LEVEL,
) -> Path:
    """Configure logging once per process. Returns the debug log file path."""
    root_logger = logging.getLogger()
    if getattr(root_logger, _CONFIGURED_ATTR, False):
        return getattr(root_logger, _LOG_FILE_ATTR)

    resolved_log_dir = Path(log_dir)
    resolved_log_dir.mkdir(parents=True, exist_ok=True)
    resolved_app_name = app_name or Path(sys.argv[0]).stem or "app"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    request_id_filter = _RequestIdFilter()

    console_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    file_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | [%(request_id)s] | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = _DynamicStdoutHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(console_formatter)
    console_handler.addFilter(request_id_filter)

    debug_file = resolved_log_dir / f"{resolved_app_name}_{timestamp}.debug.log"
    debug_handler = logging.FileHandler(debug_file, encoding="utf-8")
    debug_handler.setLevel(FILE_LOG_LEVEL)
    debug_handler.setFormatter(file_formatter)
    debug_handler.addFilter(request_id_filter)

    tracing_handler = _TracingHandler()
    tracing_handler.setLevel(logging.INFO)
    tracing_handler.setFormatter(file_formatter)
    tracing_handler.addFilter(request_id_filter)

    root_logger.setLevel(FILE_LOG_LEVEL)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(debug_handler)
    root_logger.addHandler(tracing_handler)

    # HTTP client chatter at DEBUG buries the run; keep it to warnings.
    for noisy in ("httpx", "httpcore", "urllib3", "openai", "unstructured"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    setattr(root_logger, _CONFIGURED_ATTR, True)
    setattr(root_logger, _LOG_FILE_ATTR, debug_file)
    return debug_file
