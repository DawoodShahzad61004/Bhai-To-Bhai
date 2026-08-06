"""Hands this project's own tools (tools/) to Claude Code, over MCP.

Claude Code only accepts external tools through the Model Context Protocol, so
"send our custom tools" means standing up an MCP server in front of them. The
server is this file run as a script: Claude Code launches one per worker turn
and speaks JSON-RPC to it over stdin/stdout.

    python claude_agents/mcp_bridge.py tavily_search

Only the tools named on the command line are exposed, which is how a worker gets
the same tool set its LangChain counterpart was built with rather than all of
them. The protocol surface is deliberately the smallest one that works --
initialize, tools/list, tools/call, ping -- because the whole vocabulary here is
"list these tools, call one of them".

When CLAUDE_CODE_SEND_CUSTOM_TOOLS is off, nothing in this module runs: Claude
Code uses its own tools instead.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from config import BASE_DIR, CLAUDE_CODE_MCP_SERVER_NAME, FILE_LOG_LEVEL, WORKSPACE_DIR
from logging_config import current_log_file, get_logger
from tools import (
    create_outline,
    edit_document,
    python_repl_tool,
    read_document,
    scrape_webpages,
    tavily_tool,
    write_document,
)

logger = get_logger(__name__)

# Bare tool names, exactly as the role briefs in prompts.py refer to them.
TOOLS = {
    tool.name: tool
    for tool in (
        tavily_tool,
        scrape_webpages,
        create_outline,
        read_document,
        write_document,
        edit_document,
        python_repl_tool,
    )
}

_PROTOCOL_VERSION = "2024-11-05"
_SERVER_VERSION = "1.0.0"

# Where the parent run tells this process to log. Claude Code keeps the server's
# stderr for its own diagnostics and never forwards it, so without this the only
# record of what a tool actually did dies with the process.
RUN_LOG_ENV = "AGENT_RUN_LOG"


# ── Config for the CLI side ───────────────────────────────────────────────────


def mcp_config_json(tool_names: tuple[str, ...]) -> str:
    """The --mcp-config payload that launches this server with `tool_names`.

    By file path rather than `-m claude_agents.mcp_bridge`, because the module
    form imports the package first -- which pulls in langgraph, langchain and
    every agent in this package before a single tool is served, on every worker
    turn. Nothing in this file needs the package.

    PYTHONPATH rather than a working directory, because Claude Code runs with
    its own cwd (the workspace) and the server has to import config, tools and
    logging_config from the project root regardless of where it was started.

    alwaysLoad is what makes this work at all. Claude Code connects MCP servers
    in the background by default and does not wait for them, so a server that
    takes a moment to start -- and this one imports langchain, so it does --
    misses the turn: the worker is handed no tools, and then reports having
    written a file it had no way to write. alwaysLoad puts the connection on the
    blocking path, before the first model request.
    """
    unknown = [name for name in tool_names if name not in TOOLS]
    if unknown:
        raise KeyError(f"No such tool in tools/: {', '.join(unknown)}")
    env = {"PYTHONPATH": str(BASE_DIR)}
    log_file = current_log_file()
    if log_file is not None:
        env[RUN_LOG_ENV] = str(log_file)
    return json.dumps(
        {
            "mcpServers": {
                CLAUDE_CODE_MCP_SERVER_NAME: {
                    "command": sys.executable,
                    "args": [str(Path(__file__).resolve()), *tool_names],
                    "env": env,
                    "alwaysLoad": True,
                }
            }
        }
    )


# ── Server ────────────────────────────────────────────────────────────────────


def _input_schema(tool) -> dict:
    """A tool's arguments as JSON Schema, taken from its LangChain signature."""
    schema = tool.get_input_schema().model_json_schema()
    schema.pop("title", None)
    return schema


def _describe(tool) -> dict:
    return {
        "name": tool.name,
        "description": (tool.description or "").strip(),
        "inputSchema": _input_schema(tool),
    }


def _call(tool, arguments: dict) -> dict:
    """Run a tool and answer in MCP's content shape.

    A failure is reported as a result with isError set, not as a JSON-RPC error:
    the model is supposed to read what went wrong and try something else, and a
    protocol-level error would never reach it.
    """
    try:
        output = tool.invoke(arguments)
    except Exception as e:
        logger.exception("MCP tool %r failed", tool.name)
        return {
            "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
            "isError": True,
        }
    return {"content": [{"type": "text", "text": str(output)}], "isError": False}


