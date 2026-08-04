import os
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI


load_dotenv()

# llm_tool = ChatGroq(
#     api_key=os.getenv("GROQ_API_KEY"),
#     model_name=os.getenv("GEN_MODEL_NAME", "llama-3.1-8b-instant"),
#     temperature=0.1,
#     max_tokens=2048,
#     max_retries=0,
# )

# llm = ChatGroq(
#     api_key=os.getenv("GROQ_API_KEY"),
#     model_name=os.getenv("GEN_MODEL_NAME", "llama-3.1-8b-instant"),
#     temperature=0.1,
#     max_tokens=2048,
#     max_retries=0,
# )

llm_tool = ChatOpenAI(
    base_url=os.getenv("CUSTOM_API_BASE"),
    api_key=os.getenv("CUSTOM_API_KEY"),
    model=os.getenv("CUSTOM_API_MODEL_NAME", "llama-3.1-8b-instruct"),
    temperature=0.1,
    max_tokens=2048,
    max_retries=0,
)

llm = ChatOpenAI(
    base_url=os.getenv("CUSTOM_API_BASE"),
    api_key=os.getenv("CUSTOM_API_KEY"),
    model=os.getenv("CUSTOM_API_MODEL_NAME", "llama-3.1-8b-instruct"),
    temperature=0.1,
    max_tokens=2048,
    max_retries=0,
)

judge_llm = ChatOpenAI(
    base_url=os.getenv("CUSTOM_API_BASE"),
    api_key=os.getenv("CUSTOM_API_KEY"),
    model=os.getenv("CUSTOM_API_MODEL_NAME", "Qwen/Qwen2.5-7B-Instruct"),
    temperature=0.0,
    max_tokens=1024,
    max_retries=0,
)

json_fix_llm = ChatOpenAI(
    base_url=os.getenv("CUSTOM_API_BASE"),
    api_key=os.getenv("CUSTOM_API_KEY"),
    model=os.getenv("CUSTOM_API_MODEL_NAME", "Qwen/Qwen2.5-Coder-3B-Instruct"),
    temperature=0.0,
    max_tokens=1024,
    max_retries=0,
)