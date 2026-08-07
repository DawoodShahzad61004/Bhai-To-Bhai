"""The one way into the pipeline.

    python orchestrator/main.py --goal "add an OAuth2 refresh flow" --target ../MyApp

Everything else in this package is a library. This module owns exactly four
things: parsing arguments, preparing the run, driving the graph (including the
requirements pause), and reporting what happened.

Run it with `--dry-run` to see the graph and the effective configuration without
starting anything, or with `--resume <run-id>` to pick up a paused or crashed run
from its checkpoint.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# The package uses bare imports (`from config import ...`), matching how it
# resolves at run time. This makes that true when main.py is run as a script from
# anywhere — docs/Bugs.md #24 is a directory rename that silently disabled a
# whole sandbox through exactly this.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifacts as art  # noqa: E402
import config  # noqa: E402
import worktrees as wt  # noqa: E402
from graph import compile_graph  # noqa: E402
from logging_config import get_logger, set_run_id, setup_logging  # noqa: E402
from state import initial_state  # noqa: E402

logger = get_logger(__name__)


# ── Argument parsing ─────────────────────────────────────────────────────────


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="Run the six-agent pipeline against a target repository.",
    )
    parser.add_argument("--goal", help="What you want built, in one or two sentences.")
    parser.add_argument(
        "--target",
        help="Path to the repository to work on. Must not be this repository.",
    )
    parser.add_argument("--run-id", help="Name this run. Defaults to a timestamp.")
    parser.add_argument(
        "--resume",
        metavar="RUN_ID",
        help="Continue a paused or crashed run from its checkpoint.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the graph and the effective configuration, then exit.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not pause for clarifying questions (same as INTERACTIVE_REQUIREMENTS=0).",
    )
    return parser.parse_args(argv)


def validate_target(target: str) -> tuple[Path | None, str]:
    """Resolve and check the target repository. Returns (path, error)."""
    path = Path(target).expanduser().resolve()

    if not path.is_dir():
        return None, f"{path} is not a directory."

    if path == config.PROJECT_ROOT:
        # Agents in this pipeline write code, run git, and merge branches. Aimed
        # at this repository they would be editing the orchestrator that is
        # driving them, mid-run.
        return None, (
            f"{path} is the orchestrator's own repository. Point --target at the "
            "project you want built."
        )

    if config.USE_GIT_WORKTREES and not wt.is_git_repo(path):
        return None, (
            f"{path} is not a git repository, and USE_GIT_WORKTREES is on. Either "
            "run `git init` there, or set USE_GIT_WORKTREES=False in config.py to "
            "run the wave in a shared checkout."
        )

    return path, ""


# ── Reporting ────────────────────────────────────────────────────────────────


def describe_config() -> str:
    lines = [
        "Configuration",
        "-------------",
        f"  transport         {config.INVOCATION}",
        f"  reviewer          {'ON' if config.ENABLE_REVIEWER else 'OFF'}",
        f"  supervisor        {'ON' if config.ENABLE_SUPERVISOR else 'OFF'}",
        f"  interactive Q&A   {'ON' if config.INTERACTIVE_REQUIREMENTS else 'OFF'}",
        f"  git worktrees     {'ON' if config.USE_GIT_WORKTREES else 'OFF'}",
        f"  parallel tasks    {config.MAX_PARALLEL_TASKS}",
        f"  rework rounds     {config.MAX_REWORK_ROUNDS} per wave",
        f"  replan rounds     {config.MAX_REPLAN_ROUNDS}",
        "",
        "Agents",
        "------",
    ]
    for name, spec in config.AGENTS.items():
        lines.append(
            f"  {name:<18}{spec.backend}/{spec.model or '(cli default)':<16}"
            f"deadline {spec.deadline_seconds}s"
        )
    lines.append(
        f"  {'coding subagent':<18}{config.CODING_AGENT.backend}/"
        f"{config.CODING_AGENT.model or '(cli default)':<16}"
        f"deadline {config.CODING_AGENT.deadline_seconds}s"
    )
    return "\n".join(lines)


def render_graph() -> str:
    """The graph as text, for `--dry-run`.

    ASCII first because it is readable in a terminal, Mermaid as the fallback
    because it needs no extra package at all — `draw_ascii` wants `grandalf`,
    which is a development convenience and not worth a runtime dependency.
    """
    graph = compile_graph().get_graph()
    try:
        return graph.draw_ascii()
    except Exception:  # pragma: no cover - depends on what is installed
        try:
            return (
                "Graph (Mermaid — paste into https://mermaid.live):\n\n"
                + graph.draw_mermaid()
            )
        except Exception as exc:
            return f"(could not render the graph: {exc})"


def ask(questions: list[str], understanding: str) -> list[str]:
    """Put the requirements agent's questions to the user.

    An empty answer is allowed and is recorded as unanswered rather than as an
    empty decision — `user_choices.md` holds only what the user actually said.
    """
    print("\n" + "=" * 72)
    print("The requirements agent needs some decisions from you.")
    if understanding:
        print(f"\nWhat it understands so far:\n  {understanding}")
    print("=" * 72)

    answers = []
    for index, question in enumerate(questions, start=1):
        print(f"\n[{index}/{len(questions)}] {question}")
        try:
            answers.append(input("> ").strip())
        except EOFError:
            # Non-interactive stdin: treat the rest as unanswered rather than
            # looping on an EOF that will never become an answer.
            print("(no input available; leaving the remaining questions unanswered)")
            answers.extend([""] * (len(questions) - len(answers)))
            break
    print()
    return answers


def report(final: dict, log_file: Path, run_dir: Path) -> int:
    """Print the outcome. Returns the process exit code.

    A run's own success signals are not evidence that it succeeded — every one of
    them agreed on the run in docs/Bugs.md #15 and all of them were wrong. So this
    prints what was *produced* alongside what the pipeline *concluded*, and it
    exits non-zero for a run that stopped on a bound, because incomplete work
    that reports success is the failure this project exists to refuse.
    """
    status = final.get("status", "unknown")
    reason = final.get("stop_reason", "")
    cost = final.get("total_cost_usd", 0.0)

    merged = [r for r in final.get("wave_results") or [] if r.get("merged")]
    changed: set[str] = set()
    for record in merged:
        for task in record.get("tasks", []):
            changed.update(task.get("changed_files") or [])

    print("\n" + "=" * 72)
    print(f"Run {final.get('run_id', '?')} — {status.upper()}")
    print("=" * 72)
    if reason:
        print(f"\n{reason}\n")
    print(f"  waves merged     {len(merged)} of {len(final.get('waves') or [])}")
    print(f"  files changed    {len(changed)}")
    print(f"  cost             ${cost:.4f}")
    if final.get("integration_branch"):
        print(f"  branch           {final['integration_branch']}")
    print(f"  artifacts        {run_dir}")
    print(f"  log              {log_file}")

    if changed:
        print("\n  Changed files:")
        for path in sorted(changed):
            print(f"    {path}")
    elif status == "completed":
        # A completed run that produced nothing is the exact signature this
        # project refuses to accept quietly.
        print("\n  WARNING: this run reports success and changed no files.")

    print()
    return 0 if status == "completed" else 1


# ── Driving the graph ────────────────────────────────────────────────────────


def run_pipeline(state: dict, thread: dict, graph) -> dict:
    """Stream the graph to completion, answering any pause along the way."""
    payload: dict | object = state

    while True:
        result = graph.invoke(payload, thread)

        interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
        if not interrupts:
            return result

        from langgraph.types import Command

        request = interrupts[0].value
        answers = ask(request.get("questions", []), request.get("understanding", ""))
        payload = Command(resume=answers)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.yes:
        config.INTERACTIVE_REQUIREMENTS = False

    if args.dry_run:
        print(describe_config())
        print()
        print(render_graph())
        return 0

    if not args.goal or not args.target:
        print("Both --goal and --target are required. Use --dry-run to inspect setup.")
        return 2

    target, error = validate_target(args.target)
    if error:
        print(f"error: {error}")
        return 2

    run_id = args.resume or args.run_id or datetime.now().strftime("run-%Y%m%d-%H%M%S")
    run_dir = config.RUNS_DIR / run_id
    artifacts = art.prepare(run_dir)

    log_file = setup_logging(app_name=f"orchestrator_{run_id}")
    set_run_id(run_id)

    logger.info("goal: %s", args.goal)
    logger.info("target: %s", target)
    print(describe_config())

    config.CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    from langgraph.checkpoint.sqlite import SqliteSaver

    # The checkpointer is not optional: it is what makes the requirements pause
    # possible, and it is what lets a killed run be resumed rather than restarted.
    with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DIR / f"{run_id}.sqlite")) as saver:
        graph = compile_graph(checkpointer=saver)
        thread = {
            "configurable": {"thread_id": run_id},
            "recursion_limit": config.RECURSION_LIMIT,
        }

        if args.resume:
            logger.info("resuming %s from its checkpoint", run_id)
            # None continues from wherever the checkpoint left off.
            final = run_pipeline(None, thread, graph)
        else:
            final = run_pipeline(
                initial_state(
                    run_id=run_id,
                    goal=args.goal,
                    target_repo=str(target),
                    run_dir=str(artifacts.run_dir),
                ),
                thread,
                graph,
            )

    return report(final, log_file, artifacts.run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
