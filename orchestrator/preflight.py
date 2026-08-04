"""Verify that the orchestrator's required local dependencies are usable."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_MONGODB_URI = "mongodb://localhost:27017"
DEFAULT_TIMEOUT_SECONDS = 10.0

# Maestro's built-in external coding-agent tools. A config entry may override
# its executable with a "command" field.
AGENT_COMMANDS = {
    "aider": "aider",
    "agy": "agy",
    "claude": "claude",
    "codex": "codex",
    "gemini": "gemini",
    "opencode": "opencode",
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _load_cli_tools(project_root: Path, config_path: Path | None) -> tuple[Path, dict[str, Any]]:
    if config_path is not None:
        config = _read_json(config_path)
        if not config:
            raise ValueError(f"CLI tools config not found: {config_path}")
        return config_path, config

    global_path = Path.home() / ".maestro" / "cli-tools.json"
    workspace_path = project_root / ".maestro" / "cli-tools.json"
    global_config = _read_json(global_path)
    workspace_config = _read_json(workspace_path)

    if not global_config and not workspace_config:
        raise ValueError(
            "CLI tools config not found at "
            f"{workspace_path} or {global_path}"
        )

    merged = dict(global_config)
    merged_tools = dict(global_config.get("tools", {}))
    merged_tools.update(workspace_config.get("tools", {}))
    merged.update(workspace_config)
    merged["tools"] = merged_tools
    return (workspace_path if workspace_config else global_path), merged


def _enabled_agent_commands(config: dict[str, Any]) -> list[tuple[str, str]]:
    tools = config.get("tools")
    if not isinstance(tools, dict):
        raise ValueError("CLI tools config must contain a 'tools' object")

    commands: list[tuple[str, str]] = []
    for name, entry in tools.items():
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue

        configured_command = entry.get("command")
        if configured_command is not None and not isinstance(configured_command, str):
            raise ValueError(f"tool '{name}' has a non-string command")

        command = configured_command or AGENT_COMMANDS.get(name)
        if command:
            commands.append((name, command))

    return commands


def _command_parts(command: str) -> list[str]:
    parts = shlex.split(command, posix=os.name != "nt")
    if not parts:
        raise ValueError("command is empty")
    return parts


def _resolve_executable(command: str) -> tuple[list[str], str]:
    parts = _command_parts(command)
    executable = os.path.expandvars(os.path.expanduser(parts[0]))
    explicit_path = Path(executable)

    if explicit_path.is_absolute() or explicit_path.parent != Path("."):
        if not explicit_path.is_file():
            raise FileNotFoundError(executable)
        resolved = str(explicit_path.resolve())
    else:
        resolved = shutil.which(executable) or ""
        if not resolved:
            raise FileNotFoundError(executable)

    return [resolved, *parts[1:]], resolved


def _run(command: list[str], timeout: float, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _first_output_line(process: subprocess.CompletedProcess[str]) -> str:
    output = (process.stdout or process.stderr).strip()
    return output.splitlines()[0] if output else "no version output"


def check_version(name: str, command: str, timeout: float) -> CheckResult:
    try:
        invocation, resolved = _resolve_executable(command)
        process = _run([*invocation, "--version"], timeout)
    except FileNotFoundError:
        return CheckResult(name, False, f"binary not found on PATH: {command}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return CheckResult(name, False, f"version probe failed: {exc}")

    detail = f"{_first_output_line(process)} ({resolved})"
    if process.returncode != 0:
        return CheckResult(name, False, f"version probe exited {process.returncode}: {detail}")
    return CheckResult(name, True, detail)


def check_git_worktree(project_root: Path, timeout: float) -> CheckResult:
    try:
        invocation, resolved = _resolve_executable("git")
        version = _run([*invocation, "--version"], timeout, project_root)
        if version.returncode != 0:
            return CheckResult(
                "Git worktree",
                False,
                f"git version probe exited {version.returncode}: {_first_output_line(version)}",
            )

        worktrees = _run([*invocation, "worktree", "list", "--porcelain"], timeout, project_root)
    except FileNotFoundError:
        return CheckResult("Git worktree", False, "git binary not found on PATH")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return CheckResult("Git worktree", False, f"probe failed: {exc}")

    if worktrees.returncode != 0:
        return CheckResult(
            "Git worktree",
            False,
            f"git worktree exited {worktrees.returncode}: {_first_output_line(worktrees)}",
        )
    return CheckResult(
        "Git worktree",
        True,
        f"supported by {_first_output_line(version)} ({resolved})",
    )


def check_mongodb(uri: str, timeout: float) -> CheckResult:
    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError
    except ImportError:
        return CheckResult(
            "MongoDB",
            False,
            "pymongo is not installed in this Python environment",
        )

    client = MongoClient(
        uri,
        serverSelectionTimeoutMS=max(1, int(timeout * 1000)),
        connectTimeoutMS=max(1, int(timeout * 1000)),
    )
    try:
        response = client.admin.command("ping")
    except PyMongoError as exc:
        return CheckResult("MongoDB", False, f"ping failed for {uri}: {exc}")
    finally:
        client.close()

    if response.get("ok") != 1.0:
        return CheckResult("MongoDB", False, f"unexpected ping response from {uri}: {response}")
    return CheckResult("MongoDB", True, f"ping succeeded at {uri}")


def run_preflight(
    project_root: Path,
    config_path: Path | None,
    mongodb_uri: str,
    timeout: float,
) -> list[CheckResult]:
    results = [check_version("Maestro", "maestro", timeout)]

    try:
        loaded_path, config = _load_cli_tools(project_root, config_path)
        agents = _enabled_agent_commands(config)
        results.append(
            CheckResult("Agent config", True, f"loaded {loaded_path}")
        )
        if not agents:
            results.append(
                CheckResult("Agent binaries", False, "no external coding agents are enabled")
            )
        else:
            results.extend(
                check_version(f"Agent: {name}", command, timeout)
                for name, command in agents
            )
    except ValueError as exc:
        results.append(CheckResult("Agent config", False, str(exc)))

    results.append(check_mongodb(mongodb_uri, timeout))
    results.append(check_git_worktree(project_root, timeout))
    return results


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check agent CLIs, MongoDB, and Git worktree support."
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to cli-tools.json (defaults to workspace, then ~/.maestro).",
    )
    parser.add_argument(
        "--mongodb-uri",
        default=os.environ.get("BHAI_TO_BHAI_MONGODB_URI", DEFAULT_MONGODB_URI),
        help="MongoDB URI (or set BHAI_TO_BHAI_MONGODB_URI).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-check timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g}).",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    project_root = Path(__file__).resolve().parent.parent
    results = run_preflight(
        project_root=project_root,
        config_path=args.config,
        mongodb_uri=args.mongodb_uri,
        timeout=args.timeout,
    )

    print("Bhai-To-Bhai preflight")
    print("=" * 24)
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.name}: {result.detail}")

    failures = sum(not result.passed for result in results)
    print("-" * 24)
    if failures:
        print(f"Preflight failed: {failures} check(s) need attention.")
        return 1

    print("Preflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
