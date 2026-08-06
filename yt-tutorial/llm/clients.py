"""LLM client construction.

One shared httpx client carries the TGI compatibility transport, so every
client -- router and worker alike -- speaks the OpenAI wire format regardless
of what the server actually emits.
"""

from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

from config import (
    CUSTOM_API_BASE,
    CUSTOM_API_KEY,
    CUSTOM_API_MODEL_NAME,
    GROQ_API_KEY,
    GROQ_MODEL_NAME,
    LLM_FREQUENCY_PENALTY,
    LLM_MAX_RETRIES,
    LLM_MAX_TOKENS,
    LLM_PROVIDER,
    LLM_RESPONSE_TIMEOUT_SECONDS,
    LLM_TEMPERATURE,
)
from llm.tgi_compat import build_http_client
from logging_config import get_logger

logger = get_logger(__name__)

# Shared across all custom-endpoint clients: rewrites TGI's non-spec payloads.
_http_client = build_http_client()


# Worker calls go through create_agent, which invokes the model directly and so
# never reaches invoker.py's timeout. Setting it on the client covers both paths;
# without it the SDK default applies and one stalled generation blocks for ten
# minutes -- long enough to look like a hang.
_TIMEOUT_SETTINGS = {"timeout": LLM_RESPONSE_TIMEOUT_SECONDS}


def _build_custom_client(**overrides):
    """A client for any OpenAI-compatible endpoint (local TGI, vLLM, ...)."""
    settings = {
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
        "frequency_penalty": LLM_FREQUENCY_PENALTY,
        **_TIMEOUT_SETTINGS,
        **overrides,
    }
    return ChatOpenAI(
        base_url=CUSTOM_API_BASE,
        api_key=CUSTOM_API_KEY,
        model=CUSTOM_API_MODEL_NAME,
        max_retries=LLM_MAX_RETRIES,
        http_client=_http_client,
        **settings,
    )


def _build_groq_client(**overrides):
    settings = {
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
        "frequency_penalty": LLM_FREQUENCY_PENALTY,
        **_TIMEOUT_SETTINGS,
        **overrides,
    }
    return ChatGroq(
        api_key=GROQ_API_KEY,
        model_name=GROQ_MODEL_NAME,
        max_retries=LLM_MAX_RETRIES,
        **settings,
    )


def build_llm(**overrides):
    """Build a chat model for the provider named in config.LLM_PROVIDER."""
    if LLM_PROVIDER == "groq":
        return _build_groq_client(**overrides)
    return _build_custom_client(**overrides)


# The single model used by every supervisor and worker.
llm = build_llm()
logger.info(
    "LLM ready | provider=%s | model=%s",
    LLM_PROVIDER,
    GROQ_MODEL_NAME if LLM_PROVIDER == "groq" else CUSTOM_API_MODEL_NAME,
)
