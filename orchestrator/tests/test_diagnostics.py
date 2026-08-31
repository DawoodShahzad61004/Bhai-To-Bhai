"""Configured backend/model diagnostics without real provider calls."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import pytest

import config
import diagnostics
from adapters.base import AgentResult
from config import AgentSpec


def _target(
    backend: str = "codex",
    model: str = "",
    *,
    deadline: int = 600,
    source: str = "AGENTS['planner']",
) -> diagnostics.DiagnosticTarget:
    return diagnostics.DiagnosticTarget(
        invocation="direct",
        backend=backend,
        model=model,
        deadline_seconds=deadline,
        sources=(source,),
    )


def _set_example_config(monkeypatch) -> None:
    monkeypatch.setattr(
        config,
        "AGENTS",
        {
            "requirements": AgentSpec("claude", "haiku", 900),
            "wave_orchestrator": AgentSpec("gemini", "gemini-3.1-flash-lite", 900),
            "merger": AgentSpec("gemini", "gemini-3.1-flash-lite", 900),
            "planner": AgentSpec("codex", "", 600),
            "reviewer": AgentSpec("codex", "", 600),
            "supervisor": AgentSpec("codex", "", 600),
        },
    )
    monkeypatch.setattr(config, "CODING_AGENT_A", AgentSpec("codex", "", 900))
    monkeypatch.setattr(config, "CODING_AGENT_B", AgentSpec("codex", "", 900))
    monkeypatch.setattr(config, "SMALL_MODELS", [("qwen3.5:4b", "ollama")])
    monkeypatch.setattr(
        config,
        "MEDIUM_MODELS",
        [
            ("gpt-oss:20b-cloud", "ollama"),
            ("nemotron-3-nano:30b-cloud", "ollama"),
            ("QuantTrio/Qwen3.6-27B-AWQ", "local_llm"),
            ("gemma4:31b-cloud", "ollama"),
            ("auto", "copilot"),
        ],
    )
    monkeypatch.setattr(config, "EXPERT_MODELS", [("", "codex")])
    monkeypatch.setattr(config, "INVOCATION", "direct")


def test_example_config_produces_nine_unique_minimum_deadline_targets(monkeypatch):
    _set_example_config(monkeypatch)

    targets = diagnostics.configured_targets()
    by_pair = {(target.backend, target.model): target for target in targets}

    assert len(targets) == 9
    assert list(by_pair) == [
        ("claude", "haiku"),
        ("gemini", "gemini-3.1-flash-lite"),
        ("codex", ""),
        ("ollama", "qwen3.5:4b"),
        ("ollama", "gpt-oss:20b-cloud"),
        ("ollama", "nemotron-3-nano:30b-cloud"),
        ("local_llm", "QuantTrio/Qwen3.6-27B-AWQ"),
        ("ollama", "gemma4:31b-cloud"),
        ("copilot", "auto"),
    ]
    assert by_pair[("gemini", "gemini-3.1-flash-lite")].deadline_seconds == 900
    assert by_pair[("codex", "")].deadline_seconds == 600
    assert by_pair[("copilot", "auto")].deadline_seconds == 900
    assert by_pair[("ollama", "qwen3.5:4b")].harness == "codex"
    assert by_pair[("local_llm", "QuantTrio/Qwen3.6-27B-AWQ")].harness == "codex"
    assert by_pair[("gemini", "gemini-3.1-flash-lite")].sources == (
        "AGENTS['wave_orchestrator']",
        "AGENTS['merger']",
    )
    assert by_pair[("codex", "")].sources == (
        "AGENTS['planner']",
        "AGENTS['reviewer']",
        "AGENTS['supervisor']",
        "CODING_AGENT_A",
        "CODING_AGENT_B",
        "EXPERT_MODELS[0]",
    )


def test_menu_only_targets_inherit_the_smaller_fallback_deadline(monkeypatch):
    monkeypatch.setattr(config, "AGENTS", {})
    monkeypatch.setattr(config, "CODING_AGENT_A", AgentSpec("codex", "", 900))
    monkeypatch.setattr(config, "CODING_AGENT_B", AgentSpec("codex", "", 700))
    monkeypatch.setattr(config, "SMALL_MODELS", [("model", "ollama")])
    monkeypatch.setattr(config, "MEDIUM_MODELS", [])
    monkeypatch.setattr(config, "EXPERT_MODELS", [])

    target = next(item for item in diagnostics.configured_targets() if item.backend == "ollama")

    assert target.deadline_seconds == 700


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("INVOCATION", "", "INVOCATION"),
        ("AGENTS", [], "AGENTS"),
        ("SMALL_MODELS", [("model", "")], "backend"),
    ],
)
def test_invalid_configuration_fails_closed(monkeypatch, attribute, value, message):
    _set_example_config(monkeypatch)
    monkeypatch.setattr(config, attribute, value)

    with pytest.raises(ValueError, match=message):
        diagnostics.configured_targets()


def test_each_pair_runs_once_with_bounded_parallelism_and_stable_result_order(
    monkeypatch, tmp_path
):
    targets = tuple(
        _target("backend", f"model-{index}", source=f"source-{index}")
        for index in range(6)
    )
    calls: list[str] = []
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_run_agent(prompt, *, spec, cwd, **kwargs):
        nonlocal active, peak
        assert prompt == diagnostics.DIAGNOSTIC_PROMPT
        assert Path(cwd).is_dir()
        with lock:
            calls.append(spec.model)
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return AgentResult(ok=True, text='{"diagnostic":"ok"}')

    monkeypatch.setattr(diagnostics.adapters, "run_agent", fake_run_agent)

    report = diagnostics.run_diagnostics(
        targets,
        max_parallel=3,
        workspace_root=tmp_path,
    )

    assert report.passed is True
    assert report.max_parallel == 3
    assert peak == 3
    assert sorted(calls) == [f"model-{index}" for index in range(6)]
    assert [result.target.model for result in report.results] == [
        f"model-{index}" for index in range(6)
    ]


@pytest.mark.parametrize(
    ("agent_result", "error_kind", "detail_fragment"),
    [
        (
            AgentResult(ok=False, error_kind="bad_request", error_message="unknown model"),
            "bad_request",
            "unknown model",
        ),
        (
            AgentResult(ok=True, text="diagnostic ok"),
            "agent_error",
            "JSON contract",
        ),
        (
            AgentResult(ok=True, text='{"diagnostic":"wrong"}'),
            "agent_error",
            "unexpected diagnostic payload",
        ),
    ],
)
def test_provider_and_contract_failures_are_reported(
    monkeypatch, tmp_path, agent_result, error_kind, detail_fragment
):
    monkeypatch.setattr(diagnostics.adapters, "run_agent", lambda *args, **kwargs: agent_result)

    report = diagnostics.run_diagnostics(
        (_target(),),
        max_parallel=3,
        workspace_root=tmp_path,
    )

    result = report.results[0]
    assert report.passed is False
    assert result.error_kind == error_kind
    assert detail_fragment in result.detail


def test_structured_adapter_payload_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setattr(
        diagnostics.adapters,
        "run_agent",
        lambda *args, **kwargs: AgentResult(ok=True, text="ignored", structured={"diagnostic": "ok"}),
    )

    report = diagnostics.run_diagnostics(
        (_target(),),
        max_parallel=3,
        workspace_root=tmp_path,
    )

    assert report.passed is True


def test_human_and_json_rendering_include_actionable_target_data():
    target = _target("ollama", "qwen3.5:4b", deadline=900, source="SMALL_MODELS[0]")
    report = diagnostics.DiagnosticReport(
        results=(
            diagnostics.DiagnosticResult(
                target=target,
                passed=False,
                duration_seconds=1.25,
                error_kind="rate_limit",
                detail="quota exhausted",
            ),
        ),
        max_parallel=1,
    )

    human = diagnostics.render_human(report)
    payload = json.loads(diagnostics.render_json(report))

    assert "direct:ollama" in human
    assert "harness=codex" in human
    assert "rate_limit: quota exhausted" in human
    assert payload["passed"] is False
    assert payload["results"][0]["model"] == "qwen3.5:4b"
    assert payload["results"][0]["sources"] == ["SMALL_MODELS[0]"]


def test_standalone_configuration_error_is_json_and_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        diagnostics,
        "run_configured_diagnostics",
        lambda: (_ for _ in ()).throw(ValueError("bad config")),
    )

    code = diagnostics.main(["--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload == {"passed": False, "configuration_error": "bad config"}


def test_automatic_diagnostics_are_disabled_by_default():
    assert config.ENABLE_AGENT_DIAGNOSTICS is False
    assert config.AGENT_DIAGNOSTIC_MAX_PARALLEL == 3
