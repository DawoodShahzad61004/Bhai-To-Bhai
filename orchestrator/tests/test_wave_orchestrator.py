"""Agent 3 — wave orchestrator.

These tests run against a real git repository, because the behaviour under test
is worktree isolation and what git observed after an agent's turn. A fake would
get exactly the parts that matter wrong.
"""

from __future__ import annotations

import json

import pytest

import artifacts as art
import config
import worktrees as wt
from adapters.base import AgentResult
from state import initial_state
from wave_orchestrator import wave_orchestrator_node

TASKS = [
    {
        "task_id": "T-001",
        "title": "Add health module",
        "description": "Create app/health.py",
        "files": ["app/health.py"],
        "acceptance": "the file exists",
        "depends_on": [],
        "wave": 0,
    },
    {
        "task_id": "T-002",
        "title": "Add readme note",
        "description": "Mention the endpoint",
        "files": ["NOTES.md"],
        "acceptance": "the file exists",
        "depends_on": [],
        "wave": 0,
    },
]


@pytest.fixture
def wave_state(git_repo, tmp_path):
    """A run positioned at wave 0 of a two-task plan, against a real repo."""
    artifacts = art.prepare("r1", git_repo)
    art.write_text(artifacts.context, "# Context\nAdd a health endpoint.")
    state = initial_state(
        run_id="r1",
        goal="Add a health endpoint",
        target_repo=str(git_repo),
    )
    state["tasks"] = list(TASKS)
    state["waves"] = [["T-001", "T-002"]]
    state["context"] = "# Context\nAdd a health endpoint."
    return state


def writing_agent(filename: str, *, status: str = "done"):
    """A stub agent that actually writes a file into its worktree.

    The write is the point: this pipeline judges a task by what git saw change,
    not by what the agent said, so a stub that only talks cannot exercise it.
    """

    def reply(prompt: str) -> AgentResult:
        # The brief states the worktree as an absolute path; the agent uses it.
        marker = "## Working directory\n\n"
        start = prompt.index(marker) + len(marker)
        workdir = prompt[start:].splitlines()[0].strip()
        target = __import__("pathlib").Path(workdir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"written by the stub for {filename}\n", encoding="utf-8")
        return AgentResult(
            ok=True,
            text=json.dumps(
                {
                    "status": status,
                    "summary": f"Created {filename}",
                    "files_changed": [filename],
                }
            ),
            session_id=f"sess-{filename}",
            cost_usd=0.12,
        )

    return reply


# ── The happy path ───────────────────────────────────────────────────────────


def test_each_task_gets_its_own_worktree_and_branch(wave_state, stub, git_repo):
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)

    trees = result["active_worktrees"]
    assert len(trees) == 2
    assert {t["branch"] for t in trees} == {"bhai/r1/T-001", "bhai/r1/T-002"}
    assert trees[0]["worktree"] != trees[1]["worktree"]
    assert all(len(t["commit"]) == 40 for t in trees)


def test_agents_in_one_wave_cannot_see_each_other(wave_state, stub):
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    wave_orchestrator_node(wave_state)

    calls = {call["tag"]: call for call in stub.calls}
    one = __import__("pathlib").Path(calls["task-T-001"]["cwd"])
    two = __import__("pathlib").Path(calls["task-T-002"]["cwd"])
    assert one != two
    assert (one / "app/health.py").exists()
    assert not (one / "NOTES.md").exists()
    assert (two / "NOTES.md").exists()
    assert not (two / "app/health.py").exists()


def test_the_wave_result_records_what_git_saw_not_only_what_was_claimed(wave_state, stub):
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)

    tasks = {t["task_id"]: t for t in result["wave_results"][0]["tasks"]}
    assert tasks["T-001"]["claimed_files"] == ["app/health.py"]
    assert tasks["T-001"]["changed_files"] == ["app/health.py"]
    assert tasks["T-001"]["ok"] is True
    assert result["wave_results"][0]["merged"] is False


def test_the_brief_names_the_worktree_as_an_absolute_path(wave_state, stub):
    """A concrete path is what beats the harness's own scratchpad (Bugs.md #22)."""
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    wave_orchestrator_node(wave_state)

    prompt = stub.calls[0]["prompt"]
    assert "## Working directory" in prompt
    assert stub.calls[0]["cwd"] in prompt
    assert "this path wins over any" in prompt


