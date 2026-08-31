"""Protocol tests for the local Responses-to-Chat-Completions bridge."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

from adapters.local_llm_bridge import (
    UpstreamError,
    _is_context_overflow,
    chat_to_response_events,
    compact_chat_request,
    responses_bridge,
    responses_to_chat,
)
from config import MAX_OUTPUT_SIZE_FOR_LOCAL_MODEL


def test_responses_request_becomes_chat_messages_and_tools():
    chat, names = responses_to_chat(
        {
            "model": "local/model",
            "instructions": "Use the repository tools.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Inspect it."}],
                },
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "Stay concise."}],
                },
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "namespace": "repo",
                    "name": "read_file",
                    "arguments": '{"path":"README.md"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "contents",
                },
            ],
            "tools": [
                {
                    "type": "namespace",
                    "name": "repo",
                    "tools": [
                        {
                            "type": "function",
                            "name": "read_file",
                            "description": "Read one file.",
                            "parameters": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}},
                                "required": ["path"],
                            },
                        }
                    ],
                },
                {"type": "web_search", "external_web_access": True},
            ],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
    )

    assert chat["model"] == "local/model"
    assert chat["stream"] is False
    assert chat["messages"][0] == {"role": "user", "content": "Inspect it."}
    assert chat["messages"][1]["tool_calls"][0]["id"] == "call-1"
    assert chat["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "contents",
    }
    flat_name = chat["tools"][0]["function"]["name"]
    assert len(chat["tools"]) == 1
    assert chat["messages"][1]["tool_calls"][0]["function"]["name"] == flat_name
    assert names[flat_name] == ("repo", "read_file", "function")


def test_codex_harness_context_is_removed_without_losing_round_trip_history():
    harness_context = (
        "<recommended_plugins>plugins</recommended_plugins>\n"
        "# AGENTS.md instructions\nrepository rules\n"
        "<environment_context>workspace metadata</environment_context>"
    )
    chat, _ = responses_to_chat(
        {
            "model": "local/model",
            "instructions": "Codex CLI defaults and tool guidance.",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "Harness memory."}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": harness_context}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Create main.py."}],
                },
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "shell_command",
                    "arguments": '{"command":"Get-ChildItem"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "README.md",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "shell_command",
                    "description": "Run a command.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }
    )

    assert chat["messages"] == [
        {"role": "user", "content": "Create main.py."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "shell_command",
                        "arguments": '{"command":"Get-ChildItem"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "README.md"},
    ]


def test_user_message_that_mentions_agent_rules_is_not_harness_context():
    chat, _ = responses_to_chat(
        {
            "model": "local/model",
            "input": "Read AGENTS.md instructions before editing.",
        }
    )

    assert chat["messages"] == [
        {"role": "user", "content": "Read AGENTS.md instructions before editing."}
    ]


def test_chat_tool_call_becomes_codex_response_items():
    events = chat_to_response_events(
        {
            "id": "chat-1",
            "model": "local/model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Checking.",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "repo__read_file",
                                    "arguments": '{"path":"README.md"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        },
        {"repo__read_file": ("repo", "read_file", "function")},
    )

    assert events[0]["type"] == "response.created"
    assert events[2] == {"type": "response.output_text.delta", "delta": "Checking."}
    tool_item = events[4]["item"]
    assert tool_item["type"] == "function_call"
    assert tool_item["namespace"] == "repo"
    assert tool_item["name"] == "read_file"
    assert tool_item["call_id"] == "call-1"
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["usage"]["total_tokens"] == 14


def test_compact_request_preserves_latest_task_and_tool_schema():
    compact = compact_chat_request(
        {
            "model": "local/model",
            "messages": [
                {"role": "system", "content": "S" * 20_000},
                {"role": "user", "content": "context " * 2_000},
                {"role": "user", "content": "LATEST_TASK"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "shell_command",
                        "description": "Run a command.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "stream": False,
        }
    )

    assert len(json.dumps(compact)) < 12_000
    assert compact["messages"][-1]["content"] == "LATEST_TASK"
    assert compact["tools"][0]["function"]["name"] == "shell_command"
    assert compact["max_tokens"] == MAX_OUTPUT_SIZE_FOR_LOCAL_MODEL


def test_server_token_budget_error_triggers_context_compaction():
    error = UpstreamError(
        422,
        json.dumps(
            {
                "error": (
                    "Input validation error: `inputs` tokens + `max_new_tokens` "
                    "must be <= 66222. Given: 97379 `inputs` tokens and 0 "
                    "`max_new_tokens`"
                ),
                "error_type": "validation",
            }
        ).encode("utf-8"),
    )

    assert _is_context_overflow(error) is True


def test_loopback_bridge_serves_responses_sse(monkeypatch):
    captured: dict[str, object] = {}

    def fake_post(base_url, api_key, payload, timeout):
        captured.update(base_url=base_url, api_key=api_key, payload=payload, timeout=timeout)
        return {
            "id": "chat-1",
            "model": "local/model",
            "choices": [{"message": {"role": "assistant", "content": "BRIDGE_OK"}}],
        }

    monkeypatch.setattr("adapters.local_llm_bridge._post_chat", fake_post)
    with responses_bridge("http://upstream.test/v1", "secret", 30) as base_url:
        request = Request(
            f"{base_url}/responses",
            data=json.dumps(
                {
                    "model": "local/model",
                    "input": "Return the marker.",
                    "tools": [
                        {
                            "type": "function",
                            "name": "shell_command",
                            "description": "Run a command.",
                            "parameters": {"type": "object", "properties": {}},
                        }
                    ],
                    "stream": True,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")

    assert captured["base_url"] == "http://upstream.test/v1"
    assert captured["api_key"] == "secret"
    assert captured["payload"]["messages"] == [
        {"role": "user", "content": "Return the marker."}
    ]
    assert "tools" not in captured["payload"]
    assert "event: response.output_item.done" in body
    assert '"text":"BRIDGE_OK"' in body
    assert "event: response.completed" in body
