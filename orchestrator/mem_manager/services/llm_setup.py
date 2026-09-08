"""LLM clients for mem_manage's dedup/merge step: one role proposes a
merged memory's text, the other judges whether that merge is faithful.

Both point at the same local OpenAI-compatible endpoint
(config.CUSTOM_API_BASE/CUSTOM_API_KEY) with different model names -
mirroring the original file's shape, minus the commented-out ChatGroq
stubs, the `llm_tool` duplicate of `llm` (no tool-calling anywhere in this
repo, see Decisions.md ADR-010), and `json_fix_llm` (dropped along with the
JSON-repair layer it existed for - both roles here return plain text, not
structured output). `max_retries=0` here is deliberate: services.llm_caller
is what actually retries, so the client itself must not also retry underneath it.

Not imported at module load time by anything else in mem_manage - only
services.dedup_merge's default (no `llm_call` injected) path reaches this
module, so importing the package doesn't require langchain_openai unless a
real LLM merge is actually attempted.
"""
from __future__ import annotations
import os

# from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

import config

# llm = ChatGroq(
#     api_key=os.getenv("GROQ_API_KEY"),
#     model_name=os.getenv("GEN_MODEL_NAME", "gpt-oss-20b"),
#     temperature=config.MERGE_LLM_TEMPERATURE,
#     max_tokens=config.MERGE_LLM_MAX_TOKENS,
#     max_retries=config.LLM_MAX_RETRIES,
# )

# judge_llm = ChatGroq(
#     api_key=os.getenv("GROQ_API_KEY"),
#     model_name=os.getenv("GEN_MODEL_NAME", "gpt-oss-20b"),
#     temperature=config.JUDGE_LLM_TEMPERATURE,
#     max_tokens=config.JUDGE_LLM_MAX_TOKENS,
#     max_retries=config.LLM_MAX_RETRIES,
# )

llm = ChatOpenAI(
    base_url=config.CUSTOM_API_BASE,
    api_key=config.CUSTOM_API_KEY,
    model=config.CUSTOM_API_MODEL_NAME,
    temperature=config.MERGE_LLM_TEMPERATURE,
    max_tokens=config.MERGE_LLM_MAX_TOKENS,
    max_retries=config.LLM_MAX_RETRIES,
)

judge_llm = ChatOpenAI(
    base_url=config.CUSTOM_API_BASE,
    api_key=config.CUSTOM_API_KEY,
    model=config.JUDGE_MODEL_NAME,
    temperature=config.JUDGE_LLM_TEMPERATURE,
    max_tokens=config.JUDGE_LLM_MAX_TOKENS,
    max_retries=config.LLM_MAX_RETRIES,
)
