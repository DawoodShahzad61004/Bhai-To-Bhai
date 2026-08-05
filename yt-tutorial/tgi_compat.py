"""OpenAI-spec compatibility shim for HuggingFace TGI endpoints.

TGI 3.x diverges from the OpenAI wire format in two directions, and an agent
loop hits both on consecutive turns.

Responses
---------
TGI returns tool-call arguments as a JSON *object*:

    "function": {"name": "Router", "arguments": {"next": "doc_writer"}}

The OpenAI API returns a JSON *string*, and langchain-core validates it as one,
so the raw TGI response fails AIMessage construction with:

    invalid_tool_calls.0.args
      Input should be a valid string [type=string_type]

TGI also omits "content" entirely on tool-call turns, where langchain requires
the key to be present.

Requests
--------
Fixing the response above yields an AIMessage with content="". On the *next*
turn langchain serializes that back out as "content": null (see
_convert_message_to_dict), and TGI answers HTTP 500 for any assistant message
with a null content. Bisected against TGI 3.0.0-sha-8f326c9: identical requests
differing only in content ""/null return 200/500 respectively.

So the same asymmetry has to be patched on the way out as well.
"""

import json
import uuid

import httpx


class TGICompatTransport(httpx.HTTPTransport):
    """Normalizes TGI chat-completion responses to the OpenAI wire format."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request = _rewrite_request(request)
        response = super().handle_request(request)

        if not request.url.path.endswith("/chat/completions"):
            return response
        # Streaming responses arrive as SSE chunks; leave them untouched.
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            return response

        response.read()
        try:
            payload = json.loads(response.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return response

        if not _normalize(payload):
            return response

        body = json.dumps(payload).encode()
        headers = httpx.Headers(response.headers)
        headers["content-length"] = str(len(body))
        headers.pop("content-encoding", None)
        return httpx.Response(
            status_code=response.status_code,
            headers=headers,
            content=body,
            request=request,
            extensions=response.extensions,
        )


def _rewrite_request(request: httpx.Request) -> httpx.Request:
    """Return a request TGI will accept, or the original if nothing needs fixing."""
    if not request.url.path.endswith("/chat/completions"):
        return request
    try:
        payload = json.loads(request.content)
    except (httpx.RequestNotRead, json.JSONDecodeError, UnicodeDecodeError):
        # Streaming upload; nothing to rewrite.
        return request

    if not _normalize_messages(payload):
        return request

    body = json.dumps(payload).encode()
    headers = httpx.Headers(request.headers)
    headers["content-length"] = str(len(body))
    return httpx.Request(
        method=request.method,
        url=request.url,
        headers=headers,
        content=body,
        extensions=request.extensions,
    )


def _normalize_messages(payload: dict) -> bool:
    """Replace null assistant content with "". Returns True if anything changed."""
    changed = False
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant" and message.get("content", "") is None:
            message["content"] = ""
            changed = True
    return changed


def _normalize(payload: dict) -> bool:
    """Fix tool-call shape in place. Returns True if anything changed."""
    changed = False
    for choice in payload.get("choices") or []:
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls") or []
        for call in tool_calls:
            # TGI labels every tool call "0", so a multi-turn transcript ends up
            # with several calls and results sharing one id and nothing pairs up.
            if call.get("id") in (None, "", "0"):
                call["id"] = f"call_{uuid.uuid4().hex[:12]}"
                changed = True
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            if isinstance(function.get("arguments"), dict):
                function["arguments"] = json.dumps(function["arguments"])
                changed = True
            # TGI emits "description": null here; not part of the OpenAI shape.
            if function.get("description", "") is None:
                function.pop("description")
                changed = True
        # TGI omits content entirely on tool-call turns.
        if tool_calls and message.get("content") is None:
            message["content"] = ""
            changed = True
    return changed


def build_http_client() -> httpx.Client:
    return httpx.Client(transport=TGICompatTransport())
