"""Loopback Responses-to-Chat-Completions compatibility bridge.

Codex only speaks the Responses wire protocol to custom providers. Some local
servers expose the older Chat Completions route instead. This module translates
that narrow protocol boundary while leaving Codex in charge of its tools,
sandbox, session, and agent loop.
"""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import re
from threading import Thread
import time
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from config import MAX_OUTPUT_SIZE_FOR_LOCAL_MODEL

logger = logging.getLogger(__name__)

_TOOL_NAME = re.compile(r"[^a-zA-Z0-9_-]")
_SERVER_MANAGED_TOOLS = {
    "code_interpreter",
    "computer",
    "file_search",
    "image_generation",
    "web_search",
    "web_search_preview",
}
_COMPACT_REQUEST_CHARS = 8_000


class BridgeRequestError(ValueError):
    """A request cannot be represented by Chat Completions."""


class UpstreamError(RuntimeError):
    """The configured Chat Completions server rejected or lost a request."""

    def __init__(self, status: int, body: bytes):
        super().__init__(f"upstream returned HTTP {status}")
        self.status = status
        self.body = body


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else json.dumps(content, ensure_ascii=False)
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _tool_output(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        text = _text_content(output)
        if text:
            return text
    return json.dumps(output, ensure_ascii=False)


def _flat_tool_name(namespace: str | None, name: str, used: set[str]) -> str:
    candidate = f"{namespace}__{name}" if namespace else name
    candidate = _TOOL_NAME.sub("_", candidate).strip("_") or "tool"
    if len(candidate) > 64:
        digest = sha256(candidate.encode("utf-8")).hexdigest()[:10]
        candidate = f"{candidate[:53]}_{digest}"
    original = candidate
    suffix = 2
    while candidate in used:
        tail = f"_{suffix}"
        candidate = f"{original[:64 - len(tail)]}{tail}"
        suffix += 1
    used.add(candidate)
    return candidate


def _chat_tools(
    tools: Any,
    allowed_names: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, tuple[str | None, str, str]]]:
    converted: list[dict[str, Any]] = []
    names: dict[str, tuple[str | None, str, str]] = {}
    used: set[str] = set()

    def add(tool: dict[str, Any], namespace: str | None = None) -> None:
        kind = str(tool.get("type", "function"))
        # These tools are executed by a Responses server, not by the Codex
        # client. A Chat Completions-only local server has no equivalent
        # execution channel, so do not advertise calls that nobody can fulfill.
        if kind in _SERVER_MANAGED_TOOLS:
            return
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise BridgeRequestError("Codex sent a tool without a name.")
        flat = _flat_tool_name(namespace, name, used)
        if allowed_names is not None and name not in allowed_names and flat not in allowed_names:
            return
        description = tool.get("description", "")
        if kind == "function":
            parameters = tool.get("parameters") or {
                "type": "object",
                "properties": {},
            }
        elif kind == "custom":
            parameters = {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            }
        else:
            raise BridgeRequestError(
                f"Local Chat Completions cannot represent Codex tool type {kind!r}."
            )
        function: dict[str, Any] = {
            "name": flat,
            "description": description if isinstance(description, str) else str(description),
            "parameters": parameters,
        }
        converted.append({"type": "function", "function": function})
        names[flat] = (namespace, name, kind)

    if tools is None:
        return converted, names
    if not isinstance(tools, list):
        raise BridgeRequestError("Codex sent a non-array tools field.")
    for tool in tools:
        if not isinstance(tool, dict):
            raise BridgeRequestError("Codex sent a malformed tool definition.")
        if tool.get("type") == "namespace":
            namespace = tool.get("name")
            nested = tool.get("tools")
            if not isinstance(namespace, str) or not isinstance(nested, list):
                raise BridgeRequestError("Codex sent a malformed tool namespace.")
            for child in nested:
                if not isinstance(child, dict):
                    raise BridgeRequestError("Codex sent a malformed namespaced tool.")
                add(child, namespace)
        else:
            add(tool)
    return converted, names


