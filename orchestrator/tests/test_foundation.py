"""The layer every agent stands on: adapters, artifacts, worktrees, state.

None of these tests reaches a real coding-agent CLI. The worktree tests do reach
real git, because the behaviour under test *is* git's.
"""

from __future__ import annotations

import json
import os

import pytest

import adapters
import artifacts as art
import worktrees as wt
import config
from adapters.base import AgentResult, _dispatch_key, classify_failure, subprocess_env
from adapters.codex import _thread_id
from config import AgentSpec
from state import event, initial_state

SPEC = AgentSpec(backend="claude", model="haiku", deadline_seconds=30)


# ── Adapter contract ─────────────────────────────────────────────────────────


def test_every_transport_is_registered():
    registered = adapters.available_backends()
    assert "stub" in registered
    assert "direct:claude" in registered
    assert "direct:codex" in registered
    assert "maestro" in registered


def test_dispatch_key_separates_transport_from_vendor():
    assert _dispatch_key("direct", "claude") == "direct:claude"
    assert _dispatch_key("direct", "codex") == "direct:codex"
    # maestro and stub speak to any vendor themselves.
    assert _dispatch_key("maestro", "codex") == "maestro"
    assert _dispatch_key("stub", "claude") == "stub"


def test_unknown_transport_returns_a_result_rather_than_raising(stub):
    result = adapters.run_agent(
        "hello", spec=SPEC, cwd=".", tag="x", invocation="nonsense"
    )
    assert result.ok is False
    assert result.error_kind == "bad_request"
    assert "nonsense" in result.error_message


def test_missing_binary_is_a_result_not_an_exception(monkeypatch):
    """A CLI that is not installed must be routable, not fatal (Bugs.md #19)."""
    monkeypatch.setattr("config.CLAUDE_BIN", "definitely-not-a-real-binary-xyz")
    result = adapters.run_agent(
        "hello",
        spec=AgentSpec(backend="claude", model="", deadline_seconds=5),
        cwd=".",
        tag="probe",
        invocation="direct",
    )
    assert result.ok is False
    assert result.error_kind == "not_installed"


@pytest.mark.parametrize(
    "message",
    [
        "You have exceeded your rate limit",
        "HTTP 429 Too Many Requests",
        "usage limit reached for this account",
        "insufficient_quota",
    ],
)
def test_rate_limits_are_classified_from_config_markers(message):
    """A limit is authoritative; everything else is just an error (ADR-006)."""
    assert classify_failure(message) == "rate_limit"


def test_ordinary_failures_keep_their_default_class():
    assert classify_failure("segmentation fault") == "agent_error"
    assert classify_failure("nothing printed", default="no_output") == "no_output"


def test_codex_session_id_comes_from_the_first_json_event():
    """Codex reports its resumable id once, as event one of the --json stream."""
    stdout = "\n".join(
        [
            '{"type":"thread.started","thread_id":"019fe1aa-9c61-7670-96b0-2339ef174b12"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}',
            '{"type":"turn.completed","usage":{"input_tokens":21760}}',
        ]
    )
    assert _thread_id(stdout) == "019fe1aa-9c61-7670-96b0-2339ef174b12"


def test_codex_error_events_do_not_hide_the_session_id():
    """`item.completed` errors are emitted on turns that succeed (feature
    warnings), so the id must survive them and they must not be read as failures.
    A BOM on line one is a Windows pipeline artefact and must not break parsing."""
    stdout = "\n".join(
        [
            '﻿{"type":"thread.started","thread_id":"abc-123"}',
            '{"type":"item.completed","item":{"type":"error","message":"under-development features enabled"}}',
            "not json at all",
        ]
    )
    assert _thread_id(stdout) == "abc-123"


def test_codex_session_id_is_absent_rather_than_invented():
    assert _thread_id("") == ""
    assert _thread_id('{"type":"turn.completed"}') == ""


def test_subprocess_env_removes_python_overrides(monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "C:/bad-python")
    monkeypatch.setenv("PYTHONPATH", "C:/bad-python/site-packages")

    env = subprocess_env()

    local_bin = str(config.PROJECT_ROOT / "node_modules" / ".bin")

    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PATH"].split(os.pathsep)[0] == local_bin


def test_stub_records_calls_and_replays_scripted_replies(stub):
    stub.set_text("planner", "PLAN OK", cost_usd=0.25)
    result = adapters.run_agent("decompose this", spec=SPEC, cwd="/tmp", tag="planner")
    assert result.ok is True
    assert result.text == "PLAN OK"
    assert result.cost_usd == 0.25
    assert stub.calls[0]["prompt"] == "decompose this"
    assert stub.calls[0]["tag"] == "planner"


def test_stub_without_a_scripted_reply_fails_loudly(stub):
    """A stub that invented a plausible reply would manufacture Bugs.md #15."""
    result = adapters.run_agent("anything", spec=SPEC, cwd="/tmp", tag="unscripted")
    assert result.ok is False
    assert result.error_kind == "no_output"


