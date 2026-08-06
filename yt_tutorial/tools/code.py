"""Python execution tool.

PythonREPL runs model-generated code with exec() in this process, with no
sandbox and full user privileges. Fine locally; isolate it before this runs
anywhere shared.
"""

from typing import Annotated

from langchain_core.tools import tool
from langchain_experimental.utilities import PythonREPL

from logging_config import get_logger

logger = get_logger(__name__)

# Module-level so state persists across calls within a session.
repl = PythonREPL()


@tool
def python_repl_tool(
    code: Annotated[str, "The Python code to execute to generate a chart"],
) -> Annotated[str, "The printed output of the executed code"]:
    """Use this to execute python code. If you want to see the output of any value,
    you should print it. This is visible to the user"""
    logger.info("python_repl_tool executing %d chars of code", len(code))
    logger.debug("code:\n%s", code)
    try:
        result = repl.run(code)
    except Exception as e:
        logger.exception("python_repl_tool failed")
        return f"Error occurred while executing Python code: {repr(e)}"
    logger.debug("repl output: %s", str(result)[:500])
    return f"Successfully executed Python code:\n```python\n{code}\n```\nOutput:\n{result}"
