"""Central logging configuration for the RAG application."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "run_logs"
_CONFIGURED_ATTR = "_orch_logging_configured"

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(request_id: str) -> None:
    _request_id_var.set(request_id)


def get_request_id() -> str:
    return _request_id_var.get()


class _RequestIdFilter(logging.Filter):
    """Inject the current request_id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        return True


class _DynamicStdoutHandler(logging.StreamHandler):
    """Follow sys.stdout so batch-run tee streams still capture console logs."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


class _TracingHandler(logging.Handler):
    """Mirror log records onto the active LangSmith run and Phoenix/OTel span, if any.

    No-ops when neither tracer has an active run/span, so this stays cheap and
    harmless when tracing isn't running (e.g. LangSmith disabled, no batch job).
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            return

        event = {
            "name": record.levelname.lower(),
            "time": datetime.now(timezone.utc).isoformat(),
            "message": message,
        }

        run_tree = None
        try:
            from langsmith.run_helpers import get_current_run_tree

            run_tree = get_current_run_tree()
            if run_tree is not None:
                run_tree.add_event(event)
        except Exception:
            pass

        # Phoenix's LangChain integration (OpenInferenceTracer) tracks spans keyed by
        # LangChain run_id in its own registry rather than via OTel's ambient current-span
        # context, so get_current_span() never sees them. Look the span up by run_id instead,
        # reusing the run_id LangSmith's run_tree already gave us above.
        try:
            if run_tree is not None:
                from openinference.instrumentation.langchain import LangChainInstrumentor

                tracer = LangChainInstrumentor()._tracer
                span = tracer.get_span(run_tree.id) if tracer is not None else None
                if span is not None and span.is_recording():
                    span.add_event(
                        message,
                        attributes={
                            "log.level": record.levelname,
                            "log.logger": record.name,
                            "log.request_id": getattr(record, "request_id", "-"),
                        },
                    )
        except Exception:
            pass


def setup_logging(
    log_dir: str | Path = DEFAULT_LOG_DIR,
    app_name: str | None = None,
    console_level: int = logging.INFO,
) -> None:
    """Configure application logging once for the current process."""
    root_logger = logging.getLogger()
    if getattr(root_logger, _CONFIGURED_ATTR, False):
        return

    resolved_log_dir = Path(log_dir)
    resolved_log_dir.mkdir(parents=True, exist_ok=True)
    resolved_app_name = app_name or Path(sys.argv[0]).stem or "app"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    request_id_filter = _RequestIdFilter()

    console_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | [%(request_id)s] | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
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
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(file_formatter)
    debug_handler.addFilter(request_id_filter)

    try:
        import importlib as _importlib
        _tt_mod = _importlib.import_module("app_workflow.services.timing_tracker")
        _tt_mod.timing_tracker.set_json_path(debug_file.with_suffix(".json"))
    except Exception:
        pass

    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(debug_handler)
    root_logger.addHandler(tracing_handler)
    root_logger.addHandler(langfuse_handler)

    # Diagnostic loggers propagate to root so their output reaches both console and debug file
    for logger_name in ("llm_data_check", "llm_json_tries", "llm_io"):
        diagnostic_logger = logging.getLogger(logger_name)
        diagnostic_logger.setLevel(logging.DEBUG)
        diagnostic_logger.propagate = True

    setattr(root_logger, _CONFIGURED_ATTR, True)


# --- chunk-run output --------------------------------------------------------
#
# Moved here from the standalone ingestion logging module so the whole
# app_workflow stack shares one logging/output home. This writes a single
# human-readable Markdown file listing every chunk produced by an ingestion
# run, together with per-run token statistics — handy for eyeballing how a
# splitter behaved without loading anything into the vector store.


class Chunk(Protocol):
    """The subset of a LangChain Document needed by the run writer."""

    page_content: str
    metadata: dict


_TOKEN_ENCODER = None


def _token_encoder():
    """Lazily build (and cache) the tiktoken encoder named in config."""
    global _TOKEN_ENCODER
    if _TOKEN_ENCODER is None:
        import tiktoken

        from app_workflow.config import TOKEN_ENCODING

        _TOKEN_ENCODER = tiktoken.get_encoding(TOKEN_ENCODING)
    return _TOKEN_ENCODER


def write_chunk_run(
    chunks: Iterable[Chunk],
    run_dir: str | Path | None = None,
) -> Path:
    """Write one human-readable file containing all chunks from this run."""
    if run_dir is None:
        from app_workflow.config import CHUNK_RUNS_DIR

        run_dir = CHUNK_RUNS_DIR

    encoder = _token_encoder()
    chunk_list = list(chunks)
    output_dir = Path(run_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_path = output_dir / f"chunks_{timestamp}.md"

    prepared_chunks = [
        (
            chunk,
            chunk.page_content.strip(),
            len(encoder.encode(chunk.page_content.strip())),
        )
        for chunk in chunk_list
    ]
    average_tokens = (
        sum(token_count for _, _, token_count in prepared_chunks) / len(prepared_chunks)
        if prepared_chunks
        else 0.0
    )

    sections = [
        "# Chunking Run",
        "",
        f"- Generated: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- Total chunks: {len(chunk_list)}",
        f"- Average chunk tokens: {average_tokens:.2f}",
    ]

    if prepared_chunks:
        # Chunk numbers below are 1-based, matching the "## Chunk N" headings.
        largest_index, (_, largest_content, largest_tokens) = max(
            enumerate(prepared_chunks, start=1),
            key=lambda item: item[1][2],
        )
        sections.extend(
            [
                f"- Largest chunk (tokens): Chunk {largest_index}",
                f"- Largest chunk tokens: {largest_tokens}",
                f"- Largest chunk characters: {len(largest_content)}",
            ]
        )

    for index, (chunk, content, token_count) in enumerate(prepared_chunks, start=1):
        source = chunk.metadata.get("source", "unknown")
        sequence = chunk.metadata.get("chunk_seq", index - 1)
        backtick_runs = re.findall(r"`+", content)
        fence = "`" * max(3, 1 + max((len(run) for run in backtick_runs), default=0))
        sections.extend(
            [
                "",
                "---",
                "",
                f"## Chunk {index}",
                "",
                f"- Source: `{source}`",
                f"- Source chunk: {sequence}",
                f"- Characters: {len(content)}",
                f"- Tokens: {token_count}",
                "",
                f"{fence}text",
                content,
                fence,
            ]
        )

    output_path.write_text("\n".join(sections) + "\n", encoding="utf-8")
    return output_path
