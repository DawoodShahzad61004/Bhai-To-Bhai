"""Agent 4 — merger.

Real git throughout: these tests are about what happens when two branches touch
the same lines, which is not something a fake can produce faithfully.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import artifacts as art
import worktrees as wt
from adapters.base import AgentResult
from merger import markers_left_in, merger_node
from state import initial_state, upsert_wave_results


def _commit_on_branch(repo: Path, run_id: str, task_id: str, filename: str, body: str) -> dict:
    """A finished coding subagent: a branch with one commit on it."""
    tree, _ = wt.create(repo, run_id=run_id, task_id=task_id, base="main")
    path = tree.path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    wt.commit_all(tree.path, f"{task_id}: write {filename}")
    return {
        "task_id": task_id,
        "ok": True,
        "branch": tree.branch,
        "worktree": str(tree.path),
        "report": f"wrote {filename}",
        "changed_files": [filename],
        "session_id": f"sess-{task_id}",
    }


@pytest.fixture
def merge_state(git_repo, tmp_path):
    artifacts = art.prepare("m1", git_repo)
    art.write_text(artifacts.context, "# Context\nBuild the thing.")
    state = initial_state(
        run_id="m1",
        goal="Build the thing",
        target_repo=str(git_repo),
    )
    state["context"] = "# Context\nBuild the thing."
    state["current_wave"] = 0
    return state


# ── The reducer both writers depend on ───────────────────────────────────────


def test_a_new_attempt_appends_and_a_merge_result_folds_in():
    """Two writers, one row: the trap this reducer exists to avoid."""
    history = upsert_wave_results([], [{"wave": 0, "attempt": 0, "tasks": ["a"]}])
    history = upsert_wave_results(history, [{"wave": 0, "attempt": 0, "merged": True}])
    assert len(history) == 1
    assert history[0]["tasks"] == ["a"]
    assert history[0]["merged"] is True

    history = upsert_wave_results(history, [{"wave": 0, "attempt": 1, "tasks": ["b"]}])
    history = upsert_wave_results(history, [{"wave": 1, "attempt": 0, "tasks": ["c"]}])
    assert [(r["wave"], r["attempt"]) for r in history] == [(0, 0), (0, 1), (1, 0)]
    # The earlier attempt is untouched by the later one.
    assert history[0]["merged"] is True
    assert "merged" not in history[1]


# ── Clean merges ─────────────────────────────────────────────────────────────


def test_two_clean_branches_merge_with_no_agent_call(merge_state, git_repo, stub):
    """The cheapest of the six boxes: a clean wave costs nothing."""
    tasks = [
        _commit_on_branch(git_repo, "m1", "T-001", "a.txt", "alpha\n"),
        _commit_on_branch(git_repo, "m1", "T-002", "b.txt", "beta\n"),
    ]
    merge_state["wave_results"] = [{"wave": 0, "attempt": 0, "task_ids": ["T-001", "T-002"], "tasks": tasks}]

    result = merger_node(merge_state)

    assert result["merge_ok"] is True
    assert stub.calls == []
    assert result["total_cost_usd"] == 0.0
    assert result["integration_branch"] == "bhai/m1/integration"

    wt.git(git_repo, "checkout", "bhai/m1/integration")
    assert (git_repo / "a.txt").exists()
    assert (git_repo / "b.txt").exists()


def test_the_merged_flag_lands_on_the_attempt_it_belongs_to(merge_state, git_repo, stub):
    tasks = [_commit_on_branch(git_repo, "m1", "T-001", "a.txt", "alpha\n")]
    merge_state["wave_results"] = [{"wave": 0, "attempt": 0, "task_ids": ["T-001"], "tasks": tasks}]

    result = merger_node(merge_state)

    record = result["wave_results"][0]
    assert record["wave"] == 0
    assert record["attempt"] == 0
    assert record["merged"] is True


def test_a_failed_task_is_skipped_by_name(merge_state, git_repo, stub):
    """Its branch survives, carrying whatever partial work it committed."""
    good = _commit_on_branch(git_repo, "m1", "T-001", "a.txt", "alpha\n")
    bad = _commit_on_branch(git_repo, "m1", "T-002", "b.txt", "beta\n")
    bad["ok"] = False
    merge_state["wave_results"] = [{"wave": 0, "attempt": 0, "task_ids": ["T-001", "T-002"], "tasks": [good, bad]}]

    result = merger_node(merge_state)

    assert result["merge_ok"] is True
    assert "1 skipped" in result["merge_report"]
    assert result["events"][0]["skipped"] == ["T-002"]
    # The failed task's branch still exists for a rework to build on.
    assert wt.git(git_repo, "rev-parse", "--verify", "bhai/m1/T-002").ok


def test_shared_workspace_mode_has_nothing_to_merge(merge_state, git_repo, stub, monkeypatch):
    monkeypatch.setattr("config.USE_GIT_WORKTREES", False)
    merge_state["wave_results"] = [{"wave": 0, "attempt": 0, "task_ids": [], "tasks": []}]

    result = merger_node(merge_state)

    assert result["merge_ok"] is True
    assert "USE_GIT_WORKTREES is off" in result["merge_report"]
    assert stub.calls == []


# ── Conflicts ────────────────────────────────────────────────────────────────


def _conflicting_wave(git_repo) -> list[dict]:
    return [
        _commit_on_branch(git_repo, "m1", "T-001", "shared.txt", "alpha\n"),
        _commit_on_branch(git_repo, "m1", "T-002", "shared.txt", "beta\n"),
    ]


def resolving_agent(body: str, *, learnings: str = ""):
    """A merge agent that actually rewrites the conflicted file."""

    def reply(prompt: str) -> AgentResult:
        # The brief names the repository as the working directory.
        return AgentResult(
            ok=True,
            text=json.dumps(
                {"status": "resolved", "summary": "kept both", "learnings": learnings}
            ),
            cost_usd=0.09,
        )

    return reply


def test_a_conflict_dispatches_the_merge_agent_with_the_files_named(
    merge_state, git_repo, stub
):
    tasks = _conflicting_wave(git_repo)
    merge_state["wave_results"] = [{"wave": 0, "attempt": 0, "task_ids": ["T-001", "T-002"], "tasks": tasks}]

    def resolve(prompt: str) -> AgentResult:
        (git_repo / "shared.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        return AgentResult(ok=True, text=json.dumps({"status": "resolved", "summary": "kept both"}), cost_usd=0.09)

    stub.set_reply("merger-*", resolve)

    result = merger_node(merge_state)

    assert result["merge_ok"] is True
    assert result["events"][0]["conflicts_resolved"] == ["T-002"]
    prompt = stub.calls[0]["prompt"]
    assert "shared.txt" in prompt
    assert "conflict markers" in prompt
    artifacts = art.prepare(merge_state["run_id"], merge_state["target_repo"])
    assert str(artifacts.shared_dir) in stub.calls[0]["extra_dirs"]
    wt.git(git_repo, "checkout", "bhai/m1/integration")
    assert (git_repo / "shared.txt").read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_a_resolution_that_leaves_a_conflict_marker_is_rejected(merge_state, git_repo, stub):
    """The specific way this claim goes wrong, checked against the filesystem."""
    tasks = _conflicting_wave(git_repo)
    merge_state["wave_results"] = [{"wave": 0, "attempt": 0, "task_ids": ["T-001", "T-002"], "tasks": tasks}]

    def sloppy(prompt: str) -> AgentResult:
        # Stages the file without removing the markers — a real agent habit.
        wt.git(git_repo, "add", "shared.txt")
        return AgentResult(ok=True, text=json.dumps({"status": "resolved", "summary": "done"}))

    stub.set_reply("merger-*", sloppy)

    result = merger_node(merge_state)

    assert result["merge_ok"] is False
    assert "conflict markers remain" in result["merge_report"]
    assert result["status"] == "failed"


def test_an_agent_that_claims_success_and_touched_nothing_is_rejected(merge_state, git_repo, stub):
    """The report is fluent and the file is still full of markers."""
    tasks = _conflicting_wave(git_repo)
    merge_state["wave_results"] = [{"wave": 0, "attempt": 0, "task_ids": ["T-001", "T-002"], "tasks": tasks}]
    stub.set_reply(
        "merger-*",
        AgentResult(ok=True, text=json.dumps({"status": "resolved", "summary": "I fixed it"})),
    )

    result = merger_node(merge_state)

    assert result["merge_ok"] is False
    assert "conflict markers remain" in result["merge_report"]


def test_deleting_a_conflicted_file_is_a_valid_resolution(merge_state, git_repo, stub):
    """Sometimes the right answer is that the file should not exist."""
    tasks = _conflicting_wave(git_repo)
    merge_state["wave_results"] = [{"wave": 0, "attempt": 0, "task_ids": ["T-001", "T-002"], "tasks": tasks}]

    def delete_it(prompt: str) -> AgentResult:
        (git_repo / "shared.txt").unlink()
        return AgentResult(
            ok=True,
            text=json.dumps({"status": "resolved", "summary": "both sides were obsolete"}),
        )

    stub.set_reply("merger-*", delete_it)

    result = merger_node(merge_state)

    assert result["merge_ok"] is True
    wt.git(git_repo, "checkout", "bhai/m1/integration")
    assert not (git_repo / "shared.txt").exists()


def test_an_unresolvable_conflict_stops_the_run_with_the_reason(merge_state, git_repo, stub):
    tasks = _conflicting_wave(git_repo)
    merge_state["wave_results"] = [{"wave": 0, "attempt": 0, "task_ids": ["T-001", "T-002"], "tasks": tasks}]
    stub.set_reply(
        "merger-*",
        AgentResult(
            ok=True,
            text=json.dumps(
                {
                    "status": "unresolvable",
                    "summary": "",
                    "unresolvable_reason": "The two tasks chose incompatible schemas.",
                }
            ),
        ),
    )

    result = merger_node(merge_state)

    assert result["merge_ok"] is False
    assert "incompatible schemas" in result["stop_reason"]
    # The merge is aborted, so the repository is left usable.
    assert wt.conflicted_files(git_repo) == []


def test_a_failed_merge_agent_stops_the_run(merge_state, git_repo, stub):
    tasks = _conflicting_wave(git_repo)
    merge_state["wave_results"] = [{"wave": 0, "attempt": 0, "task_ids": ["T-001", "T-002"], "tasks": tasks}]
    stub.set_reply(
        "merger-*",
        AgentResult(ok=False, error_kind="rate_limit", error_message="usage limit"),
    )

    result = merger_node(merge_state)

    assert result["merge_ok"] is False
    assert "rate_limit" in result["merge_report"]
    assert wt.conflicted_files(git_repo) == []


def test_merge_agent_findings_reach_the_learnings_file(merge_state, git_repo, stub):
    tasks = _conflicting_wave(git_repo)
    merge_state["wave_results"] = [{"wave": 0, "attempt": 0, "task_ids": ["T-001", "T-002"], "tasks": tasks}]

    def resolve(prompt: str) -> AgentResult:
        (git_repo / "shared.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        return AgentResult(
            ok=True,
            text=json.dumps(
                {
                    "status": "resolved",
                    "summary": "kept both",
                    "learnings": "shared.txt is edited by most tasks; serialise them.",
                }
            ),
        )

    stub.set_reply("merger-*", resolve)
    merger_node(merge_state)

    learnings = art.read_text(
        art.prepare(merge_state["run_id"], merge_state["target_repo"]).learnings
    )
    assert "serialise them" in learnings


# ── The marker check itself ──────────────────────────────────────────────────


def test_markers_are_detected_only_at_the_start_of_a_line(tmp_path):
    (tmp_path / "bad.txt").write_text("a\n<<<<<<< HEAD\nb\n=======\nc\n>>>>>>> x\n", encoding="utf-8")
    (tmp_path / "fine.txt").write_text("a diagram: x <<<<<<< y\n", encoding="utf-8")
    (tmp_path / "clean.txt").write_text("nothing here\n", encoding="utf-8")

    found = markers_left_in(tmp_path, ["bad.txt", "fine.txt", "clean.txt", "missing.txt"])
    assert found == ["bad.txt"]


# ── Bookkeeping ──────────────────────────────────────────────────────────────


def test_reaching_the_merger_with_no_wave_stops_the_run(merge_state, stub):
    result = merger_node(merge_state)
    assert result["status"] == "failed"
    assert "no wave to merge" in result["stop_reason"]
