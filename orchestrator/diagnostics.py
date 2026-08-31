"""Live availability checks for every configured backend/model pair.

The diagnostic deliberately exercises the same adapter boundary as a real run.
A version probe can prove that a CLI exists; it cannot prove that credentials,
the selected model, the harness/provider route, and structured replies work
together.  Each unique configured pair therefore receives one minimal real
turn, with no file or shell tools requested.

Every probe writes both sides of its exchange — what was sent, and what came
back verbatim — to the run's debug log.  A one-line verdict is enough to know
*that* a pair is broken; it is never enough to know why.  "Successful turn
violated the diagnostic JSON contract" describes a dozen different faults, and
which one it was is only ever visible in the reply the model actually sent.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
import contextvars
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Sequence

import adapters
import config
from logging_config import get_logger, setup_logging
import parsing


logger = get_logger(__name__)

# A diagnostic reply is one small JSON object, but a *failing* one can be a
# whole vendor stack trace or a verbose model monologue.  Same head/tail budget
# the adapters use, for the same reason: the log stays readable and still shows
# both ends of anything oversized.
_DEBUG_BLOCK_LIMIT = 12000


def _debug_block(text: str) -> str:
    if len(text) <= _DEBUG_BLOCK_LIMIT:
        return text
    head = text[:4000]
    tail = text[-4000:]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n... [{omitted} chars omitted] ...\n{tail}"


DIAGNOSTIC_PAYLOAD = {"diagnostic": "ok"}
DIAGNOSTIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"diagnostic": {"const": "ok"}},
    "required": ["diagnostic"],
    "additionalProperties": False,
}
DIAGNOSTIC_PROMPT = (
    "This is a backend availability diagnostic. Do not inspect or modify files "
    "and do not call tools. Reply with exactly one JSON object and nothing else: "
    '{"diagnostic":"ok"}'
)


@dataclass(frozen=True)
class DiagnosticTarget:
    """One deduplicated invocation/backend/model combination to exercise."""

    invocation: str
    backend: str
    model: str
    deadline_seconds: int
    sources: tuple[str, ...]

    @property
    def adapter_key(self) -> str:
        return f"direct:{self.backend}" if self.invocation == "direct" else self.invocation

    @property
    def harness(self) -> str:
        if self.invocation != "direct":
            return self.invocation
        if self.backend in ("ollama", "local_llm"):
            return "codex"
        return self.backend

    @property
    def tag(self) -> str:
        """How this probe names itself to the adapter and in the log.

        The same string on both sides is what lets one pair's dispatch, prompt,
        reply and verdict be read together in a file where every probe is
        interleaved with every other.
        """
        return f"diagnostic-{_safe_fragment(self.backend)}-{_safe_fragment(self.model)}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "invocation": self.invocation,
            "adapter": self.adapter_key,
            "harness": self.harness,
            "backend": self.backend,
            "model": self.model,
            "deadline_seconds": self.deadline_seconds,
            "sources": list(self.sources),
        }


@dataclass(frozen=True)
class DiagnosticResult:
    """The normalized outcome of one live target probe."""

    target: DiagnosticTarget
    passed: bool
    duration_seconds: float
    error_kind: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.target.as_dict(),
            "passed": self.passed,
            "duration_seconds": self.duration_seconds,
            "error_kind": self.error_kind,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DiagnosticReport:
    """All configured probes, retained in deterministic configuration order."""

    results: tuple[DiagnosticResult, ...]
    max_parallel: int

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(result.passed for result in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checked": len(self.results),
            "max_parallel": self.max_parallel,
            "results": [result.as_dict() for result in self.results],
        }


def _positive_deadline(value: Any, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{source}.deadline_seconds must be a positive integer")
    return value


def _spec_values(spec: Any, source: str) -> tuple[str, str, int]:
    try:
        backend = spec.backend
        model = spec.model
        deadline = spec.deadline_seconds
    except AttributeError as exc:
        raise ValueError(f"{source} must be an AgentSpec-like value") from exc
    if not isinstance(backend, str) or not backend:
        raise ValueError(f"{source}.backend must be a non-empty string")
    if not isinstance(model, str):
        raise ValueError(f"{source}.model must be a string")
    return backend, model, _positive_deadline(deadline, source)


def configured_targets() -> tuple[DiagnosticTarget, ...]:
    """Collect and deduplicate all active pairs exposed by ``config.py``.

    Fixed stage/fallback entries contribute their own deadlines.  Menu entries
    are potential coding agents and inherit the smaller fallback-coding deadline,
    matching the runtime's use of the fallback coding-agent deadline for a
    planner-selected roster.  Repeated pairs keep every source and the smallest
    applicable deadline.
    """
    invocation = config.INVOCATION
    if not isinstance(invocation, str) or not invocation:
        raise ValueError("INVOCATION must be a non-empty string")

    agents = config.AGENTS
    if not isinstance(agents, dict):
        raise ValueError("AGENTS must be a dictionary")

    entries: list[tuple[str, str, int, str]] = []
    for name, spec in agents.items():
        source = f"AGENTS[{name!r}]"
        backend, model, deadline = _spec_values(spec, source)
        entries.append((backend, model, deadline, source))

    fallback_specs: list[tuple[str, Any]] = [
        ("CODING_AGENT_A", config.CODING_AGENT_A),
        ("CODING_AGENT_B", config.CODING_AGENT_B),
    ]
    fallback_deadlines: list[int] = []
    for source, spec in fallback_specs:
        backend, model, deadline = _spec_values(spec, source)
        entries.append((backend, model, deadline, source))
        fallback_deadlines.append(deadline)
    menu_deadline = min(fallback_deadlines)

    for menu_name in ("SMALL_MODELS", "MEDIUM_MODELS", "EXPERT_MODELS"):
        menu = getattr(config, menu_name)
        if not isinstance(menu, (list, tuple)):
            raise ValueError(f"{menu_name} must be a list or tuple")
        for index, pair in enumerate(menu):
            source = f"{menu_name}[{index}]"
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError(f"{source} must be a (model, backend) pair")
            model, backend = pair
            if not isinstance(model, str):
                raise ValueError(f"{source} model must be a string")
            if not isinstance(backend, str) or not backend:
                raise ValueError(f"{source} backend must be a non-empty string")
            entries.append((backend, model, menu_deadline, source))

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for backend, model, deadline, source in entries:
        key = (backend, model)
        if key not in grouped:
            grouped[key] = {"deadline": deadline, "sources": [source]}
            continue
        grouped[key]["deadline"] = min(grouped[key]["deadline"], deadline)
        grouped[key]["sources"].append(source)

    if not grouped:
        raise ValueError("config.py contains no active backend/model pairs")

    return tuple(
        DiagnosticTarget(
            invocation=invocation,
            backend=backend,
            model=model,
            deadline_seconds=record["deadline"],
            sources=tuple(record["sources"]),
        )
        for (backend, model), record in grouped.items()
    )


def _safe_fragment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return (cleaned or "default")[:48]


def _log_input(target: DiagnosticTarget, cwd: Path) -> None:
    """Record everything the probe is about to send, before it sends it.

    Written first rather than alongside the reply, so a pair that hangs until
    its deadline still leaves behind what it was asked.
    """
    logger.debug(
        "[%s] input | adapter=%s | harness=%s | model=%s | deadline=%ds | cwd=%s | sources=%s",
        target.tag,
        target.adapter_key,
        target.harness,
        target.model or "(cli default)",
        target.deadline_seconds,
        cwd,
        ", ".join(target.sources),
    )
    logger.debug("[%s] input prompt:\n%s", target.tag, _debug_block(DIAGNOSTIC_PROMPT))
    logger.debug(
        "[%s] input schema: %s",
        target.tag,
        json.dumps(DIAGNOSTIC_SCHEMA, ensure_ascii=False, sort_keys=True),
    )


def _log_output(target: DiagnosticTarget, result: adapters.AgentResult) -> None:
    """Record the turn exactly as the adapter handed it back.

    Both channels, because either one alone can be the whole story: a backend
    that honours structured output leaves `text` empty, and a backend that
    ignores the schema puts its answer — or its refusal — only in `text`.
    """
    logger.debug("[%s] output | %s", target.tag, result.summary())
    logger.debug("[%s] output text:\n%s", target.tag, _debug_block(result.text))
    if result.structured is not None:
        logger.debug(
            "[%s] output structured:\n%s",
            target.tag,
            _debug_block(json.dumps(result.structured, ensure_ascii=False, sort_keys=True)),
        )


def _log_verdict(result: DiagnosticResult) -> None:
    """Close the probe's block with the line the report will show."""
    logger.debug(
        "[%s] verdict | %s | %.1fs/%ds | %s",
        result.target.tag,
        "PASS" if result.passed else f"FAIL {result.error_kind}",
        result.duration_seconds,
        result.target.deadline_seconds,
        result.detail,
    )


