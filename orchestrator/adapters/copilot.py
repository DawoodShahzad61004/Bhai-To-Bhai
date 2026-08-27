"""The GitHub Copilot CLI as a subprocess.

Copilot has the same architectural role as the direct Claude and Codex adapters:
it is a coding-agent CLI with its own prompt transport, permissions, session
state, and output format. Those differences stay at this boundary so workflow
nodes still consume one non-raising `AgentResult`.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any

from adapters.base import (
    AgentResult,
    classify_failure,
    register_backend,
    resolve_binary,
    run_with_deadline,
)
import config
from config import AgentSpec
from logging_config import get_logger

logger = get_logger(__name__)

_CONSOLE_EXCERPT = 200
_DEBUG_BLOCK_LIMIT = 12000

# Workflow nodes use Claude-style abstract tool names. Copilot CLI exposes a
# different concrete tool inventory, so normalize the request at this adapter
# boundary instead of granting every tool unconditionally.
#
# `powershell` is a session-based tool: a long-running command (e.g. a test
# runner that drops into watch mode) does not return its output inline, and
# the companion tools below are how the agent reads it back, feeds it input,
# or ends it. Omitting them from `--available-tools` while still allowing
# `powershell` itself lets an agent start a command it then has no way to
# read the result of — `read_powershell is unavailable in this environment
# (CommandNotFound)` is that failure surfacing mid-task.
_POWERSHELL_TOOL_FAMILY = ("powershell", "read_powershell", "write_powershell", "kill_powershell")

_TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "read": ("view",),
    "notebookread": ("view",),
    "glob": ("glob",),
    "grep": ("grep",),
    "rg": ("grep",),
    "bash": _POWERSHELL_TOOL_FAMILY if os.name == "nt" else ("bash",),
    "shell": _POWERSHELL_TOOL_FAMILY if os.name == "nt" else ("bash",),
    "powershell": _POWERSHELL_TOOL_FAMILY,
    "write": ("create", "edit", "apply_patch"),
    "edit": ("edit", "apply_patch"),
    "multiedit": ("edit", "apply_patch"),
    "notebookedit": ("edit", "apply_patch"),
    "create": ("create",),
    "apply_patch": ("apply_patch",),
}

_READ_TOOL_REQUESTS = frozenset({"read", "notebookread", "glob", "grep", "rg"})
_SHELL_TOOL_REQUESTS = frozenset({"bash", "shell", "powershell"})
_WRITE_TOOL_REQUESTS = frozenset(
    {"write", "edit", "multiedit", "notebookedit", "create", "apply_patch"}
)

# These stages should never edit product files directly. Requirements/planning
# get an even tighter shell allowlist because they only need repository
# inspection and graphify queries. Reviewer/supervisor may need arbitrary test
# commands, so their shell stays available when explicitly requested, while
# Copilot's direct file-write tools remain denied.
_READ_ONLY_TAGS = frozenset(
    {"requirements", "planner", "wave_orchestrator", "reviewer", "supervisor"}
)
_RESTRICTED_SHELL_TAGS = frozenset({"requirements", "planner"})
_SAFE_INSPECTION_SHELL_PATTERNS = (
    "shell(graphify:*)",
    "shell(git status)",
    "shell(git diff:*)",
    "shell(git log:*)",
    "shell(git show:*)",
    "shell(git ls-files:*)",
    "shell(git rev-parse:*)",
    "shell(git branch:*)",
)


def _normalise_requested_tools(tools: tuple[str, ...]) -> tuple[str, ...]:
    """Translate shared abstract tool names to Copilot CLI tool names."""
    concrete: list[str] = []
    for requested in tools:
        key = requested.strip().lower()
        aliases = _TOOL_ALIASES.get(key)
        if aliases is None:
            # Preserve a Copilot-native tool name supplied by a caller.
            aliases = (requested.strip(),)
        for tool in aliases:
            if tool and tool not in concrete:
                concrete.append(tool)
    return tuple(concrete)


def _tool_permission_args(*, tag: str, tools: tuple[str, ...]) -> list[str]:
    """Build least-privilege Copilot visibility and permission flags."""
    requested = {tool.strip().lower() for tool in tools if tool.strip()}
    concrete = _normalise_requested_tools(tools)

    # Keep the model's tool inventory constrained. If a caller requests no
    # tools, expose only ask_user and simultaneously disable it below, yielding
    # no usable tool without falling back to Copilot's full default inventory.
    available = concrete or ("ask_user",)
    args = [f"--available-tools={','.join(available)}"]

    if requested & _READ_TOOL_REQUESTS:
        args.append("--allow-tool=read")

    wants_shell = bool(requested & _SHELL_TOOL_REQUESTS)
    if wants_shell:
        if tag in _RESTRICTED_SHELL_TAGS:
            args.extend(f"--allow-tool={pattern}" for pattern in _SAFE_INSPECTION_SHELL_PATTERNS)
        else:
            args.append("--allow-tool=shell")

    wants_write = bool(requested & _WRITE_TOOL_REQUESTS)
    if wants_write and tag not in _READ_ONLY_TAGS:
        args.append("--allow-tool=write")
    else:
        # Deny beats allow in Copilot CLI. This protects read-only stages even
        # if a future caller accidentally includes a write-capable tool.
        args.append("--deny-tool=write")

    return args


def _debug_block(text: str) -> str:
    if len(text) <= _DEBUG_BLOCK_LIMIT:
        return text
    head = text[:4000]
    tail = text[-4000:]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n... [{omitted} chars omitted] ...\n{tail}"


def _text_from_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                nested = _text_from_value(item.get("text") or item.get("content"))
                if nested:
                    parts.append(nested)
        return "\n".join(part.strip() for part in parts if part and part.strip())
    if isinstance(value, dict):
        for key in ("text", "content", "message", "result", "response"):
            text = _text_from_value(value.get(key))
            if text:
                return text
    return ""


def _find_session_id(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("session_id", "sessionId"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        session = value.get("session")
        if isinstance(session, dict):
            candidate = session.get("id")
            if isinstance(candidate, str) and candidate:
                return candidate
        for nested in value.values():
            candidate = _find_session_id(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for nested in value:
            candidate = _find_session_id(nested)
            if candidate:
                return candidate
    return ""


def _error_message(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, str):
        return error.strip()
    if isinstance(error, dict):
        return _text_from_value(error) or str(error)
    if payload.get("is_error") is True:
        return _text_from_value(payload) or "Copilot reported an error"
    payload_type = str(payload.get("type") or payload.get("event") or "").lower()
    if payload_type == "error":
        return _text_from_value(payload) or "Copilot reported an error"
    return ""


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the first decodable JSON object embedded in *text*, if any."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _contains_json_object(text: str) -> bool:
    return _extract_json_object(text) is not None


def _extract_balanced_object(text: str, start: int) -> str:
    """Extract a balanced ``{...}`` block, respecting quoted strings."""
    if start < 0 or start >= len(text) or text[start] != "{":
        return ""

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    return ""


def _prompt_contract_block(prompt: str) -> str:
    """Return the JSON template embedded in a role prompt, when present."""
    lowered = prompt.lower()
    markers = (
        "return this json object:",
        "reply with a single json object matching this schema:",
        "reply with a single json object and nothing else:",
    )
    for marker in markers:
        marker_index = lowered.find(marker)
        if marker_index < 0:
            continue
        object_start = prompt.find("{", marker_index + len(marker))
        if object_start >= 0:
            block = _extract_balanced_object(prompt, object_start)
            if block:
                return block
    return ""


def _top_level_keys_from_template(template: str) -> tuple[str, ...]:
    """Extract top-level object keys from a prompt's JSON-like template.

    Prompt templates are often not valid JSON because their string values
    contain placeholders such as ``<...>``. We therefore inspect structure
    rather than decoding the template.
    """
    if not template.startswith("{"):
        return ()

    keys: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    string_start = -1
    last_string = ""

    index = 0
    while index < len(template):
        char = template[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                if string_start >= 0:
                    last_string = template[string_start:index]
            index += 1
            continue

        if char == '"':
            in_string = True
            string_start = index + 1
            index += 1
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == ":" and depth == 1 and last_string:
            # The most recently closed string immediately preceding a colon at
            # object depth 1 is a top-level key.
            between_start = index - 1
            while between_start >= 0 and template[between_start].isspace():
                between_start -= 1
            if between_start >= 0 and template[between_start] == '"':
                if last_string not in keys:
                    keys.append(last_string)
            last_string = ""

        index += 1

    return tuple(keys)


def _required_top_level_keys(
    *,
    prompt: str,
    json_schema: dict[str, Any] | None,
) -> tuple[str, ...]:
    """Resolve required top-level keys from a real schema or prompt template."""
    if json_schema is not None:
        required = json_schema.get("required")
        if isinstance(required, list):
            return tuple(key for key in required if isinstance(key, str) and key)
        properties = json_schema.get("properties")
        if isinstance(properties, dict):
            return tuple(str(key) for key in properties)

    return _top_level_keys_from_template(_prompt_contract_block(prompt))


def _structured_reply_problem(
    text: str,
    *,
    required_keys: tuple[str, ...],
) -> str:
    """Return an empty string when structured output satisfies the contract."""
    payload = _extract_json_object(text)
    if payload is None:
        return "no JSON object was found"

    missing = [key for key in required_keys if key not in payload]
    if missing:
        return "missing required top-level keys: " + ", ".join(missing)

    return ""


def _prompt_requests_json_object(prompt: str) -> bool:
    """Detect the structured-output contract already embedded in role prompts.

    Some workflow nodes define their JSON contract entirely in prompt text and
    pass ``json_schema=None`` through the shared adapter interface. Copilot's
    format-repair fallback must therefore recognize that contract independently
    of the optional adapter-level schema argument.
    """
    lowered = prompt.lower()
    markers = (
        "reply with a single json object",
        "return this json object",
        "return a single json object",
        "respond with a single json object",
        "single json object and nothing else",
    )
    return any(marker in lowered for marker in markers)


def _parse_json_lines(stdout: str) -> tuple[str, str, str]:
    """Return (text, session_id, error) from Copilot JSONL-ish output.

    The CLI documents JSONL but not a stable event schema in local help. This
    parser therefore reads structured records conservatively, preferring common
    answer fields and preserving a session id when any event exposes one.
    """
    text = ""
    session_id = ""
    error = ""
    parsed_any = False

    for raw_line in stdout.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        parsed_any = True

        if not session_id:
            session_id = _find_session_id(payload)

        error = error or _error_message(payload)

        payload_type = str(payload.get("type") or payload.get("event") or "").lower()
        if payload_type == "assistant.message":
            data = payload.get("data")
            if isinstance(data, dict):
                candidate = _text_from_value(data.get("content"))
                if candidate:
                    text = candidate

        for key in ("result", "response", "text", "message", "content"):
            candidate = _text_from_value(payload.get(key))
            if candidate:
                text = candidate

    if not parsed_any:
        return stdout.strip(), "", ""
    return text.strip(), session_id, error.strip()


def _build_argv(
    *,
    spec: AgentSpec,
    prompt: str,
    cwd: str,
    tag: str,
    tools: tuple[str, ...],
    resume_session: str,
    extra_dirs: tuple[str, ...],
) -> list[str]:
    argv = [
        resolve_binary(config.COPILOT_BIN),
        "-C",
        cwd,
        "--output-format",
        "json",
        "--silent",
        "--no-color",
        "--no-ask-user",
        "--no-custom-instructions",
        "--disable-builtin-mcps",
    ]
    argv += _tool_permission_args(tag=tag, tools=tools)
    if spec.model:
        argv += ["--model", spec.model]
    for extra_dir in extra_dirs:
        argv += ["--add-dir", extra_dir]
    if resume_session:
        argv += [f"--resume={resume_session}"]
    argv += ["-p", prompt]
    return argv


def _repair_structured_reply(
    *,
    spec: AgentSpec,
    cwd: str,
    tag: str,
    session_id: str,
    json_schema: dict[str, Any] | None,
    contract_block: str,
    required_keys: tuple[str, ...],
    previous_text: str,
    extra_dirs: tuple[str, ...],
) -> tuple[str, str, str]:
    """Ask the same Copilot session to re-emit its answer as JSON only.

    Copilot CLI's ``--output-format json`` controls the CLI event stream, not
    the model's response shape. A second, tool-free turn is therefore the
    narrowest reliable recovery when the first turn did the work but ignored
    the requested schema.
    """
    if json_schema is not None:
        schema_clause = (
            "It must match this JSON schema exactly:\n"
            f"{json.dumps(json_schema, ensure_ascii=False)}\n\n"
        )
    elif contract_block:
        schema_clause = (
            "Use this exact output-object template from the original prompt. "
            "Keep these field names and value types; replace placeholder text "
            "with the actual result:\n"
            f"{contract_block}\n\n"
        )
    else:
        schema_clause = (
            "It must match the exact JSON object contract specified in the "
            "original prompt for this session.\n\n"
        )

    if required_keys:
        schema_clause += (
            "The JSON object MUST contain all of these top-level keys exactly: "
            + ", ".join(required_keys)
            + ". Do not substitute generic fields such as status, message, "
              "outcome, files, recommendation, or timestamp unless they are "
              "explicitly part of the template.\n\n"
        )
    repair_prompt = (
        "Your previous turn completed the requested analysis, but its final "
        "reply violated the machine-readable output contract from the original "
        "prompt. Do not inspect the repository again. Do not run commands. Do "
        "not modify anything. Using only information already established in "
        "this session, re-emit the result now as exactly one JSON object and "
        "nothing else. No prose, no markdown fence, no commentary. "
        + schema_clause
        + "For reference, your previous final reply was:\n"
        + previous_text
    )

    repair_argv = _build_argv(
        spec=spec,
        prompt=repair_prompt,
        cwd=cwd,
        tag=f"{tag}:format-repair",
        tools=(),
        resume_session=session_id,
        extra_dirs=extra_dirs,
    )
    logger.debug(
        "[%s] structured-output repair argv: %s",
        tag,
        repair_argv[:-1] + ["<prompt>"],
    )
    logger.debug(
        "[%s] structured-output repair prompt:\n%s",
        tag,
        _debug_block(repair_prompt),
    )

    completed = run_with_deadline(
        repair_argv,
        input=None,
        cwd=cwd,
        timeout=spec.deadline_seconds,
    )
    stdout = completed.stdout or ""
    stderr = (completed.stderr or "").strip()
    if stderr:
        logger.debug("[%s] structured-output repair stderr:\n%s", tag, _debug_block(stderr))
    logger.debug(
        "[%s] structured-output repair stdout:\n%s",
        tag,
        _debug_block(stdout.strip()),
    )

    text, repaired_session_id, error = _parse_json_lines(stdout)
    if completed.returncode != 0:
        reason = error or text or stderr[-300:] or f"copilot exited {completed.returncode}"
        return "", repaired_session_id or session_id, reason
    if error:
        return "", repaired_session_id or session_id, error
    return text.strip(), repaired_session_id or session_id, ""


def run(
    prompt: str,
    *,
    spec: AgentSpec,
    system_prompt: str,
    cwd: str,
    tag: str,
    tools: tuple[str, ...],
    json_schema: dict[str, Any] | None,
    resume_session: str,
    extra_dirs: tuple[str, ...] = (),
) -> AgentResult:
    """One GitHub Copilot CLI turn. Returns a result; never raises."""
    full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    if json_schema is not None:
        full_prompt = (
            f"{full_prompt}\n\nReply with a single JSON object matching this schema:\n"
            f"{json.dumps(json_schema)}"
        )

    argv = _build_argv(
        spec=spec,
        prompt=full_prompt,
        cwd=cwd,
        tag=tag,
        tools=tools,
        resume_session=resume_session,
        extra_dirs=extra_dirs,
    )
    logger.debug("[%s] argv: %s", tag, argv[:-1] + ["<prompt>"])
    logger.debug("[%s] prompt:\n%s", tag, _debug_block(full_prompt))
    if tools:
        logger.debug(
            "[%s] requested tools=%s | copilot tools=%s",
            tag,
            tools,
            _normalise_requested_tools(tools),
        )

    started = time.perf_counter()
    try:
        completed = run_with_deadline(
            argv,
            input=None,
            cwd=cwd,
            timeout=spec.deadline_seconds,
        )
    except FileNotFoundError:
        return AgentResult(
            ok=False,
            error_kind="not_installed",
            error_message=(
                f"GitHub Copilot CLI {config.COPILOT_BIN!r} was not found from this process's "
                "environment. Set COPILOT_BIN or install it."
            ),
            duration_seconds=time.perf_counter() - started,
        )
    except subprocess.TimeoutExpired as exc:
        text, session_id, _ = _parse_json_lines(exc.stdout or "")
        return AgentResult(
            ok=False,
            error_kind="timeout",
            error_message=(
                f"GitHub Copilot CLI ran past its {spec.deadline_seconds}s deadline and was "
                "abandoned. Anything it saved before then is still on disk."
            ),
            text=text,
            duration_seconds=time.perf_counter() - started,
            session_id=session_id,
        )

    elapsed = time.perf_counter() - started
    stdout = completed.stdout or ""
    stderr = (completed.stderr or "").strip()
    if stderr:
        logger.debug("[%s] stderr:\n%s", tag, _debug_block(stderr))
    logger.debug("[%s] stdout:\n%s", tag, _debug_block(stdout.strip()))

    text, session_id, error = _parse_json_lines(stdout)

    # ``--output-format json`` makes Copilot emit JSONL events; it does NOT
    # guarantee that the assistant's final message follows our requested JSON
    # schema. If the substantive turn succeeded but ignored the contract,
    # resume that exact session for one tool-free formatting-only correction.
    structured_output_required = (
        json_schema is not None or _prompt_requests_json_object(full_prompt)
    )
    contract_block = _prompt_contract_block(full_prompt)
    required_keys = _required_top_level_keys(
        prompt=full_prompt,
        json_schema=json_schema,
    )
    structured_problem = (
        _structured_reply_problem(text, required_keys=required_keys)
        if structured_output_required and text
        else ""
    )

    if (
        completed.returncode == 0
        and not error
        and structured_output_required
        and text
        and structured_problem
        and session_id
    ):
        logger.warning(
            "[%s] Copilot violated the structured-output contract (%s); "
            "requesting one same-session format repair",
            tag,
            structured_problem,
        )
        try:
            repaired_text, repaired_session_id, repair_error = _repair_structured_reply(
                spec=spec,
                cwd=cwd,
                tag=tag,
                session_id=session_id,
                json_schema=json_schema,
                contract_block=contract_block,
                required_keys=required_keys,
                previous_text=text,
                extra_dirs=extra_dirs,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(
                ok=False,
                error_kind="timeout",
                error_message=(
                    "GitHub Copilot completed the substantive turn but its "
                    "structured-output repair exceeded the deadline."
                ),
                text=text,
                duration_seconds=time.perf_counter() - started,
                session_id=session_id,
            )
        if repair_error:
            return AgentResult(
                ok=False,
                error_kind=classify_failure(repair_error),
                error_message=(
                    "GitHub Copilot completed the substantive turn but failed "
                    f"the structured-output repair: {repair_error}"
                ),
                text=text,
                duration_seconds=time.perf_counter() - started,
                session_id=repaired_session_id or session_id,
            )
        if repaired_text:
            text = repaired_text
            session_id = repaired_session_id or session_id

        structured_problem = _structured_reply_problem(
            text,
            required_keys=required_keys,
        )

    if completed.returncode != 0:
        reason = error or text or stderr[-300:] or f"copilot exited {completed.returncode}"
        return AgentResult(
            ok=False,
            error_kind=classify_failure(reason),
            error_message=reason,
            duration_seconds=elapsed,
            session_id=session_id,
        )
    if error:
        return AgentResult(
            ok=False,
            error_kind=classify_failure(error),
            error_message=error,
            duration_seconds=elapsed,
            session_id=session_id,
        )
    if structured_output_required and text and structured_problem:
        return AgentResult(
            ok=False,
            error_kind="agent_error",
            error_message=(
                "GitHub Copilot violated the requested structured-output contract "
                "even after one same-session format-repair turn: "
                f"{structured_problem}."
            ),
            text=text,
            duration_seconds=time.perf_counter() - started,
            session_id=session_id,
        )

    if not text:
        if stderr:
            return AgentResult(
                ok=False,
                error_kind=classify_failure(stderr),
                error_message=stderr,
                duration_seconds=elapsed,
                session_id=session_id,
            )
        return AgentResult(
            ok=False,
            error_kind="no_output",
            error_message=(
                "GitHub Copilot CLI exited 0 but produced no parseable final reply. "
                f"{stderr[-300:] or '(no stderr)'}"
            ),
            duration_seconds=elapsed,
            session_id=session_id,
        )

    logger.info(
        "[%s] copilot done | %.1fs | session=%s",
        tag,
        elapsed,
        session_id[:8] or "-",
    )
    logger.info("[%s] reply: %s", tag, text[:_CONSOLE_EXCERPT])
    logger.debug("[%s] full reply:\n%s", tag, _debug_block(text))
    return AgentResult(ok=True, text=text, duration_seconds=elapsed, session_id=session_id)


register_backend("direct:copilot", run)