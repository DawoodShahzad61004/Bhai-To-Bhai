"""Document tools. All reads and writes are confined to config.WORKSPACE_DIR."""

import os
from typing import Annotated, Dict, List, Optional

from langchain_core.tools import tool

from config import WORKSPACE_DIR
from logging_config import get_logger

logger = get_logger(__name__)


def _workspace_path(file_name: str) -> str:
    """Resolve a tool-supplied name inside the workspace.

    basename() is deliberate: models echo the absolute path back from a previous
    tool result, and os.path.join discards its base when the tail is absolute,
    which would let an agent write anywhere on disk.
    """
    resolved = os.path.join(WORKSPACE_DIR, os.path.basename(file_name))
    if os.path.basename(file_name) != file_name:
        logger.debug("Sandboxed tool path %r -> %r", file_name, resolved)
    return resolved


def _available_documents() -> str:
    """List what is actually in the workspace, so a wrong name self-corrects."""
    if not os.path.isdir(WORKSPACE_DIR):
        return "none"
    return ", ".join(sorted(os.listdir(WORKSPACE_DIR))) or "none"


@tool
def create_outline(
    points: Annotated[List[str], "A list of points to create an outline from"],
    file_name: Annotated[str, "The file name to save the outline to"],
) -> Annotated[str, "The file where the outline was saved"]:
    """Create an outline from the provided points and save it to a file."""
    file_to_use = _workspace_path(file_name)
    logger.info(
        "create_outline -> %s (%d points)", os.path.basename(file_to_use), len(points)
    )
    os.makedirs(os.path.dirname(file_to_use), exist_ok=True)
    with open(file_to_use, "w", encoding="utf-8") as f:
        for i, point in enumerate(points, start=1):
            f.write(f"{i}. {point}\n")
    return f"Outline saved to: {os.path.basename(file_to_use)}"


@tool
def read_document(
    file_name: Annotated[str, "The file name of the document to read"],
    start: Annotated[Optional[int], "Starting line number (0-indexed)"] = None,
    end: Annotated[Optional[int], "Ending line number (0-indexed, exclusive)"] = None,
) -> Annotated[str, "The content of the document"]:
    """Read a document and return its content."""
    file_to_use = _workspace_path(file_name)
    logger.info("read_document -> %s", os.path.basename(file_to_use))
    if not os.path.exists(file_to_use):
        logger.warning(
            "read_document: %r not found; available=%s", file_name, _available_documents()
        )
        return f"Error: no document named {file_name}. Available documents: {_available_documents()}"
    with open(file_to_use, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if start is None:
        start = 0
    if end is None:
        end = len(lines)
    logger.debug("read_document returned lines %d:%d of %d", start, end, len(lines))
    return "".join(lines[start:end])


@tool
def write_document(
    content: Annotated[str, "The content to write to the document"],
    file_name: Annotated[str, "The file name to save the document to"],
) -> Annotated[str, "The file where the document was saved"]:
    """Write content to a document."""
    file_to_use = _workspace_path(file_name)
    os.makedirs(os.path.dirname(file_to_use), exist_ok=True)
    with open(file_to_use, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info(
        "write_document -> %s (%d chars)", os.path.basename(file_to_use), len(content)
    )
    return f"Document saved to: {os.path.basename(file_to_use)}"


@tool
def edit_document(
    file_name: Annotated[str, "The file name of the document to edit"],
    insert: Annotated[
        Dict[int, str],
        "Keys are line numbers (0-indexed), values are the text to insert there",
    ],
) -> Annotated[str, "The file where the document was saved"]:
    """Insert lines into an existing document."""
    file_to_use = _workspace_path(file_name)
    logger.info(
        "edit_document -> %s (%d insert(s))", os.path.basename(file_to_use), len(insert)
    )
    if not os.path.exists(file_to_use):
        logger.warning(
            "edit_document: %r not found; available=%s", file_name, _available_documents()
        )
        return f"Error: no document named {file_name}. Available documents: {_available_documents()}"
    with open(file_to_use, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for line_number, text in sorted(insert.items()):
        if line_number < 0 or line_number > len(lines):
            logger.warning(
                "edit_document: line %d out of bounds (document has %d lines)",
                line_number,
                len(lines),
            )
            return f"Error: line number {line_number} is out of bounds; the document has {len(lines)} lines."
        lines.insert(line_number, text + "\n")

    with open(file_to_use, "w", encoding="utf-8") as f:
        f.writelines(lines)

    return f"Document edited and saved to: {os.path.basename(file_to_use)}"