def responses_to_chat(
    payload: dict[str, Any],
    allowed_tool_names: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, tuple[str | None, str, str]]]:
    """Translate one Codex Responses request into a Chat Completions request."""
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise BridgeRequestError("Codex sent no model in its Responses request.")

    chat_tools, tool_names = _chat_tools(payload.get("tools"), allowed_tool_names)
    reverse_names = {value: key for key, value in tool_names.items()}
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions:
        system_parts.append(instructions)

    input_items = payload.get("input", [])
    if isinstance(input_items, str):
        input_items = [{"type": "message", "role": "user", "content": input_items}]
    if not isinstance(input_items, list):
        raise BridgeRequestError("Codex sent a non-array Responses input field.")

    for item in input_items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            role = item.get("role", "user")
            content = _text_content(item.get("content"))
            if role in ("developer", "system"):
                if content:
                    system_parts.append(content)
                continue
            if role not in ("system", "user", "assistant"):
                role = "user"
            messages.append({"role": role, "content": content})
        elif kind in ("function_call", "custom_tool_call"):
            tool_kind = "custom" if kind == "custom_tool_call" else "function"
            namespace = item.get("namespace")
            if not isinstance(namespace, str):
                namespace = None
            original_name = str(item.get("name") or "tool")
            chat_name = reverse_names.get(
                (namespace, original_name, tool_kind),
                _TOOL_NAME.sub("_", original_name),
            )
            arguments = item.get("arguments")
            if kind == "custom_tool_call":
                arguments = json.dumps({"input": item.get("input", "")}, ensure_ascii=False)
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments or {}, ensure_ascii=False)
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": str(item.get("call_id") or uuid4()),
                            "type": "function",
                            "function": {
                                "name": chat_name,
                                "arguments": arguments,
                            },
                        }
                    ],
                }
            )
        elif kind in ("function_call_output", "custom_tool_call_output"):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or ""),
                    "content": _tool_output(item.get("output", "")),
                }
            )

    if system_parts:
        messages.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})
    chat: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if chat_tools:
        chat["tools"] = chat_tools
        choice = payload.get("tool_choice")
        if choice in ("auto", "none", "required"):
            chat["tool_choice"] = choice
    if payload.get("parallel_tool_calls") is not None:
        chat["parallel_tool_calls"] = bool(payload["parallel_tool_calls"])
    return chat, tool_names


def _compact_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n\n[... compacted for local model context ...]\n\n"
    remaining = max(0, limit - len(marker))
    head = remaining * 3 // 5
    return f"{text[:head]}{marker}{text[-(remaining - head):]}"


def compact_chat_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Fit Codex's large default envelope into a 4K local-model context.

    This is used only after the upstream explicitly rejects the full request for
    context length. The newest user task and executable tool schema receive the
    largest shares; older contextual messages and generic Codex instructions
    are retained as bounded head/tail excerpts.
    """
    compact = dict(payload)
    messages = [dict(message) for message in payload.get("messages", [])]
    if not messages:
        return compact

    latest_user = max(
        (index for index, message in enumerate(messages) if message.get("role") == "user"),
        default=len(messages) - 1,
    )
    for index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if index == latest_user:
            limit = 5_000
        elif message.get("role") == "system":
            limit = 2_500
        else:
            limit = 1_000
        message["content"] = _compact_text(content, limit)
    compact["messages"] = messages
    compact["max_tokens"] = min(int(payload.get("max_tokens") or MAX_OUTPUT_SIZE_FOR_LOCAL_MODEL), MAX_OUTPUT_SIZE_FOR_LOCAL_MODEL)

    encoded = json.dumps(compact, ensure_ascii=False)
    if len(encoded) <= _COMPACT_REQUEST_CHARS:
        return compact

    # Tool schemas are needed for executable calls, so reduce prose before
    # touching them. The final task keeps twice the space of global guidance.
    for index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, str):
            continue
        message["content"] = _compact_text(content, 3_000 if index == latest_user else 1_200)
    compact["messages"] = messages
    return compact


def _is_context_overflow(error: UpstreamError) -> bool:
    """Return True for context-window errors and log server-reported token counts.

    The bridge cannot know the exact token count from the JSON request alone because
    tokenization is model-specific.  When the upstream server rejects the request for
    context length, however, servers such as vLLM usually include the exact token
    counts in the error message.  Extract those values here so the log shows the
    actual request size rather than only character counts.
    """
    raw_body = error.body.decode("utf-8", errors="replace")
    body = raw_body.lower()
    return "maximum context length" in body or "context_length" in body


def chat_to_response_events(
    payload: dict[str, Any],
    tool_names: dict[str, tuple[str | None, str, str]],
) -> list[dict[str, Any]]:
    """Translate one non-streaming Chat Completions response into Responses SSE events."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise BridgeRequestError("The local server returned no Chat Completions choice.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise BridgeRequestError("The local server returned no assistant message.")

    response_id = str(payload.get("id") or f"resp_{uuid4().hex}")
    model = str(payload.get("model") or "local-model")
    created = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "in_progress",
        "model": model,
        "output": [],
    }
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": created},
        {"type": "response.in_progress", "response": created},
    ]

    text = _text_content(message.get("content"))
    if text:
        message_item = {
            "id": f"msg_{uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
        events.append({"type": "response.output_text.delta", "delta": text})
        events.append({"type": "response.output_item.done", "item": message_item})

    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        raise BridgeRequestError("The local server returned malformed tool calls.")
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function")
        if not isinstance(function, dict):
            continue
        flat_name = str(function.get("name") or "")
        namespace, name, kind = tool_names.get(flat_name, (None, flat_name, "function"))
        arguments = function.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        call_id = str(tool_call.get("id") or f"call_{uuid4().hex}")
        item_id = f"{'ctc' if kind == 'custom' else 'fc'}_{uuid4().hex}"
        if kind == "custom":
            try:
                decoded = json.loads(arguments)
                custom_input = decoded.get("input", arguments) if isinstance(decoded, dict) else arguments
            except json.JSONDecodeError:
                custom_input = arguments
            item: dict[str, Any] = {
                "id": item_id,
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "input": str(custom_input),
            }
        else:
            item = {
                "id": item_id,
                "type": "function_call",
                "name": name,
                "arguments": arguments,
                "call_id": call_id,
            }
        if namespace:
            item["namespace"] = namespace
        events.append({"type": "response.output_item.done", "item": item})

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    completed = {
        "id": response_id,
        "object": "response",
        "status": "completed",
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens),
        },
    }
    events.append({"type": "response.completed", "response": completed})
    return events


