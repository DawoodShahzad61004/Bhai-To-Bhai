"""Test fixtures shared by every agent's suite.

Two things every test in this tree gets for free: the `orchestrator/` directory
on `sys.path` (so the bare `from config import ...` style the package uses
resolves the same way it does at runtime), and the stub transport reset between
tests so one test's scripted replies cannot leak into the next.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent
if str(ORCHESTRATOR_DIR) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATOR_DIR))

import adapters.stub as stub_backend  # noqa: E402
import artifacts as artifacts_module  # noqa: E402
from state import initial_state  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_transport(monkeypatch):
    """Every test runs on the stub transport, with no scripted replies.

    Autouse and unconditional: a test that reaches a real CLI would cost money
    and would not be deterministic. A test that needs one asks for it explicitly
    by overriding INVOCATION itself.
    """
    monkeypatch.setattr("config.INVOCATION", "stub", raising=False)
    stub_backend.reset()
    yield
    stub_backend.reset()


@pytest.fixture
def stub():
    """The stub transport, for scripting replies and asserting on calls."""
    return stub_backend


@pytest.fixture
def run_dir(tmp_path):
    """An empty artifact store for one run, sibling to a throwaway target."""
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    return artifacts_module.prepare("testrun", target)


@pytest.fixture
def state(tmp_path, run_dir):
    """A fresh pipeline state pointed at a throwaway target repository.

    Shares `run_dir`'s target and run id, so the two fixtures resolve to the
    same artifact store when a test asks for both.
    """
    return initial_state(
        run_id=run_dir.run_id,
        goal="Add a health-check endpoint",
        target_repo=str(tmp_path / "target"),
    )


@pytest.fixture
def git_repo(tmp_path):
    """A real git repository with one commit, for the worktree tests.

    Real git rather than a fake: worktree creation, merge conflicts and branch
    resolution are precisely the behaviours a fake would get wrong, and they are
    what the merger agent's whole existence depends on.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    run("init", "-b", "main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    run("config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "initial")
    return repo