def test_stub_patterns_match_by_glob_and_latest_wins(stub):
    stub.set_text("task-*", "generic")
    stub.set_text("task-002", "specific")
    generic = adapters.run_agent("x", spec=SPEC, cwd=".", tag="task-001")
    specific = adapters.run_agent("x", spec=SPEC, cwd=".", tag="task-002")
    assert generic.text == "generic"
    assert specific.text == "specific"


def test_result_summary_reads_differently_for_each_outcome():
    ok = AgentResult(ok=True, duration_seconds=1.5, cost_usd=0.02)
    bad = AgentResult(ok=False, error_kind="timeout", error_message="ran long")
    assert "ok in 1.5s" in ok.summary()
    assert bad.summary().startswith("timeout:")


# ── Artifacts ────────────────────────────────────────────────────────────────


def test_prepare_creates_the_run_layout(tmp_path):
    a = art.prepare(tmp_path / "r1")
    assert a.run_dir.is_dir()
    assert a.reviews_dir.is_dir()
    assert a.context.name == "context.md"
    assert a.user_choices.name == "user_choices.md"


def test_task_ids_are_sanitised_into_paths_and_branches():
    """The planner is a model; its output is untrusted input here."""
    assert art.safe_id("T-001") == "T-001"
    assert art.safe_id("../../etc/passwd") == "etc-passwd"
    assert art.safe_id("feat/add auth") == "feat-add-auth"
    assert art.safe_id("") == "task"


def test_reading_a_missing_artifact_reports_rather_than_raises(run_dir):
    assert art.read_text(run_dir.context) == ""
    assert art.read_text(run_dir.context, default="none yet") == "none yet"
    assert art.read_json(run_dir.plan) is None


def test_learnings_accumulate_across_agents(run_dir):
    art.append_learning(run_dir, "planner", "waves must respect depends_on")
    art.append_learning(run_dir, "merger", "rename conflicts need -X find-renames")
    body = art.read_text(run_dir.learnings)
    assert "planner" in body and "merger" in body
    assert "waves must respect depends_on" in body
    assert "rename conflicts" in body
    assert body.count("##") == 2


def test_concurrent_appends_to_learnings_do_not_interleave_or_corrupt(run_dir):
    """The coding subagents write learnings.md from separate OS processes,
    genuinely in parallel — a Python-only lock would not reach across them, so
    this has to prove the OS-level lock actually holds under real concurrency."""
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor

    script = str(__import__("pathlib").Path(art.__file__).resolve())

    def append(i: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, script, "append-learning", str(run_dir.run_dir), f"task-{i}", f"finding {i}"],
            capture_output=True,
            text=True,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(append, range(8)))

    for result in results:
        assert result.returncode == 0, result.stderr

    body = art.read_text(run_dir.learnings)
    assert body.count("# Learnings") == 1
    for i in range(8):
        assert f"finding {i}" in body