def _probe(target: DiagnosticTarget, cwd: Path) -> DiagnosticResult:
    """Exercise one target, with both sides of the exchange in the debug log."""
    _log_input(target, cwd)
    result = _attempt(target, cwd)
    _log_verdict(result)
    return result


def _attempt(target: DiagnosticTarget, cwd: Path) -> DiagnosticResult:
    spec = config.AgentSpec(
        backend=target.backend,
        model=target.model,
        deadline_seconds=target.deadline_seconds,
    )
    started = time.perf_counter()
    try:
        result = adapters.run_agent(
            DIAGNOSTIC_PROMPT,
            spec=spec,
            cwd=str(cwd),
            tag=target.tag,
            tools=(),
            json_schema=DIAGNOSTIC_SCHEMA,
            invocation=target.invocation,
        )
    except Exception as exc:  # the adapter boundary promises not to raise; keep auditing if it does
        logger.debug(
            "[%s] output raised %s: %s", target.tag, type(exc).__name__, exc, exc_info=True
        )
        return DiagnosticResult(
            target=target,
            passed=False,
            duration_seconds=time.perf_counter() - started,
            error_kind="agent_error",
            detail=f"diagnostic invocation raised {type(exc).__name__}: {exc}",
        )

    duration = time.perf_counter() - started
    _log_output(target, result)
    if not result.ok:
        return DiagnosticResult(
            target=target,
            passed=False,
            duration_seconds=duration,
            error_kind=result.error_kind or "agent_error",
            detail=result.error_message or "the adapter reported an unspecified failure",
        )

    parsed = parsing.extract_json(result.text, result.structured)
    if not parsed.ok:
        return DiagnosticResult(
            target=target,
            passed=False,
            duration_seconds=duration,
            error_kind="agent_error",
            detail=f"successful turn violated the diagnostic JSON contract: {parsed.error}",
        )
    if parsed.value != DIAGNOSTIC_PAYLOAD:
        returned = json.dumps(parsed.value, ensure_ascii=False, sort_keys=True)
        return DiagnosticResult(
            target=target,
            passed=False,
            duration_seconds=duration,
            error_kind="agent_error",
            detail=f"successful turn returned unexpected diagnostic payload: {returned[:300]}",
        )

    return DiagnosticResult(
        target=target,
        passed=True,
        duration_seconds=duration,
        detail="backend, model, harness, and structured response are available",
    )