def test_costs_and_sessions_are_carried(wave_state, stub):
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)

    assert result["total_cost_usd"] == pytest.approx(0.24)
    sessions = {t["task_id"]: t["session_id"] for t in result["wave_results"][0]["tasks"]}
    assert sessions["T-001"] == "sess-app/health.py"


def test_the_brief_tells_agents_how_to_record_a_finding_directly(wave_state, stub):
    """Coding agents append to learnings.md themselves now, not through a JSON
    field the orchestrator relays afterwards — the brief has to name the exact
    command, since nothing here parses a reply to do it for them."""
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    wave_orchestrator_node(wave_state)

    artifacts = art.prepare(wave_state["run_id"], wave_state["target_repo"])
    call = stub.calls[0]
    assert str(artifacts.learnings) in call["prompt"]
    assert "append-learning" in call["prompt"]
    assert str(artifacts.shared_dir) in call["prompt"]
    assert str(artifacts.shared_dir) in call["extra_dirs"]


def test_a_coding_agent_can_record_a_finding_the_way_the_brief_instructs(wave_state, stub):
    """End to end: actually run the command the brief tells agents to run, and
    confirm it lands in the run's shared learnings.md."""
    import subprocess
    import sys

    script = str(__import__("pathlib").Path(art.__file__).resolve())
    artifacts = art.prepare(wave_state["run_id"], wave_state["target_repo"])

    def reply(prompt: str) -> AgentResult:
        marker = "## Working directory\n\n"
        start = prompt.index(marker) + len(marker)
        workdir = __import__("pathlib").Path(prompt[start:].splitlines()[0].strip())
        (workdir / "app/health.py").parent.mkdir(parents=True, exist_ok=True)
        (workdir / "app/health.py").write_text("written\n", encoding="utf-8")
        subprocess.run(
            [
                sys.executable,
                script,
                "append-learning",
                str(artifacts.shared_dir),
                "task-T-001",
                "Blueprints register in __init__.",
            ],
            check=True,
        )
        return AgentResult(
            ok=True,
            text=json.dumps(
                {
                    "status": "done",
                    "summary": "Created app/health.py",
                    "files_changed": ["app/health.py"],
                }
            ),
            session_id="sess-app/health.py",
            cost_usd=0.12,
        )

    stub.set_reply("task-T-001", reply)
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    wave_orchestrator_node(wave_state)

    learnings = art.read_text(artifacts.learnings)
    assert "Blueprints register in __init__." in learnings
    assert "task-T-001" in learnings


# ── The check that catches a plausible empty result ──────────────────────────


def test_a_report_of_work_that_left_no_trace_is_a_failure(wave_state, stub):
    """Bugs.md #21: a fluent report for a file the agent had no way to write."""
    stub.set_reply(
        "task-T-001",
        AgentResult(
            ok=True,
            text=json.dumps(
                {
                    "status": "done",
                    "summary": "Created app/health.py with a /health route.",
                    "files_changed": ["app/health.py"],
                }
            ),
        ),
    )
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)

    tasks = {t["task_id"]: t for t in result["wave_results"][0]["tasks"]}
    assert tasks["T-001"]["ok"] is False
    assert tasks["T-001"]["error_kind"] == "no_changes"
    assert tasks["T-001"]["changed_files"] == []
    # The other task is unaffected: one bad report does not lose a wave.
    assert tasks["T-002"]["ok"] is True


def test_a_blocked_agent_is_not_counted_as_done(wave_state, stub):
    stub.set_reply(
        "task-T-001",
        AgentResult(
            ok=True,
            text=json.dumps(
                {
                    "status": "blocked",
                    "summary": "",
                    "files_changed": [],
                    "blocked_reason": "No database URL",
                }
            ),
        ),
    )
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)
    tasks = {t["task_id"]: t for t in result["wave_results"][0]["tasks"]}
    assert tasks["T-001"]["ok"] is False
    assert "No database URL" in tasks["T-001"]["report"]


def test_a_failed_agents_partial_work_is_still_committed(wave_state, stub):
    """A rework starts from whatever the previous attempt actually left."""

    def half_then_fail(prompt: str) -> AgentResult:
        marker = "## Working directory\n\n"
        start = prompt.index(marker) + len(marker)
        workdir = __import__("pathlib").Path(prompt[start:].splitlines()[0].strip())
        (workdir / "half.py").write_text("# started\n", encoding="utf-8")
        return AgentResult(ok=False, error_kind="timeout", error_message="ran past 900s")

    stub.set_reply("task-T-001", half_then_fail)
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)

    tasks = {t["task_id"]: t for t in result["wave_results"][0]["tasks"]}
    assert tasks["T-001"]["ok"] is False
    assert tasks["T-001"]["changed_files"] == ["half.py"]
    assert len(tasks["T-001"]["commit"]) == 40
    assert "committed on its branch" in tasks["T-001"]["report"]


