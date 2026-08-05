import os
from pathlib import Path
from dotenv import load_dotenv
from typing import List, Annotated, Optional, Dict
from langchain_unstructured import UnstructuredLoader
from langchain_core.tools import tool
from langchain_tavily import TavilySearch
from langchain_experimental.utilities import PythonREPL

load_dotenv()

@tool
def scrape_webpages(urls: list[str]) -> str:
    """Search the web using the provided query."""
    loader = UnstructuredLoader(web_url=urls)
    docs = loader.load()
    return "\n".join([f'<Document name="{doc.metadata.get("title", "")}">\n{doc.page_content}\n</Document>' for doc in docs])

def _temp_path(file_name: str) -> str:
    """Resolve a tool-supplied name inside temp/.

    basename() is deliberate: models echo the absolute path back from a previous
    tool result, and os.path.join discards its base when the tail is absolute,
    which lets the agent write anywhere on disk.
    """
    return os.path.join(os.getcwd(), "temp", os.path.basename(file_name))


def _available_documents() -> str:
    """List what is actually in temp/, so a wrong file name is self-correcting."""
    temp_dir = os.path.join(os.getcwd(), "temp")
    if not os.path.isdir(temp_dir):
        return "none"
    return ", ".join(sorted(os.listdir(temp_dir))) or "none"

@tool
def create_outline(
    points: Annotated[List[str], "A list of points to create an outline from"],
    file_name: Annotated[str, "The file path to save the outline to"]
) -> Annotated[str, "The file path where the outline was saved"]:
    """Create an outline from the provided points and save it to a file."""
    print("Creating outline...")
    file_to_use = _temp_path(file_name)
    os.makedirs(os.path.dirname(file_to_use), exist_ok=True)
    with open(file_to_use, "w", encoding="utf-8") as f:
        for i, point in enumerate(points, start=1):
            f.write(f"{i}. {point}\n")
    return f"Outline saved to: {os.path.basename(file_to_use)}"

@tool
def read_document(
    file_name: Annotated[str, "The file path of the document to read"],
    start: Annotated[Optional[int], "The starting line number to read from (0-indexed)"]=None,
    end: Annotated[Optional[int], "The ending line number to read to (0-indexed, exclusive)"]=None
) -> Annotated[str, "The content of the document"]:
    """Read a document and return its content."""

    print("Reading document...")
    file_to_use = _temp_path(file_name)
    if not os.path.exists(file_to_use):
        return f"Error: no document named {file_name}. Available documents: {_available_documents()}"
    with open(file_to_use, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if start is None:
        start = 0
    if end is None:
        end = len(lines)
    return "".join(lines[start:end])

@tool
def write_document(
    content: Annotated[str, "The content to write to the document"],
    file_name: Annotated[str, "The file path to save the document to"]
):
    """Write content to a document."""
    file_to_use = _temp_path(file_name)
    os.makedirs(os.path.dirname(file_to_use), exist_ok=True)
    with open(file_to_use, "w", encoding="utf-8") as f:
        f.write(content)
    print("Writing document...")
    return f"Document saved to: {os.path.basename(file_to_use)}"

@tool
def edit_document(
    file_name: Annotated[str, "The file path of the document to edit"],
    insert: Annotated[Dict[int, str], "A dictionary where keys are line numbers (0-indexed) and values are the content to insert at those lines"],
):
    """Edit a document."""
    print("Editing document...")
    file_to_use = _temp_path(file_name)
    if not os.path.exists(file_to_use):
        return f"Error: no document named {file_name}. Available documents: {_available_documents()}"
    with open(file_to_use, "r", encoding="utf-8") as f:
        lines = f.readlines()
    sorted_insert = sorted(insert.items())

    for line_number, text in sorted_insert:
        if line_number < 0 or line_number > len(lines):
            return f"Error: line number {line_number} is out of bounds; the document has {len(lines)} lines."
        lines.insert(line_number, text + "\n")
        
    with open(file_to_use, "w", encoding="utf-8") as f:
        f.writelines(lines)
        
    return f"Document edited and saved to: {os.path.basename(file_to_use)}"

tavily_tool = TavilySearch(
    name="tavily_search",
    description="A tool that can search the web and extract relevant information.",
    max_results=5
)

repl = PythonREPL()

@tool
def python_repl_tool(
    code: Annotated[str, "The Python code to execute to generate a chart"]
):
    """Use this to execute python code. If you want to see the output of any value,
    you should print it. This is visible to the user"""
    try:
        result = repl.run(code)
    except Exception as e:
        return f"Error occurred while executing Python code: {repr(e)}"
    return f"Successfully executed Python code:\n```python\n{code}\n```\nOutput:\n{result}"