def test_the_append_learning_cli_reuses_the_same_writer(run_dir):
    import subprocess
    import sys

    script = str(__import__("pathlib").Path(art.__file__).resolve())
    result = subprocess.run(
        [sys.executable, script, "append-learning", str(run_dir.run_dir), "task-T-001", "found a thing"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    body = art.read_text(run_dir.learnings)
    assert "found a thing" in body
    assert "task-T-001" in body


def test_events_are_written_on_arrival(run_dir):
    """Durability at event time, not at process exit (ADR-005)."""
    art.append_event(run_dir, event("wave_started", wave=0))
    art.append_event(run_dir, event("wave_merged", wave=0))
    lines = run_dir.events.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["kind"] == "wave_started"
    assert "at" in json.loads(lines[1])


def test_tasks_round_trip_through_disk(run_dir):
    tasks = [
        {"task_id": "T-001", "title": "one", "description": "d1"},
        {"task_id": "T-002", "title": "two", "description": "d2"},
    ]
    paths = art.write_tasks(run_dir, tasks)
    assert len(paths) == 2
    assert paths[0].name == "TASK-T-001.json"
    assert [t["task_id"] for t in art.load_tasks(run_dir)] == ["T-001", "T-002"]


def test_artifacts_survive_non_ascii(run_dir):
    """Bare open() on Windows is cp1252 and dies on an em dash (Bugs.md #11)."""
    art.write_text(run_dir.context, "Goal — ship it. Café. 日本語.")
    assert "—" in art.read_text(run_dir.context)


# ── State ────────────────────────────────────────────────────────────────────


def test_initial_state_zeroes_every_counter():
    """A bound must never be evaluated against a missing value."""
    s = initial_state(run_id="r", goal="g", target_repo="/t", run_dir="/d")
    assert s["current_wave"] == 0
    assert s["rework_count"] == 0
    assert s["replan_count"] == 0
    assert s["wave_results"] == []
    assert s["events"] == []
    assert s["status"] == "running"


def test_event_carries_a_timestamp_and_its_fields():
    e = event("review_verdict", verdict="rework", wave=2)
    assert e["kind"] == "review_verdict"
    assert e["verdict"] == "rework"
    assert e["wave"] == 2
    assert e["at"].endswith("+00:00")


# ── Worktrees (real git) ─────────────────────────────────────────────────────


def test_fixture_repo_is_a_repo(git_repo):
    assert wt.is_git_repo(git_repo)
    assert wt.current_branch(git_repo) == "main"
    assert len(wt.head_sha(git_repo)) == 40


def test_a_non_repo_is_reported_not_raised(tmp_path):
    assert wt.is_git_repo(tmp_path) is False


def test_worktrees_isolate_two_tasks(git_repo):
    a, ra = wt.create(git_repo, run_id="r1", task_id="T-001", base="main")
    b, rb = wt.create(git_repo, run_id="r1", task_id="T-002", base="main")
    assert ra.ok and rb.ok
    assert a is not None and b is not None
    assert a.path != b.path
    assert a.branch == "bhai/r1/T-001"

    (a.path / "a.txt").write_text("from A\n", encoding="utf-8")
    (b.path / "b.txt").write_text("from B\n", encoding="utf-8")
    # Neither agent can see the other's work.
    assert not (a.path / "b.txt").exists()
    assert not (b.path / "a.txt").exists()

    wt.cleanup(git_repo, [a, b])
    assert not a.path.exists()
    assert not b.path.exists()


def test_orchestrator_side_commit_captures_what_an_agent_left(git_repo):
    """ADR-005: a killed process must still leave a diff."""
    tree, _ = wt.create(git_repo, run_id="r2", task_id="T-001", base="main")
    (tree.path / "new.txt").write_text("work\n", encoding="utf-8")
    assert wt.has_changes(tree.path) is True

    result = wt.commit_all(tree.path, "checkpoint: agent killed mid-turn")
    assert result.ok
    assert wt.has_changes(tree.path) is False
    assert len(wt.head_sha(tree.path)) == 40

    # A second commit with nothing to commit is a no-op, not a failure.
    again = wt.commit_all(tree.path, "checkpoint")
    assert again.ok
    assert again.stdout == "nothing to commit"
    wt.cleanup(git_repo, [tree])


def test_clean_merges_land_on_the_integration_branch(git_repo):
    branch, created = wt.ensure_integration_branch(git_repo, "r3")
    assert created.ok
    assert branch == "bhai/r3/integration"

    trees = []
    for index, task in enumerate(("T-001", "T-002")):
        tree, _ = wt.create(git_repo, run_id="r3", task_id=task, base="main")
        (tree.path / f"file{index}.txt").write_text(f"{task}\n", encoding="utf-8")
        wt.commit_all(tree.path, f"{task}: add file{index}")
        trees.append(tree)

    for tree in trees:
        result = wt.merge(git_repo, branch=tree.branch, into=branch, message=f"merge {tree.task_id}")
        assert result.ok, result.stderr
        assert wt.conflicted_files(git_repo) == []

    assert (git_repo / "file0.txt").exists()
    assert (git_repo / "file1.txt").exists()
    wt.cleanup(git_repo, trees)


def test_a_conflict_is_left_in_place_for_the_merger_to_resolve(git_repo):
    """Aborting here would throw away the state the merger is dispatched to fix."""
    branch, _ = wt.ensure_integration_branch(git_repo, "r4")

    trees = []
    for task, text in (("T-001", "alpha"), ("T-002", "beta")):
        tree, _ = wt.create(git_repo, run_id="r4", task_id=task, base="main")
        (tree.path / "shared.txt").write_text(f"{text}\n", encoding="utf-8")
        wt.commit_all(tree.path, f"{task}: write shared.txt")
        trees.append(tree)

    first = wt.merge(git_repo, branch=trees[0].branch, into=branch, message="merge 1")
    assert first.ok

    second = wt.merge(git_repo, branch=trees[1].branch, into=branch, message="merge 2")
    assert second.ok is False
    conflicts = wt.conflicted_files(git_repo)
    assert conflicts == ["shared.txt"]
    # The conflict markers are on disk, which is what the merger agent reads.
    assert "<<<<<<<" in (git_repo / "shared.txt").read_text(encoding="utf-8")

    assert wt.abort_merge(git_repo).ok
    wt.cleanup(git_repo, trees)


def test_a_stale_worktree_path_is_reconciled_not_collided_with(git_repo):
    """A leftover from an interrupted run must not block the next one."""
    first, _ = wt.create(git_repo, run_id="r5", task_id="T-001", base="main")
    (first.path / "partial.txt").write_text("half-done\n", encoding="utf-8")

    second, result = wt.create(git_repo, run_id="r5", task_id="T-001", base="main")
    assert result.ok, result.stderr
    assert second is not None
    assert second.path == first.path
    assert not (second.path / "partial.txt").exists()
    wt.cleanup(git_repo, [second])
