"""The entry point: argument handling, target validation, and reporting."""

from __future__ import annotations

from pathlib import Path

import pytest

import config
import main


# ── Target validation ────────────────────────────────────────────────────────


def test_a_real_git_repo_is_accepted(git_repo):
    path, error = main.validate_target(str(git_repo))
    assert error == ""
    assert path == git_repo.resolve()


def test_the_orchestrators_own_repository_is_refused():
    """Agents here write code and merge branches; aimed at this repo they would
    be editing the orchestrator that is driving them, mid-run."""
    path, error = main.validate_target(str(config.PROJECT_ROOT))
    assert path is None
    assert "orchestrator's own repository" in error


def test_a_missing_directory_is_refused(tmp_path):
    path, error = main.validate_target(str(tmp_path / "nope"))
    assert path is None
    assert "not a directory" in error


def test_a_non_repo_is_refused_when_worktrees_are_on(tmp_path, monkeypatch):
    monkeypatch.setattr("config.USE_GIT_WORKTREES", True)
    plain = tmp_path / "plain"
    plain.mkdir()

    path, error = main.validate_target(str(plain))

    assert path is None
    assert "not a git repository" in error
    # The message names both ways out rather than only the failure.
    assert "git init" in error
    assert "USE_GIT_WORKTREES" in error


def test_a_non_repo_is_allowed_in_shared_workspace_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("config.USE_GIT_WORKTREES", False)
    plain = tmp_path / "plain"
    plain.mkdir()

    path, error = main.validate_target(str(plain))

    assert error == ""
    assert path == plain.resolve()


# ── Arguments ────────────────────────────────────────────────────────────────


def test_goal_and_target_are_both_required(capsys):
    assert main.main(["--goal", "do a thing"]) == 2
    assert "required" in capsys.readouterr().out


def test_a_bad_target_exits_two_without_starting_anything(capsys, tmp_path):
    code = main.main(["--goal", "g", "--target", str(tmp_path / "nowhere")])
    assert code == 2
    assert "error:" in capsys.readouterr().out


def test_dry_run_prints_the_configuration_and_the_graph(capsys):
    code = main.main(["--dry-run"])
    out = capsys.readouterr().out

    assert code == 0
    assert "transport" in out
    assert "reviewer" in out
    assert "supervisor" in out
    # Every agent's backend and model is stated, so a run is never a surprise.
    assert "requirements" in out
    assert "coding subagent" in out


def test_yes_switches_off_the_clarification_pause(monkeypatch):
    monkeypatch.setattr("config.INTERACTIVE_REQUIREMENTS", True)
    main.main(["--dry-run", "--yes"])
    assert config.INTERACTIVE_REQUIREMENTS is False


def test_describe_config_reflects_the_toggles(monkeypatch):
    monkeypatch.setattr("config.ENABLE_REVIEWER", False)
    monkeypatch.setattr("config.ENABLE_SUPERVISOR", True)
    text = main.describe_config()
    assert "reviewer          OFF" in text
    assert "supervisor        ON" in text


# ── Reporting ────────────────────────────────────────────────────────────────


def _final(**kw):
    base = {
        "run_id": "r1",
        "status": "completed",
        "stop_reason": "done",
        "total_cost_usd": 1.25,
        "waves": [["T-001"]],
        "wave_results": [
            {"wave": 0, "merged": True, "tasks": [{"changed_files": ["a.py", "b.py"]}]}
        ],
    }
    base.update(kw)
    return base


def test_a_completed_run_exits_zero_and_lists_what_changed(capsys, tmp_path):
    code = main.report(_final(), tmp_path / "log.txt", tmp_path / "run")
    out = capsys.readouterr().out

    assert code == 0
    assert "COMPLETED" in out
    assert "a.py" in out and "b.py" in out
    assert "$1.2500" in out


def test_a_bounded_run_exits_non_zero(capsys, tmp_path):
    """Incomplete work reporting success is the failure this refuses."""
    code = main.report(
        _final(status="bounded", stop_reason="rework budget spent"), tmp_path / "l", tmp_path / "r"
    )
    out = capsys.readouterr().out

    assert code == 1
    assert "BOUNDED" in out
    assert "rework budget spent" in out


def test_a_failed_run_exits_non_zero(capsys, tmp_path):
    code = main.report(_final(status="failed"), tmp_path / "l", tmp_path / "r")
    assert code == 1


def test_a_completed_run_that_changed_nothing_is_flagged(capsys, tmp_path):
    """Bugs.md #15's exact signature, called out where a reader will see it."""
    final = _final(wave_results=[{"wave": 0, "merged": True, "tasks": [{"changed_files": []}]}])

    code = main.report(final, tmp_path / "l", tmp_path / "r")
    out = capsys.readouterr().out

    assert code == 0  # the pipeline's own verdict is respected
    assert "WARNING" in out  # and contradicted in plain sight
    assert "changed no files" in out


def test_unmerged_waves_are_not_counted_as_merged(capsys, tmp_path):
    final = _final(
        waves=[["T-001"], ["T-002"]],
        wave_results=[
            {"wave": 0, "merged": True, "tasks": [{"changed_files": ["a.py"]}]},
            {"wave": 1, "merged": False, "tasks": [{"changed_files": ["b.py"]}]},
        ],
    )

    main.report(final, tmp_path / "l", tmp_path / "r")
    out = capsys.readouterr().out

    assert "waves merged     1 of 2" in out
    assert "b.py" not in out


# ── The question prompt ──────────────────────────────────────────────────────


def test_answers_are_collected_in_order(monkeypatch, capsys):
    answers = iter(["yes", "postgres"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))

    result = main.ask(["Cache it?", "Which database?"], "A small service.")

    assert result == ["yes", "postgres"]
    assert "A small service." in capsys.readouterr().out


def test_a_closed_stdin_leaves_the_rest_unanswered(monkeypatch):
    """Better than looping on an EOF that will never become an answer."""

    def raise_eof(*_):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    result = main.ask(["One?", "Two?", "Three?"], "")

    assert result == ["", "", ""]
