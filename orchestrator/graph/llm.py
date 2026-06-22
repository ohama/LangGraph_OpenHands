"""orchestrator/graph/llm.py
make_llm() factory — env-based ChatOpenAI configuration for LiteLLM proxy.

Design constraints (ORCH-05):
- base_url and model names ALWAYS come from environment variables.
- This module NEVER instantiates ChatOpenAI at import time (RESEARCH Pitfall 2:
  module-level instantiation reads env vars before load_dotenv() runs in main.py).
- max_retries=0 on every instance — retries amplify load on a wedged qwen-122b;
  let the job FAIL instead of queuing retries that thrash memory.
- streaming=False — LiteLLM 1.86.1 handles non-streaming correctly; no 504 issue.
  ainvoke() returns a complete AIMessage.content regardless of this flag.
- api_key is the literal string "dummy" — the LiteLLM proxy does not check keys
  for local use.

Environment variables (set in .env or launchd plist):
  LITELLM_BASE_URL  — proxy URL, default http://localhost:4000/v1
  RESEARCH_MODEL    — model alias for research_node, default qwen-122b
  PLAN_MODEL        — model alias for plan_node, default qwen-122b
"""
import os

from langchain_openai import ChatOpenAI


def make_llm(
    model: str | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.3,
    timeout: float = 300,
) -> ChatOpenAI:
    """Create a ChatOpenAI instance pointed at the LiteLLM proxy.

    NOT a singleton — each call creates a new instance. This is intentional:
    different nodes need different max_tokens / temperature settings.

    Args:
        model: Model alias override. If None, falls back to os.getenv("RESEARCH_MODEL",
               "qwen-122b"). Callers should pass os.getenv("RESEARCH_MODEL"/"PLAN_MODEL")
               explicitly to satisfy ORCH-05 grep verification.
        max_tokens: Bounded output tokens. research=2000, plan=1500 (ORCH-04 / Pitfall 5).
        temperature: Sampling temperature. research=0.4 (creative), plan=0.2 (deterministic).
        timeout: HTTP timeout in seconds. 300s safe for up to ~12k tokens at 42 tok/s.

    Returns:
        Configured ChatOpenAI instance. Call inside node functions only, never at
        module top-level (RESEARCH Pitfall 2).
    """
    base_url = os.getenv("LITELLM_BASE_URL", "http://localhost:4000/v1")
    resolved_model = model or os.getenv("RESEARCH_MODEL", "qwen-122b")
    return ChatOpenAI(
        model=resolved_model,
        base_url=base_url,
        api_key="dummy",       # LiteLLM proxy does not check the key for local use
        streaming=False,       # Non-streaming works fine; no 504 on LiteLLM 1.86.1
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        max_retries=0,         # CRITICAL: no retries — avoid amplifying load on 122B wedge
    )