def _post_chat(base_url: str, api_key: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(request, timeout=max(0.1, timeout)) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise UpstreamError(exc.code, exc.read()) from exc
    except (URLError, OSError) as exc:
        raise UpstreamError(502, json.dumps({"error": {"message": str(exc)}}).encode()) from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise UpstreamError(
            502, json.dumps({"error": {"message": "Local server returned invalid JSON."}}).encode()
        ) from exc
    if not isinstance(result, dict):
        raise UpstreamError(
            502, json.dumps({"error": {"message": "Local server returned a non-object."}}).encode()
        )
    return result


class _BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float,
        allowed_tool_names: set[str] | None,
    ):
        super().__init__(("127.0.0.1", 0), _BridgeHandler)
        self.upstream_base_url = base_url
        self.upstream_api_key = api_key
        self.upstream_timeout = timeout
        self.allowed_tool_names = allowed_tool_names


class _BridgeHandler(BaseHTTPRequestHandler):
    server: _BridgeServer

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("local LLM bridge: " + format, *args)

    def _json_error(self, status: int, message: str) -> None:
        body = json.dumps({"error": {"message": message}}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.split("?", 1)[0].rstrip("/") != "/v1/responses":
            self._json_error(404, "The bridge only exposes POST /v1/responses.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise BridgeRequestError("Responses request body must be an object.")
            chat, names = responses_to_chat(payload, self.server.allowed_tool_names)
            try:
                upstream = _post_chat(
                    self.server.upstream_base_url,
                    self.server.upstream_api_key,
                    chat,
                    self.server.upstream_timeout,
                )
            except UpstreamError as exc:
                if not _is_context_overflow(exc):
                    raise
                compact = compact_chat_request(chat)
                logger.info(
                    "Local LLM context overflow; retrying compact request (%d -> %d chars).",
                    len(json.dumps(chat, ensure_ascii=False)),
                    len(json.dumps(compact, ensure_ascii=False)),
                )
                upstream = _post_chat(
                    self.server.upstream_base_url,
                    self.server.upstream_api_key,
                    compact,
                    self.server.upstream_timeout,
                )
            events = chat_to_response_events(upstream, names)
        except BridgeRequestError as exc:
            self._json_error(400, str(exc))
            return
        except UpstreamError as exc:
            body = exc.body or json.dumps({"error": {"message": str(exc)}}).encode("utf-8")
            self.send_response(exc.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not payload.get("stream", False):
            completed = events[-1]["response"]
            completed["output"] = [event["item"] for event in events if "item" in event]
            body = json.dumps(completed).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for event in events:
            kind = event["type"]
            data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0].rstrip("/")

        if path != "/v1/models":
            self._json_error(404, "The bridge only exposes GET /v1/models.")
            return

        body = json.dumps(
            {
                "object": "list",
                "data": [
                    {
                        "id": "QuantTrio/Qwen3.6-27B-AWQ",
                        "object": "model",
                        "created": 0,
                        "owned_by": "local",
                    }
                ],
            }
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

def _allowed_codex_tools(tools: tuple[str, ...]) -> set[str] | None:
    if not tools:
        return None
    allowed: set[str] = set()
    if {"Read", "Write", "Edit", "Glob", "Grep", "Bash"}.intersection(tools):
        allowed.add("shell_command")
    if "Read" in tools:
        allowed.add("view_image")
    return allowed


@contextmanager
def responses_bridge(
    base_url: str,
    api_key: str,
    timeout: float,
    tools: tuple[str, ...] = (),
) -> Iterator[str]:
    """Serve the compatibility endpoint on loopback for one Codex dispatch."""
    server = _BridgeServer(base_url, api_key, timeout, _allowed_codex_tools(tools))
    thread = Thread(target=server.serve_forever, name="local-llm-responses-bridge", daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)