def run_diagnostics(
    targets: Sequence[DiagnosticTarget],
    *,
    max_parallel: int,
    workspace_root: str | Path | None = None,
) -> DiagnosticReport:
    """Probe every target exactly once with bounded concurrency."""
    if not targets:
        raise ValueError("at least one diagnostic target is required")
    if isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or max_parallel <= 0:
        raise ValueError("AGENT_DIAGNOSTIC_MAX_PARALLEL must be a positive integer")

    if workspace_root is None:
        workspace_context = tempfile.TemporaryDirectory(prefix="bhai-agent-diagnostics-")
    else:
        root = Path(workspace_root)
        root.mkdir(parents=True, exist_ok=True)
        workspace_context = nullcontext(str(root))

    workers = min(max_parallel, len(targets))
    with workspace_context as workspace:
        root = Path(workspace)
        workspaces: list[Path] = []
        for index, target in enumerate(targets):
            path = root / (
                f"{index:02d}-{_safe_fragment(target.backend)}-"
                f"{_safe_fragment(target.model)}"
            )
            path.mkdir(parents=True, exist_ok=True)
            workspaces.append(path)

        ordered: list[DiagnosticResult | None] = [None] * len(targets)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent-diagnostic") as pool:
            # copy_context() carries the run's correlation id into each thread,
            # the same way dispatch.py does for concurrent coding subagents. A
            # ContextVar does not cross a thread boundary on its own, and every
            # probe here logs from a worker (ADR-013).
            futures = {
                pool.submit(contextvars.copy_context().run, _probe, target, workspaces[index]): index
                for index, target in enumerate(targets)
            }
            for future in as_completed(futures):
                ordered[futures[future]] = future.result()

    return DiagnosticReport(
        results=tuple(result for result in ordered if result is not None),
        max_parallel=workers,
    )


def run_configured_diagnostics() -> DiagnosticReport:
    return run_diagnostics(
        configured_targets(),
        max_parallel=config.AGENT_DIAGNOSTIC_MAX_PARALLEL,
    )


def render_human(report: DiagnosticReport) -> str:
    lines = ["Bhai-To-Bhai agent diagnostics", "=" * 31]
    for result in report.results:
        target = result.target
        status = "PASS" if result.passed else "FAIL"
        model = target.model or "(cli default)"
        lines.append(
            f"[{status}] {target.adapter_key} | harness={target.harness} | model={model} | "
            f"{result.duration_seconds:.1f}s/{target.deadline_seconds}s"
        )
        lines.append(f"       sources: {', '.join(target.sources)}")
        if not result.passed:
            lines.append(f"       {result.error_kind}: {result.detail}")

    passed = sum(result.passed for result in report.results)
    lines.append("-" * 31)
    lines.append(
        f"Diagnostics {'passed' if report.passed else 'failed'}: "
        f"{passed}/{len(report.results)} target(s) passed."
    )
    return "\n".join(lines)


def render_json(report: DiagnosticReport) -> str:
    return json.dumps(report.as_dict(), indent=2, ensure_ascii=False)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exercise every active backend/model pair configured in orchestrator/config.py."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print one machine-readable JSON report instead of the human summary.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    # Standalone, nothing else has configured logging, so the exchange would go
    # nowhere. `--json` is a machine channel and the console handler shares its
    # stdout, so that mode keeps the console silent and the file complete.
    log_file = setup_logging(
        app_name="diagnostics",
        console_level=logging.CRITICAL if args.json else config.CONSOLE_LOG_LEVEL,
    )
    try:
        report = run_configured_diagnostics()
    except ValueError as exc:
        if args.json:
            print(json.dumps({"passed": False, "configuration_error": str(exc)}, indent=2))
        else:
            print("Bhai-To-Bhai agent diagnostics")
            print("=" * 31)
            print(f"[FAIL] configuration: {exc}")
        return 1

    if args.json:
        print(render_json(report))
    else:
        print(render_human(report))
        print(f"Prompt and reply for every target: {log_file}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