def test_a_wave_where_everything_failed_stops_the_run(wave_state, stub):
    """Never hand the merger an empty wave and call the run clean."""
    stub.set_reply("task-*", AgentResult(ok=False, error_kind="rate_limit", error_message="limit"))

    result = wave_orchestrator_node(wave_state)

    assert result["status"] == "failed"
    assert "Every task in wave 0 failed" in result["stop_reason"]
    assert "rate_limit" in result["stop_reason"]


def test_one_failure_does_not_lose_the_rest_of_the_wave(wave_state, stub):
    stub.set_reply("task-T-001", AgentResult(ok=False, error_kind="timeout", error_message="slow"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)

    assert result.get("status") != "failed"
    tasks = {t["task_id"]: t for t in result["wave_results"][0]["tasks"]}
    assert tasks["T-002"]["ok"] is True


# ── Rework ───────────────────────────────────────────────────────────────────


def test_rework_reaches_the_same_agent_session(wave_state, stub):
    """"Here is what you got wrong" has to refer to something it remembers."""
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))
    first = wave_orchestrator_node(wave_state)

    reworked = {
        **wave_state,
        **first,
        "wave_results": first["wave_results"],
        "review_verdict": "rework",
        "review_comments": "The endpoint returns plain text, not JSON.",
    }
    stub.calls.clear()
    wave_orchestrator_node(reworked)

    calls = {call["tag"]: call for call in stub.calls}
    assert calls["task-T-001"]["resume_session"] == "sess-app/health.py"
    assert "The endpoint returns plain text, not JSON." in calls["task-T-001"]["prompt"]
    assert "This is a rework" in calls["task-T-001"]["prompt"]


def test_rework_discards_the_rejected_worktrees(wave_state, stub, git_repo):
    """The notes: the worktree's work is reverted/deleted, and the process cycles."""
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))
    first = wave_orchestrator_node(wave_state)
    rejected_path = first["active_worktrees"][0]["worktree"]
    assert __import__("pathlib").Path(rejected_path).exists()

    reworked = {
        **wave_state,
        **first,
        "review_verdict": "rework",
        "review_comments": "Wrong content type.",
    }
    second = wave_orchestrator_node(reworked)

    # A fresh worktree, branched from the integration branch again — not the
    # rejected one with the agent's previous attempt still in it.
    new_path = second["active_worktrees"][0]["worktree"]
    assert not (__import__("pathlib").Path(new_path) / "REJECTED").exists()
    assert second["wave_results"][0]["attempt"] == 1


def test_the_rework_flag_is_cleared_so_the_next_wave_is_not_treated_as_one(wave_state, stub):
    stub.set_reply("task-*", writing_agent("out.txt"))
    result = wave_orchestrator_node({**wave_state, "review_verdict": "rework", "review_comments": "x"})
    assert result["review_verdict"] is None
    assert result["review_comments"] == ""


def test_a_first_attempt_does_not_resume_any_session(wave_state, stub):
    stub.set_reply("task-*", writing_agent("out.txt"))
    wave_orchestrator_node(wave_state)
    assert all(call["resume_session"] == "" for call in stub.calls)


# ── Wave bookkeeping ─────────────────────────────────────────────────────────


def test_a_wave_index_past_the_plan_stops_the_run(wave_state, stub):
    result = wave_orchestrator_node({**wave_state, "current_wave": 5})
    assert result["status"] == "failed"
    assert "the plan has 1" in result["stop_reason"]


def test_a_wave_naming_unknown_tasks_stops_the_run(wave_state, stub):
    result = wave_orchestrator_node({**wave_state, "waves": [["T-999"]]})
    assert result["status"] == "failed"
    assert "none of them is in the plan" in result["stop_reason"]


