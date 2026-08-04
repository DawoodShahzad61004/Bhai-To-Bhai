import os
from pathlib import Path
from dotenv import load_dotenv
from typing import List, Annotated, Optional, Dict
from langchain_unstructured import UnstructuredLoader
from langchain_core.tools import tool
from langchain_tavily import TavilySearch

load_dotenv()

@tool
def scrape_webpages(urls: list[str]) -> str:
    """Search the web using the provided query."""
    loader = UnstructuredLoader(web_url=urls)
    docs = loader.load()
    return "\n".join([f'<Document name="{doc.metadata.get("title", "")}">\n{doc.page_content}\n</Document>' for doc in docs])

@tool
def create_outline(
    points: Annotated[List[str], "A list of points to create an outline from"],
    file_name: Annotated[str, "The file path to save the outline to"]
) -> Annotated[str, "The file path where the outline was saved"]:
    """Create an outline from the provided points and save it to a file."""
    print("Creating outline...")
    file_to_use = os.path.join(os.getcwd(), "temp", file_name)
    with open(file_to_use, "w") as f:
        for i, point in enumerate(points, start=1):
            f.write(f"{i}. {point}\n")
    return f"Outline save to: {file_to_use}"

@tool
def read_document(
    file_name: Annotated[str, "The file path of the document to read"],
    start: Annotated[Optional[int], "The starting line number to read from (0-indexed)"]=None,
    end: Annotated[Optional[int], "The ending line number to read to (0-indexed, exclusive)"]=None
) -> Annotated[str, "The content of the document"]:
    """Read a document and return its content."""

    print("Reading document...")
    file_to_use = os.path.join(os.getcwd(), "temp", file_name)
    with open(file_to_use, "r") as f:
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
    file_to_use = os.path.join(os.getcwd(), "temp", file_name)
    os.makedirs(os.path.dirname(file_to_use), exist_ok=True)
    with open(file_to_use, "w") as f:
        f.write(content)
    print("Writing document...")
    return f"Document saved to: {file_to_use}"

@tool
def edit_document(
    file_name: Annotated[str, "The file path of the document to edit"],
    insert: Annotated[Dict[int, str], "A dictionary where keys are line numbers (0-indexed) and values are the content to insert at those lines"],
):
    """Edit a document."""
    print("Editing document...")
    file_to_use = os.path.join(os.getcwd(), "temp", file_name)
    with open(file_to_use, "r") as f:
        lines = f.readlines()
    sorted_insert = sorted(insert.items())
    
    for line_number, text in sorted_insert:
        if line_number < 0 or line_number > len(lines):
            raise ValueError(f"Line number {line_number} is out of bounds for the document.")
        lines.insert(line_number, text + "\n")
        
    with open(file_to_use, "w") as f:
        f.writelines(lines)
        
    return f"Document edited and saved to: {file_to_use}"

tavily_tool = TavilySearch(
    name="tavily_search",
    description="A tool that can search the web and extract relevant information.",
    max_results=5
)