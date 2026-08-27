"""The layer every agent stands on: adapters, artifacts, worktrees, state.

None of these tests reaches a real coding-agent CLI. The worktree tests do reach
real git, because the behaviour under test *is* git's.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import adapters
import artifacts as art
import worktrees as wt
import config
from adapters.base import AgentResult, _dispatch_key, classify_failure, subprocess_env
from adapters.copilot import _parse_json_lines
from adapters.codex import _codex_failure, _thread_id
from config import AgentSpec
from state import event, initial_state

SPEC = AgentSpec(backend="claude", model="haiku", deadline_seconds=30)


# ── Adapter contract ─────────────────────────────────────────────────────────


def test_every_transport_is_registered():
    registered = adapters.available_backends()
    assert "stub" in registered
    assert "direct:claude" in registered
    assert "direct:codex" in registered
    assert "direct:copilot" in registered
    assert "direct:ollama" in registered
    assert "direct:local_llm" in registered
    assert "maestro" in registered


def test_dispatch_key_separates_transport_from_vendor():
    assert _dispatch_key("direct", "claude") == "direct:claude"
    assert _dispatch_key("direct", "codex") == "direct:codex"
    assert _dispatch_key("direct", "copilot") == "direct:copilot"
    assert _dispatch_key("direct", "local_llm") == "direct:local_llm"
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


def test_ollama_adapter_uses_codex_local_provider(monkeypatch, tmp_path):
    model = "qwen2.5-coder:14b-instruct-q4_K_M"
    captured: dict[str, object] = {}

    def fake_run_with_deadline(argv, *, input, cwd, timeout):
        captured.update(argv=argv, input=input, cwd=cwd, timeout=timeout)
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text("OLLAMA_ADAPTER_OK", encoding="utf-8")
        stdout = '{"type":"thread.started","thread_id":"ollama-session"}\n'
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr("adapters.codex.run_with_deadline", fake_run_with_deadline)
    result = adapters.run_agent(
        "Return the requested marker.",
        spec=AgentSpec(backend="ollama", model=model, deadline_seconds=30),
        cwd=str(tmp_path),
        tag="ollama-probe",
        invocation="direct",
    )

    argv = captured["argv"]
    assert result.ok is True
    assert result.text == "OLLAMA_ADAPTER_OK"
    assert result.session_id == "ollama-session"
    assert argv[argv.index("--model") + 1] == model
    assert argv[argv.index("--local-provider") + 1] == "ollama"
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=none"
    assert "--oss" in argv
    assert "--disable" not in argv


def test_local_llm_adapter_uses_codex_custom_responses_provider(monkeypatch):
    captured: dict[str, object] = {}
    model = "local/model"
    secret = "must-not-appear-in-argv"
    monkeypatch.setattr("config.CUSTOM_API_BASE", "http://local.test/v1/")
    monkeypatch.setattr("config.CUSTOM_API_KEY", secret)
    monkeypatch.setattr("config.CUSTOM_API_MODEL_NAME", model)
    monkeypatch.setattr("adapters.local_llm._advertised_wire_api", lambda *_: "responses")

    # A status-JSON reply (not bare narration) so the adapter's finish-condition
    # guard accepts this as a completed turn on the first call, keeping this a
    # single-dispatch probe of argv construction rather than of the guard itself.
    reply = '{"status": "done", "files_changed": ["marker"], "summary": "LOCAL_LLM_ADAPTER_OK"}'

    def fake_run_with_deadline(argv, *, input, cwd, timeout):
        captured.update(argv=argv, input=input, cwd=cwd, timeout=timeout)
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(reply, encoding="utf-8")
        stdout = '{"type":"thread.started","thread_id":"local-session"}\n'
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr("adapters.codex.run_with_deadline", fake_run_with_deadline)
    result = adapters.run_agent(
        "Return the requested marker.",
        spec=AgentSpec(backend="local_llm", model="", deadline_seconds=30),
        cwd=os.getcwd(),
        tag="local-llm-probe",
        invocation="direct",
        resume_session="previous-local-session",
    )

    argv = captured["argv"]
    overrides = [argv[index + 1] for index, value in enumerate(argv) if value == "-c"]
    assert result.ok is True
    assert result.text == reply
    assert result.session_id == "local-session"
    assert argv[argv.index("--model") + 1] == model
    assert 'model_provider="local_llm"' in overrides
    assert 'model_providers.local_llm.base_url="http://local.test/v1"' in overrides
    assert 'model_providers.local_llm.env_key="CUSTOM_API_KEY"' in overrides
    assert 'model_providers.local_llm.wire_api="responses"' in overrides
    assert argv[argv.index("--disable") + 1] == "memories"
    assert argv[argv.index("resume") + 1] == "previous-local-session"
    assert argv.index("--disable") < argv.index("resume")
    assert "--oss" not in argv
    assert "--local-provider" not in argv
    assert secret not in " ".join(argv)


def test_local_llm_adapter_is_nudged_by_the_generic_completion_guard(monkeypatch):
    """The continuation-nudge guard (#21/#23) lives in adapters.run_agent(),
    not in any one vendor adapter, so it applies to the local-model backend too
    — the one most prone to plan-out-loud replies — even though this adapter
    itself has no idea the guard exists. Prove the reply is nudged, not
    accepted, on a first narration-only turn."""
    monkeypatch.setattr("config.CUSTOM_API_BASE", "http://local.test/v1/")
    monkeypatch.setattr("config.CUSTOM_API_KEY", "secret")
    monkeypatch.setattr("config.CUSTOM_API_MODEL_NAME", "local/model")
    monkeypatch.setattr("adapters.local_llm._advertised_wire_api", lambda *_: "responses")

    replies = [
        "Now I need to update test_calc.py.",
        '{"status": "done", "files_changed": ["test_calc.py"], "summary": "added tests"}',
    ]
    calls: list[dict[str, object]] = []

    def fake_run_with_deadline(argv, *, input, cwd, timeout):
        calls.append({"argv": argv, "input": input})
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(replies[len(calls) - 1], encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 0, '{"type":"thread.started","thread_id":"local-sess"}\n', ""
        )

    monkeypatch.setattr("adapters.codex.run_with_deadline", fake_run_with_deadline)
    result = adapters.run_agent(
        "Do the task.",
        spec=AgentSpec(backend="local_llm", model="", deadline_seconds=30),
        cwd=os.getcwd(),
        tag="local-llm-nudge-probe",
        invocation="direct",
        expects_status_json=True,
    )

    assert len(calls) == 2
    assert result.ok is True
    assert result.text == replies[1]
    second_argv = calls[1]["argv"]
    assert second_argv[second_argv.index("resume") + 1] == "local-sess"


def test_ollama_adapter_is_nudged_by_the_generic_completion_guard(monkeypatch):
    """Same as the local_llm case above: the guard runs a layer above this
    adapter, in adapters.run_agent(), so Ollama's small/medium models — also
    among the most prone to plan-out-loud replies — get it for free too."""
    replies = [
        "Now I need to update test_calc.py.",
        '{"status": "done", "files_changed": ["test_calc.py"], "summary": "added tests"}',
    ]
    calls: list[dict[str, object]] = []

    def fake_run_with_deadline(argv, *, input, cwd, timeout):
        calls.append({"argv": argv, "input": input})
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(replies[len(calls) - 1], encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 0, '{"type":"thread.started","thread_id":"ollama-sess"}\n', ""
        )

    monkeypatch.setattr("adapters.codex.run_with_deadline", fake_run_with_deadline)
    result = adapters.run_agent(
        "Do the task.",
        spec=AgentSpec(backend="ollama", model="qwen3.5:4b", deadline_seconds=30),
        cwd=os.getcwd(),
        tag="ollama-nudge-probe",
        invocation="direct",
        expects_status_json=True,
    )

    assert len(calls) == 2
    assert result.ok is True
    assert result.text == replies[1]
    second_argv = calls[1]["argv"]
    assert second_argv[second_argv.index("resume") + 1] == "ollama-sess"


def test_local_llm_adapter_bridges_chat_completions_only_server(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr("config.CUSTOM_API_BASE", "http://local.test/v1")
    monkeypatch.setattr("config.CUSTOM_API_KEY", "secret")
    monkeypatch.setattr("config.CUSTOM_API_MODEL_NAME", "local/model")
    monkeypatch.setattr("adapters.local_llm._advertised_wire_api", lambda *_: "chat_completions")

    class FakeBridge:
        def __enter__(self):
            captured["bridge_started"] = True
            return "http://127.0.0.1:54321/v1"

        def __exit__(self, *_):
            captured["bridge_stopped"] = True

    monkeypatch.setattr("adapters.local_llm.responses_bridge", lambda *_: FakeBridge())

    def fake_run_with_deadline(argv, *, input, cwd, timeout):
        captured["argv"] = argv
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text("BRIDGED", encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 0, '{"type":"thread.started","thread_id":"bridge-session"}\n', ""
        )

    monkeypatch.setattr("adapters.codex.run_with_deadline", fake_run_with_deadline)

    result = adapters.run_agent(
        "Return OK.",
        spec=AgentSpec(backend="local_llm", model="", deadline_seconds=30),
        cwd=os.getcwd(),
        tag="local-llm-incompatible",
        invocation="direct",
    )

    overrides = [
        captured["argv"][index + 1]
        for index, value in enumerate(captured["argv"])
        if value == "-c"
    ]
    assert result.ok is True
    assert result.text == "BRIDGED"
    assert captured["bridge_started"] is True
    assert captured["bridge_stopped"] is True
    assert 'model_providers.local_llm.base_url="http://127.0.0.1:54321/v1"' in overrides
    assert captured["argv"][captured["argv"].index("--disable") + 1] == "memories"


def test_copilot_adapter_builds_noninteractive_command(monkeypatch):
    captured: dict[str, object] = {}
    cwd = os.getcwd()
    run_dir = str(Path.cwd() / "orchestrator" / "runs")

    def fake_run_with_deadline(argv, *, input, cwd, timeout):
        captured.update(argv=argv, input=input, cwd=cwd, timeout=timeout)
        stdout = "\n".join(
            [
                '{"type":"session","session_id":"copilot-session"}',
                '{"type":"message","content":"COPILOT_ADAPTER_OK"}',
            ]
        )
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr("adapters.copilot.run_with_deadline", fake_run_with_deadline)
    result = adapters.run_agent(
        "Return the requested marker.",
        spec=AgentSpec(backend="copilot", model="auto", deadline_seconds=30),
        system_prompt="System rules.",
        cwd=cwd,
        tag="copilot-probe",
        invocation="direct",
        resume_session="previous-session",
        extra_dirs=(run_dir,),
    )

    argv = captured["argv"]
    assert result.ok is True
    assert result.text == "COPILOT_ADAPTER_OK"
    assert result.session_id == "copilot-session"
    assert captured["input"] is None
    assert captured["cwd"] == cwd
    assert argv[argv.index("-C") + 1] == cwd
    assert argv[argv.index("--model") + 1] == "auto"
    assert argv[argv.index("--add-dir") + 1] == run_dir
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--allow-all-tools" in argv
    assert "--no-ask-user" in argv
    assert "--resume=previous-session" in argv
    assert "System rules." in argv[argv.index("-p") + 1]


def test_copilot_json_parser_preserves_session_and_last_reply():
    stdout = "\n".join(
        [
            '{"type":"session","session":{"id":"abc-123"}}',
            '{"type":"message","content":[{"type":"text","text":"draft"}]}',
            '{"type":"message","content":[{"type":"text","text":"final"}]}',
        ]
    )
    assert _parse_json_lines(stdout) == ("final", "abc-123", "")


def test_copilot_json_parser_reads_nested_final_assistant_message():
    stdout = "\n".join(
        [
            '{"type":"user.message","data":{"content":"ignore this prompt"}}',
            '{"type":"assistant.message_delta","data":{"deltaContent":"draft"}}',
            '{"type":"assistant.message","data":{"content":"final reply"}}',
            '{"type":"result","sessionId":"session-123","exitCode":0}',
        ]
    )
    assert _parse_json_lines(stdout) == ("final reply", "session-123", "")


def test_copilot_stderr_only_failure_is_not_reported_as_no_output(monkeypatch):
    message = (
        "Error: Authentication token found but could not be validated.\n\n"
        "  Failed to fetch OAuth user login (503): GitHub returned: No server is currently available."
    )

    def fake_run_with_deadline(argv, *, input, cwd, timeout):
        return subprocess.CompletedProcess(argv, 0, "", message)

    monkeypatch.setattr("adapters.copilot.run_with_deadline", fake_run_with_deadline)
    result = adapters.run_agent(
        "Return OK.",
        spec=AgentSpec(backend="copilot", model="auto", deadline_seconds=30),
        cwd=os.getcwd(),
        tag="copilot-auth-failure",
        invocation="direct",
    )

    assert result.ok is False
    assert result.error_kind == "agent_error"
    assert "Authentication token found" in result.error_message
    assert "503" in result.error_message


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


def test_codex_failure_uses_the_structured_turn_error():
    stdout = "\n".join(
        [
            '{"type":"item.completed","item":{"type":"error","message":"warning"}}',
            '{"type":"error","message":"upstream rejected the request"}',
            '{"type":"turn.failed","error":{"message":"System message must be at the beginning."}}',
        ]
    )

    assert _codex_failure(stdout) == "System message must be at the beginning."


def test_codex_session_id_is_absent_rather_than_invented():
    assert _thread_id("") == ""
    assert _thread_id('{"type":"turn.completed"}') == ""


def test_codex_nudges_a_narration_only_reply_until_the_status_json_arrives(monkeypatch):
    """The guard from #21/#23: a reply with no tool call ends Codex's own turn
    (CODING_FRAME says so verbatim), which can strand the agent one message short
    of the required status JSON. adapters.run_agent() must resume that same
    session with a continuation nudge rather than accepting the narration as a
    finished turn — exercised here through the codex backend, but the loop
    itself is generic (see the local_llm/ollama tests above for the same guard
    on other backends)."""
    replies = [
        "Good, learnings.md is created. Now I need to update test_calc.py.",
        '{"status": "done", "files_changed": ["test_calc.py"], "summary": "added tests"}',
    ]
    calls: list[dict[str, object]] = []

    def fake_run_with_deadline(argv, *, input, cwd, timeout):
        calls.append({"argv": argv, "input": input})
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(replies[len(calls) - 1], encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 0, '{"type":"thread.started","thread_id":"sess-1"}\n', ""
        )

    monkeypatch.setattr("adapters.codex.run_with_deadline", fake_run_with_deadline)
    result = adapters.run_agent(
        "Do the task.",
        spec=AgentSpec(backend="codex", model="", deadline_seconds=30),
        system_prompt="System rules.",
        cwd=os.getcwd(),
        tag="codex-nudge-probe",
        invocation="direct",
        expects_status_json=True,
    )

    assert len(calls) == 2
    assert result.ok is True
    assert result.text == replies[1]
    assert result.session_id == "sess-1"
    second_argv = calls[1]["argv"]
    assert second_argv[second_argv.index("resume") + 1] == "sess-1"
    assert calls[1]["input"] == "Continue — you have not sent the final status JSON yet. Finish the task, then reply with the required JSON object and nothing else."


def test_codex_gives_up_after_max_continuation_attempts(monkeypatch):
    """A model that never produces the status JSON must not be nudged forever."""
    calls: list[dict[str, object]] = []

    def fake_run_with_deadline(argv, *, input, cwd, timeout):
        calls.append({"argv": argv, "input": input})
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text("still narrating", encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 0, '{"type":"thread.started","thread_id":"sess-2"}\n', ""
        )

    monkeypatch.setattr("adapters.codex.run_with_deadline", fake_run_with_deadline)
    result = adapters.run_agent(
        "Do the task.",
        spec=AgentSpec(backend="codex", model="", deadline_seconds=30),
        cwd=os.getcwd(),
        tag="codex-nudge-giveup-probe",
        invocation="direct",
        expects_status_json=True,
    )

    # One first attempt plus config.MAX_CODING_AGENT_CONTINUATION_ATTEMPTS nudges, then give up.
    assert len(calls) == config.MAX_CODING_AGENT_CONTINUATION_ATTEMPTS + 1
    assert result.ok is True
    assert result.text == "still narrating"


def test_codex_does_not_nudge_a_reply_outside_the_coding_frame(monkeypatch):
    """Reviewer/supervisor turns may also be Codex-backed but reply in their
    own shape, never CODING_FRAME's {status, files_changed, ...}. Without
    `expects_status_json`, adapters.run_agent() must accept a narration-shaped
    reply as-is rather than endlessly nudging it toward a contract it never
    had."""
    calls: list[dict[str, object]] = []

    def fake_run_with_deadline(argv, *, input, cwd, timeout):
        calls.append({"argv": argv, "input": input})
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text('{"verdict": "approved"}', encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 0, '{"type":"thread.started","thread_id":"sess-3"}\n', ""
        )

    monkeypatch.setattr("adapters.codex.run_with_deadline", fake_run_with_deadline)
    result = adapters.run_agent(
        "Review the wave.",
        spec=AgentSpec(backend="codex", model="", deadline_seconds=30),
        cwd=os.getcwd(),
        tag="codex-reviewer-probe",
        invocation="direct",
    )

    assert len(calls) == 1
    assert result.ok is True
    assert result.text == '{"verdict": "approved"}'


def test_subprocess_env_removes_python_overrides(monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "C:/bad-python")
    monkeypatch.setenv("PYTHONPATH", "C:/bad-python/site-packages")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setattr("config.CUSTOM_API_BASE", "http://192.168.1.11:3001/v1")

    env = subprocess_env()

    local_bin = str(config.PROJECT_ROOT / "node_modules" / ".bin")

    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PATH"].split(os.pathsep)[0] == local_bin
    assert env["NO_PROXY"].split(",") == ["localhost", "192.168.1.11"]
    assert "192.168.1.11" in env["no_proxy"].split(",")


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
    target = tmp_path / "target"
    target.mkdir()
    a = art.prepare("r1", target)
    assert a.root.is_dir()
    assert a.reviews_dir.is_dir()
    assert a.context.name == "context.md"
    assert a.user_choices.name == "user_choices.md"


def test_project_artifacts_use_category_paths_outside_the_target_repo(tmp_path):
    target = tmp_path / "target"
    target.mkdir()

    a = art.prepare("run-001", target)

    root = wt.artifact_root(target)
    assert a.root == root
    assert a.shared_dir == root / "shared"
    assert a.context == root / "shared" / "context.md"
    assert a.learnings == root / "shared" / "learnings.md"
    assert a.user_choices == root / "shared" / "user_choices.md"

    assert a.plan == root / "records" / "plans" / "run-001.json"
    assert a.events == root / "records" / "events" / "run-001.jsonl"
    assert a.task_file("T-001").parent == root / "records" / "tasks" / "run-001"
    assert a.reviews_dir == root / "records" / "reviews" / "run-001"

    for readable in (
        a.context,
        a.learnings,
        a.learnings_lock,
        a.user_choices,
        a.events,
    ):
        assert readable.is_file()
        assert readable.read_text(encoding="utf-8") == ""

    # Structured stage outputs have no honest empty representation. Their
    # directories exist, but files appear only when the owning stage writes.
    assert not a.plan.exists()
    assert a.task_files() == []
    assert list(a.reviews_dir.iterdir()) == []

    art.append_learning(a, "planner", "shared across runs")
    assert a.learnings_lock == root / "shared" / "learnings.md.lock"

    # The target's own working tree is never touched.
    assert not (target / "runs").exists()


def test_multiple_runs_resolve_to_the_same_project_shared_artifacts(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    first = art.prepare("run-001", target)
    second = art.prepare("run-002", target)

    assert first.context == second.context
    assert first.learnings == second.learnings
    assert first.user_choices == second.user_choices
    assert first.plan != second.plan

    art.append_learning(first, "run-001", "first finding")
    art.append_learning(second, "run-002", "second finding")
    body = art.read_text(second.learnings)
    assert "first finding" in body
    assert "second finding" in body


def test_two_targets_sharing_a_basename_do_not_collide(tmp_path):
    first_target = tmp_path / "a" / "target"
    second_target = tmp_path / "b" / "target"
    first_target.mkdir(parents=True)
    second_target.mkdir(parents=True)

    first = art.prepare("run-001", first_target)
    second = art.prepare("run-001", second_target)

    assert first.root != second.root
    art.write_text(first.context, "first project")
    art.write_text(second.context, "second project")
    assert art.read_text(first.context) == "first project"
    assert art.read_text(second.context) == "second project"


def test_user_choices_are_append_only_and_idempotent_per_run(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    first = art.prepare("run-001", target)
    second = art.prepare("run-002", target)

    art.append_user_choices(first, "run-001", "## Run one\n\nUse PostgreSQL.")
    art.append_user_choices(first, "run-001", "## Duplicate replay")
    art.append_user_choices(second, "run-002", "## Run two\n\nKeep the API public.")

    body = art.read_text(first.user_choices)
    assert body.count("# User choices") == 1
    assert body.count("<!-- run:run-001 -->") == 1
    assert body.count("<!-- run:run-002 -->") == 1
    assert "Use PostgreSQL." in body
    assert "Keep the API public." in body
    assert "Duplicate replay" not in body
    assert first.learnings_lock.is_file()


def test_prepare_migrates_shared_memory_from_the_legacy_in_repo_runs_dir(tmp_path):
    target = tmp_path / "target"
    legacy = target / "runs"
    (legacy / "reviews").mkdir(parents=True)
    art.write_text(legacy / "context.md", "legacy context")
    art.write_text(legacy / "user_choices.md", "legacy choices")
    art.write_text(legacy / "learnings.md", "legacy learnings")
    # Pre-ADR-037 per-run records. These are deliberately left in place, not
    # reshuffled into the new run's record tree.
    art.write_json(legacy / "plans" / "old-run.json", {"summary": "legacy plan"})
    art.write_text(legacy / "reviews" / "wave-00-attempt-00.md", "legacy review")

    a = art.prepare("run-001", target)

    assert art.read_text(a.context) == "legacy context"
    assert art.read_text(a.user_choices) == "legacy choices"
    assert art.read_text(a.learnings) == "legacy learnings"
    # Old per-run records are untouched — neither deleted nor copied.
    assert (legacy / "plans" / "old-run.json").is_file()
    assert not a.plan.exists()

    art.write_text(a.context, "new context")
    art.prepare("run-001", target)
    assert art.read_text(a.context) == "new context"


def test_prepare_does_not_migrate_a_runs_dir_the_project_already_owns(tmp_path):
    """A target that already has its own `runs/` (unrelated content) must be
    left alone — this is a real collision observed in practice, not a
    hypothetical (a sibling project's own dry-run logs)."""
    target = tmp_path / "target"
    legacy = target / "runs"
    legacy.mkdir(parents=True)
    art.write_text(legacy / "application-owned.txt", "not an orchestrator artifact")

    a = art.prepare("run-001", target)

    assert art.read_text(a.context) == ""
    assert (legacy / "application-owned.txt").is_file()
    assert not (legacy / "context.md").exists()


def test_prepare_does_not_overwrite_existing_empty_capable_artifacts(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    first = art.prepare("run-001", target)
    art.write_text(first.context, "current context")
    art.write_text(first.learnings, "existing learnings")
    art.write_text(first.user_choices, "existing choices")
    art.write_text(first.events, '{"kind":"existing"}\n')

    second = art.prepare("run-001", target)

    assert art.read_text(second.context) == "current context"
    assert art.read_text(second.learnings) == "existing learnings"
    assert art.read_text(second.user_choices) == "existing choices"
    assert art.read_text(second.events) == '{"kind":"existing"}\n'


def test_prepare_never_writes_inside_the_target_working_tree(git_repo):
    a = art.prepare("run-001", git_repo)
    art.write_text(a.context, "context")
    art.append_learning(a, "planner", "finding")
    art.append_user_choices(a, "run-001", "## Choices\n\nUse JSON.")
    art.write_json(a.plan, {"summary": "plan"})
    art.write_json(a.task_file("T-001"), {"task_id": "T-001"})
    art.write_text(a.review_file(0, 0), "review")
    art.append_event(a, {"kind": "started"})

    project_owned = git_repo / "runs" / "application-owned.txt"
    project_owned.parent.mkdir(parents=True, exist_ok=True)
    project_owned.write_text("not an orchestrator artifact\n", encoding="utf-8")

    status = wt.git(git_repo, "status", "--porcelain", "--untracked-files=all")
    assert status.ok
    # The project's own file is untouched and still shows as untracked.
    assert "runs/application-owned.txt" in status.stdout.replace("\\", "/")
    # Nothing this module wrote appears in the target's own git status at all.
    for generated in (
        "context.md",
        "learnings.md",
        "learnings.md.lock",
        "user_choices.md",
        "plan.json",
        "TASK-",
        "review",
    ):
        assert generated not in status.stdout


def test_task_ids_are_sanitised_into_paths_and_branches():
    """The planner is a model; its output is untrusted input here."""
    assert art.safe_id("T-001") == "T-001"
    assert art.safe_id("../../etc/passwd") == "etc-passwd"
    assert art.safe_id("feat/add auth") == "feat-add-auth"
    assert art.safe_id("") == "task"


def test_reading_a_missing_artifact_reports_rather_than_raises(run_dir):
    # context.md is eagerly created empty by prepare(); a genuinely absent
    # artifact is one of the lazy ones, like a review that hasn't run yet.
    missing = run_dir.review_file(0, 0)
    assert art.read_text(missing) == ""
    assert art.read_text(missing, default="none yet") == "none yet"
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
            [sys.executable, script, "append-learning", str(run_dir.shared_dir), f"task-{i}", f"finding {i}"],
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
        [sys.executable, script, "append-learning", str(run_dir.shared_dir), "task-T-001", "found a thing"],
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
    s = initial_state(run_id="r", goal="g", target_repo="/t")
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


def test_the_orchestrator_side_commit_excludes_build_artifacts(git_repo):
    """docs/Bugs.md #51: a task's own commands (`npm install`, running a `.py`
    file) leave build artifacts behind, and a blind `git add -A` turns them
    into tracked, committed state — which then collides with an untracked
    copy of the same path in the long-lived shared integration checkout."""
    tree, _ = wt.create(git_repo, run_id="r2b", task_id="T-001", base="main")
    (tree.path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tree.path / "node_modules" / "pkg").mkdir(parents=True)
    (tree.path / "node_modules" / "pkg" / "index.js").write_text("{}\n", encoding="utf-8")
    (tree.path / "__pycache__").mkdir()
    (tree.path / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"\x00")

    result = wt.commit_all(tree.path, "T-001: add app.py")
    assert result.ok

    committed = wt.git(tree.path, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()
    assert "app.py" in committed
    assert not any("node_modules" in path for path in committed)
    assert not any("__pycache__" in path for path in committed)

    # Left on disk, just not tracked — this keeps an artifact out of the
    # commit, it does not delete anything from the agent's own working tree.
    assert (tree.path / "node_modules" / "pkg" / "index.js").exists()
    assert (tree.path / "__pycache__" / "app.cpython-313.pyc").exists()
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


def test_merge_preserves_an_installed_dependency_tree(git_repo):
    """Bugs.md #51 follow-on: once dependencies are installed in the
    integration checkout, by whatever means, a later wave's merge must not
    wipe them — reinstalling is not guaranteed to be possible again in the
    same run (offline npm cache, a sandboxed coding agent, ...)."""
    branch, _ = wt.ensure_integration_branch(git_repo, "r3b")
    wt.git(git_repo, "checkout", branch)
    (git_repo / "node_modules" / "pkg").mkdir(parents=True)
    (git_repo / "node_modules" / "pkg" / "index.js").write_text("{}\n", encoding="utf-8")
    (git_repo / "stray.tmp").write_text("leftover\n", encoding="utf-8")

    tree, _ = wt.create(git_repo, run_id="r3b", task_id="T-001", base=branch)
    (tree.path / "file.txt").write_text("T-001\n", encoding="utf-8")
    wt.commit_all(tree.path, "T-001: add file")

    result = wt.merge(git_repo, branch=tree.branch, into=branch, message="merge T-001")

    assert result.ok, result.stderr
    assert (git_repo / "node_modules" / "pkg" / "index.js").exists()
    assert not (git_repo / "stray.tmp").exists()
    wt.cleanup(git_repo, [tree])


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
