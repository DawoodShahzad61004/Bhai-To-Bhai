"""Protocol tests for the local Responses-to-Chat-Completions bridge."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

from adapters.local_llm_bridge import (
    chat_to_response_events,
    compact_chat_request,
    responses_bridge,
    responses_to_chat,
)


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
    assert chat["messages"][0] == {
        "role": "system",
        "content": "Use the repository tools.\n\nStay concise.",
    }
    assert chat["messages"][1] == {"role": "user", "content": "Inspect it."}
    assert chat["messages"][2]["tool_calls"][0]["id"] == "call-1"
    assert chat["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "contents",
    }
    flat_name = chat["tools"][0]["function"]["name"]
    assert len(chat["tools"]) == 1
    assert chat["messages"][2]["tool_calls"][0]["function"]["name"] == flat_name
    assert names[flat_name] == ("repo", "read_file", "function")


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
    assert compact["max_tokens"] == 512


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
    assert "event: response.output_item.done" in body
    assert '"text":"BRIDGE_OK"' in body
    assert "event: response.completed" in body
