"""Web research tools."""

from langchain_core.tools import tool
from langchain_tavily import TavilySearch
from langchain_unstructured import UnstructuredLoader

from config import TAVILY_MAX_RESULTS
from logging_config import get_logger

logger = get_logger(__name__)

tavily_tool = TavilySearch(
    name="tavily_search",
    description="A tool that can search the web and extract relevant information.",
    max_results=TAVILY_MAX_RESULTS,
)


@tool
def scrape_webpages(urls: list[str]) -> str:
    """Scrape the provided web pages and return their text content."""
    logger.info("scrape_webpages -> %d url(s)", len(urls))
    loader = UnstructuredLoader(web_url=urls)
    docs = loader.load()
    logger.info("scrape_webpages retrieved %d document(s)", len(docs))
    return "\n".join(
        f'<Document name="{doc.metadata.get("title", "")}">\n{doc.page_content}\n</Document>'
        for doc in docs
    )