def test_a_later_wave_branches_from_what_has_been_integrated(wave_state, stub, git_repo):
    """Wave N builds on the merger's output, not on the repository's HEAD."""
    branch, _ = wt.ensure_integration_branch(git_repo, "r1")
    wt.git(git_repo, "checkout", branch)
    (git_repo / "from_wave_0.txt").write_text("merged earlier\n", encoding="utf-8")
    wt.commit_all(git_repo, "wave 0 integration")

    stub.set_reply("task-T-003", writing_agent("later.txt"))
    later = {
        **wave_state,
        "tasks": wave_state["tasks"] + [{**TASKS[0], "task_id": "T-003", "wave": 1}],
        "waves": [["T-001", "T-002"], ["T-003"]],
        "current_wave": 1,
    }

    result = wave_orchestrator_node(later)

    worktree = __import__("pathlib").Path(result["active_worktrees"][0]["worktree"])
    assert (worktree / "from_wave_0.txt").exists()


def test_shared_workspace_mode_runs_in_the_target_repo(wave_state, stub, git_repo, monkeypatch):
    monkeypatch.setattr("config.USE_GIT_WORKTREES", False)
    stub.set_reply("task-*", writing_agent("out.txt"))

    result = wave_orchestrator_node(wave_state)

    assert all(t["worktree"] == str(git_repo) for t in result["active_worktrees"])
    assert (git_repo / "out.txt").exists()


# ── Coding-agent roster ──────────────────────────────────────────────────────


def test_the_default_pair_is_used_when_the_plan_named_no_roster(wave_state, stub):
    stub.set_reply("task-*", writing_agent("out.txt"))

    wave_orchestrator_node(wave_state)

    calls = {call["tag"]: call for call in stub.calls}
    # T-001 is index 0 in wave_state["tasks"], T-002 is index 1 — round-robin is
    # keyed off that stable index, not off completion order under the thread pool.
    assert calls["task-T-001"]["spec"].backend == config.CODING_AGENT_A.backend
    assert calls["task-T-002"]["spec"].backend == config.CODING_AGENT_B.backend


def test_the_planners_roster_is_used_when_present(wave_state, stub):
    stub.set_reply("task-*", writing_agent("out.txt"))
    roster = [{"backend": "claude", "model": "haiku"}, {"backend": "codex", "model": ""}]

    wave_orchestrator_node({**wave_state, "coding_agents": roster})

    calls = {call["tag"]: call for call in stub.calls}
    assert calls["task-T-001"]["spec"].backend == "claude"
    assert calls["task-T-001"]["spec"].model == "haiku"
    assert calls["task-T-002"]["spec"].backend == "codex"
    assert calls["task-T-002"]["spec"].model == ""


def test_a_roster_of_one_puts_every_task_on_the_same_agent(wave_state, stub):
    stub.set_reply("task-*", writing_agent("out.txt"))

    wave_orchestrator_node({**wave_state, "coding_agents": [{"backend": "codex", "model": ""}]})

    assert all(call["spec"].backend == "codex" for call in stub.calls)


def test_the_roster_offset_advances_across_waves_instead_of_resetting(git_repo, tmp_path, stub):
    """A bare per-wave index would let wave 2 land back on wave 1's slots.

    A 5-slot roster with 2 expert-tier entries first and 3 small/medium after
    should give wave 0 (2 tasks) the two expert entries and wave 1 (3 tasks) the
    three small/medium entries that follow — not the first three entries
    overall, which would repeat the two experts.
    """
    artifacts = art.prepare("r2", git_repo)
    art.write_text(artifacts.context, "# Context\nRoster offset test.")

    state = initial_state(
        run_id="r2",
        goal="roster offset test",
        target_repo=str(git_repo),
    )
    state["tasks"] = [
        {**TASKS[0], "task_id": "T-001", "wave": 0},
        {**TASKS[0], "task_id": "T-002", "wave": 0},
        {**TASKS[0], "task_id": "T-003", "wave": 1},
        {**TASKS[0], "task_id": "T-004", "wave": 1},
        {**TASKS[0], "task_id": "T-005", "wave": 1},
    ]
    state["waves"] = [["T-001", "T-002"], ["T-003", "T-004", "T-005"]]
    state["context"] = "# Context\nRoster offset test."
    state["coding_agents"] = [
        {"backend": "claude", "model": "sonnet"},
        {"backend": "codex", "model": ""},
        {"backend": "claude", "model": "haiku"},
        {"backend": "claude", "model": "haiku"},
        {"backend": "claude", "model": "haiku"},
    ]

    stub.set_reply("task-*", writing_agent("out.txt"))
    first = wave_orchestrator_node(state)

    calls = {call["tag"]: call for call in stub.calls}
    assert calls["task-T-001"]["spec"].model == "sonnet"
    assert calls["task-T-002"]["spec"].backend == "codex"

    stub.calls.clear()
    wave_orchestrator_node({**state, **first, "current_wave": 1})

    calls = {call["tag"]: call for call in stub.calls}
    assert calls["task-T-003"]["spec"].model == "haiku"
    assert calls["task-T-004"]["spec"].model == "haiku"
    assert calls["task-T-005"]["spec"].model == "haiku"


