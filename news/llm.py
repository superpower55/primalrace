"""LLM client abstraction.

Three providers, chosen via PROVIDER env var (default "ollama"):
  - "anthropic"  → Claude (Opus 4.7 by default), with prompt caching
  - "ollama"     → Ollama Cloud or self-hosted Ollama (Gemma 4 31B by default;
                   Gemma 4 12B on an Oracle Cloud VM, see news/oracle/)
  - "openai"     → any OpenAI-compatible endpoint (Google AI Studio, Groq, …)

All providers return a dict:
  {text, input_tokens, output_tokens, cache_read_tokens, cache_create_tokens, model}

Caching tokens are 0 for Ollama (no caching there).
"""
from __future__ import annotations

import json
import os
import urllib.request

PROVIDER = os.environ.get("PROVIDER", "ollama").lower()


# ============================================================================
# Anthropic
# ============================================================================

def _anthropic_generate(system_blocks: list[dict], user_msg: str,
                        max_tokens: int = 2000) -> dict:
    import anthropic
    client = anthropic.Anthropic()
    model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_blocks,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    u = resp.usage
    return {
        "text": text,
        "model": model,
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_create_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


# ============================================================================
# Ollama (Cloud at https://ollama.com or self-hosted)
# ============================================================================

def _ollama_generate(system_blocks: list[dict], user_msg: str,
                     max_tokens: int = 2000) -> dict:
    """Call /api/chat. Combines the multi-block system prompt into one string —
    Ollama has no prompt-caching API, but a self-hosted server reuses the KV
    cache for an identical prefix, so the system prompt still goes first.

    Self-hosted (CPU) servers need two extra knobs:
      OLLAMA_NUM_CTX   context window; the system prompt alone is ~25k tokens
                       and Ollama silently truncates past its default
      OLLAMA_TIMEOUT   seconds to wait for one response (CPU is slow)
    """
    # `or` so an empty env var (unset GitHub repo variable) still gets the default
    base_url = (os.environ.get("OLLAMA_BASE_URL") or "https://ollama.com").rstrip("/")
    api_key = os.environ.get("OLLAMA_API_KEY", "")
    model = os.environ.get("OLLAMA_MODEL") or "gemma4:31b-cloud"
    num_ctx = int(os.environ.get("OLLAMA_NUM_CTX") or 0)
    timeout = int(os.environ.get("OLLAMA_TIMEOUT") or 180)

    system_text = "\n\n".join(b["text"] for b in system_blocks)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "format": "json",          # ask Ollama to constrain output to valid JSON
        "options": {
            "temperature": 0.7,
            "num_predict": max_tokens,
            **({"num_ctx": num_ctx} if num_ctx else {}),
        },
    }

    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))

    text = data.get("message", {}).get("content", "")
    return {
        "text": text,
        "model": model,
        "input_tokens": data.get("prompt_eval_count", 0) or 0,
        "output_tokens": data.get("eval_count", 0) or 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
    }


# ============================================================================
# OpenAI-compatible (Groq, Google AI Studio, OpenRouter, ollama /v1, …)
# ============================================================================

def _openai_generate(system_blocks: list[dict], user_msg: str,
                     max_tokens: int = 2000) -> dict:
    """POST {base}/chat/completions — works with any OpenAI-compatible host.

    Env:
      OPENAI_BASE_URL  e.g. https://api.groq.com/openai/v1
                       or https://generativelanguage.googleapis.com/v1beta/openai
      OPENAI_API_KEY
      OPENAI_MODEL     e.g. llama-3.3-70b-versatile / gemini-2.0-flash
    """
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if not base_url or not model:
        raise ValueError("PROVIDER=openai needs OPENAI_BASE_URL and OPENAI_MODEL")

    system_text = "\n\n".join(b["text"] for b in system_blocks)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.7,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "primalrace-news/1.0",
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
        method="POST",
    )
    data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            # 503 = provider capacity spike, 429 = per-minute rate limit —
            # both are transient, back off and retry
            if e.code in (429, 503) and attempt < 2:
                import time
                time.sleep(15 * (attempt + 1))
                continue
            raise RuntimeError(f"{base_url} HTTP {e.code}: {body}") from e

    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {}) or {}
    return {
        "text": text,
        "model": model,
        "input_tokens": usage.get("prompt_tokens", 0) or 0,
        "output_tokens": usage.get("completion_tokens", 0) or 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
    }


# ============================================================================
# Dispatch
# ============================================================================

def generate(system_blocks: list[dict], user_msg: str,
             max_tokens: int = 2000) -> dict:
    if PROVIDER == "anthropic":
        return _anthropic_generate(system_blocks, user_msg, max_tokens)
    if PROVIDER == "ollama":
        return _ollama_generate(system_blocks, user_msg, max_tokens)
    if PROVIDER == "openai":
        return _openai_generate(system_blocks, user_msg, max_tokens)
    raise ValueError(f"unknown PROVIDER={PROVIDER!r}; "
                     f"use 'anthropic', 'ollama' or 'openai'")


def provider_label() -> str:
    if PROVIDER == "anthropic":
        return f"anthropic/{os.environ.get('ANTHROPIC_MODEL', 'claude-opus-4-7')}"
    if PROVIDER == "ollama":
        host = os.environ.get("OLLAMA_BASE_URL") or "https://ollama.com"
        return f"ollama@{host}/{os.environ.get('OLLAMA_MODEL') or 'gemma4:31b-cloud'}"
    if PROVIDER == "openai":
        host = os.environ.get("OPENAI_BASE_URL", "?")
        return f"openai@{host}/{os.environ.get('OPENAI_MODEL', '?')}"
    return PROVIDER