def _dispatch(method: str, params: dict, tools: dict) -> dict | None:
    """Answer one request, or return None if the method is unknown."""
    if method == "initialize":
        return {
            # Echo the client's version when it states one: this server's whole
            # surface is tool listing and tool calling, which every revision of
            # the protocol spells the same way.
            "protocolVersion": params.get("protocolVersion") or _PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": CLAUDE_CODE_MCP_SERVER_NAME, "version": _SERVER_VERSION},
        }
    if method == "tools/list":
        return {"tools": [_describe(tool) for tool in tools.values()]}
    if method == "tools/call":
        name = params.get("name", "")
        tool = tools.get(name)
        if tool is None:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"No tool named {name}. Available: {', '.join(tools)}",
                    }
                ],
                "isError": True,
            }
        return _call(tool, params.get("arguments") or {})
    if method == "ping":
        return {}
    return None


def _handle(message: dict, tools: dict) -> dict | None:
    """One JSON-RPC message in, at most one out.

    Notifications carry no id and must never be answered -- replying to one
    makes the client discard the response and log a protocol violation.
    """
    request_id = message.get("id")
    method = message.get("method", "")
    result = _dispatch(method, message.get("params") or {}, tools)
    if request_id is None:
        return None
    if result is None:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _attach_run_log() -> None:
    """Record this process's log lines in the run's own debug log.

    setup_logging() is deliberately not used: it configures a console handler as
    well, and this process has no console to spare. Only the file matters here.
    """
    path = os.getenv(RUN_LOG_ENV)
    if not path:
        return
    handler = logging.FileHandler(path, encoding="utf-8", delay=True)
    handler.setLevel(FILE_LOG_LEVEL)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | [mcp] | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(FILE_LOG_LEVEL)
    root.addHandler(handler)

    # matplotlib logs every font it scores while rendering a chart -- hundreds of
    # DEBUG lines around the two that say what the tool did.
    for noisy in ("matplotlib", "PIL", "httpx", "httpcore", "urllib3", "unstructured"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def serve(tool_names: list[str]) -> None:
    """Read JSON-RPC from stdin, write it to stdout, until the client closes."""
    tools = {name: TOOLS[name] for name in tool_names}
    _attach_run_log()

    # Every tool in tools/ works inside the workspace, and the document tools get
    # there by resolving absolute paths of their own. python_repl_tool cannot: it
    # execs code the model wrote, and a savefig("chart.png") in that code lands
    # wherever this process happens to be standing -- which, for a server Claude
    # Code launched, is not the workspace. The chart is then written somewhere
    # nobody looks, and the worker truthfully reports saving a file that the run
    # can never find. Standing the process in the workspace makes the relative
    # paths in generated code agree with the sandbox the rest of the tools keep.
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    os.chdir(WORKSPACE_DIR)
    logger.info(
        "MCP bridge serving %s from %s", ", ".join(tool_names) or "(nothing)", os.getcwd()
    )

    # stdout is the protocol channel and nothing else may touch it. A tool that
    # prints, or a library that writes a warning, would land in the middle of a
    # JSON-RPC frame and desynchronise the stream -- so the real stdout is kept
    # private here and sys.stdout is pointed at stderr for the rest of the
    # process. Logging is left unconfigured for the same reason: with no
    # handlers, records go to stderr.
    channel = sys.stdout
    sys.stdout = sys.stderr
    channel.reconfigure(encoding="utf-8", newline="\n")
    sys.stdin.reconfigure(encoding="utf-8")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("MCP bridge ignored unparseable frame: %s", line[:200])
            continue
        response = _handle(message, tools)
        if response is not None:
            channel.write(json.dumps(response) + "\n")
            channel.flush()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python claude_agents/mcp_bridge.py <tool> [<tool> ...]", file=sys.stderr)
        print(f"available tools: {', '.join(sorted(TOOLS))}", file=sys.stderr)
        return 2
    unknown = [name for name in argv if name not in TOOLS]
    if unknown:
        print(f"unknown tool(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available tools: {', '.join(sorted(TOOLS))}", file=sys.stderr)
        return 2
    serve(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