# ── The coding-turn finish contract ──────────────────────────────────────────
#
# Bugs.md #21/#23: a coding subagent's own CLI ends its turn the instant it
# replies, tool call or not, which lets a plan-out-loud message like "Now I
# need to update X" become the whole turn. These tests cover the brief itself
# (CODING_FRAME bans narration-only replies and defines finish_reason) and the
# one call site that must ask adapters.run_agent() to enforce it. The guard
# itself lives in run_agent(), not in any one vendor adapter, so it applies no
# matter which backend a coding subagent is dispatched to — proved here on the
# plain stub transport, which every backend flows through identically.


def test_the_brief_forbids_a_narration_only_reply(wave_state, stub):
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    wave_orchestrator_node(wave_state)

    system_prompt = stub.calls[0]["system_prompt"]
    assert "Never send a message that contains only narration" in system_prompt
    assert "same message as the tool call" in system_prompt


def test_the_brief_defines_finish_reason_and_ties_length_to_a_truncated_reply(wave_state, stub):
    stub.set_reply("task-T-001", writing_agent("app/health.py"))
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    wave_orchestrator_node(wave_state)

    system_prompt = stub.calls[0]["system_prompt"]
    assert '"finish_reason": "stop" | "length"' in system_prompt
    # The field exists so a truncated tool_call block (max_tokens too small)
    # is reported as an honest "blocked", not misread as a finished "done".
    assert '"length"' in system_prompt
    assert "almost out of room to respond" in system_prompt


def test_a_narration_only_reply_is_nudged_regardless_of_which_backend_replied(wave_state, stub):
    """The continuation-nudge guard is not Codex-specific: it lives in
    adapters.run_agent() itself, so it fires on whichever backend a coding
    subagent happens to be dispatched to. Proved here on the plain stub
    transport — no codex.py involved at all — by scripting a narration-only
    first reply and a proper status JSON second reply, and checking the task
    dispatch calls the backend twice rather than accepting the narration."""
    workdir_holder: dict[str, str] = {}
    replies: list[str] = []

    def reply(prompt: str) -> AgentResult:
        replies.append(prompt)
        if len(replies) == 1:
            marker = "## Working directory\n\n"
            start = prompt.index(marker) + len(marker)
            workdir_holder["path"] = prompt[start:].splitlines()[0].strip()
            return AgentResult(
                ok=True, text="Now I need to update app/health.py.", session_id="sess-nudge"
            )
        target = __import__("pathlib").Path(workdir_holder["path"]) / "app/health.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("written by the stub\n", encoding="utf-8")
        return AgentResult(
            ok=True,
            text=json.dumps(
                {"status": "done", "summary": "Created app/health.py", "files_changed": ["app/health.py"]}
            ),
            session_id="sess-nudge",
        )

    stub.set_reply("task-T-001", reply)
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    result = wave_orchestrator_node(wave_state)

    assert len(replies) == 2
    assert "final status JSON" in replies[1]
    tasks = {t["task_id"]: t for t in result["wave_results"][0]["tasks"]}
    assert tasks["T-001"]["ok"] is True


def test_the_finish_guard_is_switched_off_through_config_alone(wave_state, stub, monkeypatch):
    """dispatch.py never hardcodes the guard on: it reads
    config.ENABLE_CODING_AGENT_FINISH_GUARD, so flipping that one setting is
    the only way to turn the guard off, with no code change anywhere else.
    With it False, a narration-only reply must be accepted as the task's
    result on the first call — no nudge, no second call."""
    monkeypatch.setattr("config.ENABLE_CODING_AGENT_FINISH_GUARD", False)
    replies: list[str] = []

    def reply(prompt: str) -> AgentResult:
        replies.append(prompt)
        return AgentResult(
            ok=True, text="Now I need to update app/health.py.", session_id="sess-off"
        )

    stub.set_reply("task-T-001", reply)
    stub.set_reply("task-T-002", writing_agent("NOTES.md"))

    wave_orchestrator_node(wave_state)

    assert len(replies) == 1
